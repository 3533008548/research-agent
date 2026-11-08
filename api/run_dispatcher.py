"""Bounded, session-aware execution inside one durable worker process.

Redis retains every admitted item until execution and persistence succeed.
Waiting sessions consume a bounded admission buffer, never an executor slot.
The process lock is intentional: SQLite and shared file tools are not yet a
distributed execution system. Renewing Redis ownership alone cannot fence
arbitrary tool side effects in a second worker process.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from api.redis_runs import QueueUnavailableError, RunOwnershipLostError

log = logging.getLogger(__name__)


@contextmanager
def worker_process_lock(path: Path):
    """OS-released lifetime lock; never expires while an old process lives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another api-worker owns this data directory; use in-process concurrency") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class _Entry:
    kind: str
    worker: object
    job: object
    future: Future | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    lost: threading.Event = field(default_factory=threading.Event)
    retry_at: float = 0
    healthy: bool = True
    waiting_stage: str = ""

    @property
    def resources(self) -> set[str]:
        result = {f"run:{self.job.run_id}"}
        for name in ("session_id", "project_id"):
            value = getattr(self.job, name, None)
            if value:
                result.add(f"{name}:{value}")
        return result


class RunDispatcher:
    def __init__(self, workers: dict, *, pending_per_kind: int = 32, poll_seconds: float = 0.05):
        self.workers = {kind: worker for kind, (worker, _) in workers.items()}
        self.limits = {kind: max(1, int(limit)) for kind, (_, limit) in workers.items()}
        self.pending_per_kind = max(1, int(pending_per_kind))
        self.poll_seconds = poll_seconds
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=sum(self.limits.values()), thread_name_prefix="run")
        self._renew_stop = threading.Event()
        self._renew_interval = min(5.0, min(w.claim_idle_ms for w in self.workers.values()) / 3000)

    def _check(self, entry: _Entry) -> None:
        if entry.lost.is_set() or not entry.healthy:
            raise RunOwnershipLostError("Run ownership unavailable; execution left unacknowledged")

    def _renew(self) -> None:
        for kind, worker in self.workers.items():
            with self._lock:
                entries = [entry for entry in self._entries.values() if entry.kind == kind]
            try:
                worker.broker.heartbeat(worker.consumer)
                owned = worker.broker.renew(worker.consumer, [entry.job.message_id for entry in entries])
                for entry in entries:
                    entry.healthy = True
                    if entry.job.message_id not in owned:
                        entry.lost.set()
                        entry.cancel.set()
            except QueueUnavailableError:
                for entry in entries:
                    entry.healthy = False
                    entry.cancel.set()
                log.warning("Run lease renewal unavailable for %s", kind)

    def _keep_alive(self) -> None:
        while not self._renew_stop.wait(self._renew_interval):
            self._renew()

    def _finish(self) -> None:
        with self._lock:
            snapshot = list(self._entries.items())
        for key, entry in snapshot:
            if entry.future is None or not entry.future.done():
                if entry.future is None and entry.lost.is_set():
                    with self._lock:
                        self._entries.pop(key, None)
                continue
            try:
                entry.future.result()
                self._check(entry)
                if not entry.worker.broker.acknowledge_owned(entry.worker.consumer, entry.job.message_id):
                    entry.lost.set()
                else:
                    entry.worker.broker.clear_cancel(entry.job.run_id)
            except Exception as exc:
                # Do not log exception text: tool exceptions may contain
                # request content. The persisted run carries safe details.
                log.warning("Run attempt retained for recovery: %s", type(exc).__name__)
                if not entry.lost.is_set():
                    entry.future = None
                    entry.retry_at = time.monotonic() + 1
                    continue
            with self._lock:
                self._entries.pop(key, None)

    def _admit(self) -> None:
        for kind, worker in self.workers.items():
            with self._lock:
                count = sum(entry.kind == kind for entry in self._entries.values())
            # Bounded prefetch lets another session bypass a busy session.
            # Leave overflow in Redis rather than creating an unbounded pool queue.
            for _ in range(self.pending_per_kind + self.limits[kind] - count):
                job = worker.broker.claim_stale(worker.consumer, min_idle_ms=worker.claim_idle_ms)
                if job is None:
                    job = worker.broker.reserve(worker.consumer, block_ms=1)
                if job is None:
                    break
                key = (kind, job.message_id)
                with self._lock:
                    if key in self._entries:
                        continue
                    self._entries[key] = _Entry(kind, worker, job)

    def _execute(self, entry: _Entry, cancelled: bool) -> None:
        # Check actual Redis ownership at dispatch and before acknowledgement;
        # the renewer covers long model calls and the admission buffer.
        if entry.job.message_id not in entry.worker.broker.renew(entry.worker.consumer, [entry.job.message_id]):
            entry.lost.set()
        self._check(entry)
        if cancelled:
            entry.worker._finish_cancelled(entry.job)
        else:
            entry.worker.execute(entry.job, check=lambda: self._check(entry), cancel_event=entry.cancel)

    def _dispatch(self) -> None:
        with self._lock:
            snapshot = list(self._entries.values())
        if snapshot:
            occupied: set[str] = set()
            counts = dict.fromkeys(self.workers, 0)
            for entry in snapshot:
                if entry.future is not None:
                    occupied.update(entry.resources)
                    counts[entry.kind] += 1
            # Stream IDs order admissions across chat/research queues. For
            # simultaneous IDs the kind is a deterministic tie breaker.
            entries = sorted(snapshot, key=lambda e: (
                tuple(int(part) for part in e.job.message_id.split("-")), e.kind,
            ))
            for entry in entries:
                if entry.future is not None or entry.lost.is_set():
                    continue
                cancelled = entry.worker.broker.cancel_requested(entry.job.run_id)
                ready = entry.retry_at <= time.monotonic() and entry.healthy
                available = counts[entry.kind] < self.limits[entry.kind]
                if ready and cancelled:
                    # Queue cancellation only writes metadata: it needs no
                    # model slot and must not wait behind a long active task.
                    future = Future()
                    try:
                        self._execute(entry, True)
                        future.set_result(None)
                    except Exception as exc:
                        future.set_exception(exc)
                    entry.future = future
                    continue
                session_ready = entry.worker.session_ready(entry.job)
                if ready and available and session_ready and not entry.resources & occupied:
                    entry.cancel.clear()
                    entry.future = self._pool.submit(self._execute, entry, cancelled)
                    counts[entry.kind] += 1
                elif ready:
                    stage = "session_wait" if entry.resources & occupied or not session_ready else "capacity_wait"
                    if stage != entry.waiting_stage:
                        entry.worker.broker.publish(entry.job.run_id, {"type": "status", "status": "queued", "stage": stage})
                        entry.waiting_stage = stage
                # A later turn of this session must not overtake a waiting
                # earlier turn, even when the earlier task's kind is full.
                if session_ready:
                    occupied.update(entry.resources)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        renewer = threading.Thread(target=self._keep_alive, name="run-leases", daemon=True)
        renewer.start()
        metrics_at = 0.0
        try:
            while not stop.is_set():
                try:
                    self._finish()
                    self._admit()
                    self._dispatch()
                    if time.monotonic() >= metrics_at:
                        for worker in self.workers.values():
                            worker.publish_retrieval_metrics()
                        metrics_at = time.monotonic() + 5
                except QueueUnavailableError:
                    log.warning("Redis unavailable; dispatcher will retry")
                    stop.wait(0.5)
                stop.wait(self.poll_seconds)
        finally:
            # Stop intake, drain executions with leases still alive. Buffered
            # entries stay unacknowledged for the next process to recover.
            self._pool.shutdown(wait=True)
            try:
                self._finish()
            finally:
                self._renew_stop.set()
                renewer.join()

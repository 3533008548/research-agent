"""Redis-backed queue, SSE event log and worker for durable chat runs.

Redis carries only transient request text and streamed output.  The final
answer, metrics and audit-safe events remain in ``SessionStore`` so clients can
still query a completed run after the Redis event key expires.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class QueueUnavailableError(RuntimeError):
    """The API could not durably enqueue a run."""


@dataclass(frozen=True)
class QueuedChatRun:
    message_id: str
    run_id: str
    session_id: str
    message: str


class RedisChatRunBroker:
    """A thin synchronous Redis Streams adapter used by API processes and workers."""

    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "research-agent",
        client=None,
        event_ttl_seconds: int = 86_400,
    ) -> None:
        if client is None:
            try:
                import redis
            except ImportError as exc:  # Clear deployment guidance, not an import-time crash.
                raise QueueUnavailableError("install the 'redis' package for REDIS_URL support") from exc
            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3)
        self._redis = client
        self.prefix = prefix.strip(":") or "research-agent"
        self.event_ttl_seconds = max(60, int(event_ttl_seconds))

    @property
    def queue_key(self) -> str:
        return f"{self.prefix}:chat-runs"

    @property
    def group_name(self) -> str:
        return f"{self.prefix}:workers"

    def event_key(self, run_id: str) -> str:
        return f"{self.prefix}:chat-run:{run_id}:events"

    def cancel_key(self, run_id: str) -> str:
        return f"{self.prefix}:chat-run:{run_id}:cancel"

    def ping(self) -> None:
        try:
            if not self._redis.ping():
                raise RuntimeError("Redis PING returned false")
        except Exception as exc:
            raise QueueUnavailableError("Redis is unavailable") from exc

    def enqueue(self, run_id: str, session_id: str, message: str) -> str:
        try:
            item_id = self._redis.xadd(
                self.queue_key,
                {"run_id": run_id, "session_id": session_id, "message": message},
            )
            return str(item_id)
        except Exception as exc:
            raise QueueUnavailableError("could not enqueue chat run") from exc

    def _ensure_group(self) -> None:
        try:
            self._redis.xgroup_create(self.queue_key, self.group_name, id="0", mkstream=True)
        except Exception as exc:
            # Redis reports BUSYGROUP when another worker made the group first.
            if "BUSYGROUP" not in str(exc):
                raise QueueUnavailableError("could not initialize Redis worker group") from exc

    @staticmethod
    def _decode_job(message_id: str, fields: dict[str, Any]) -> QueuedChatRun | None:
        try:
            return QueuedChatRun(
                message_id=str(message_id),
                run_id=str(fields["run_id"]),
                session_id=str(fields["session_id"]),
                message=str(fields["message"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def reserve(self, consumer: str, *, block_ms: int = 1_000) -> QueuedChatRun | None:
        self._ensure_group()
        try:
            rows = self._redis.xreadgroup(
                self.group_name,
                consumer,
                {self.queue_key: ">"},
                count=1,
                block=max(1, int(block_ms)),
            )
        except Exception as exc:
            raise QueueUnavailableError("could not reserve chat run") from exc
        if not rows:
            return None
        _stream, messages = rows[0]
        if not messages:
            return None
        message_id, fields = messages[0]
        return self._decode_job(message_id, fields)

    def claim_stale(
        self,
        consumer: str,
        *,
        min_idle_ms: int = 120_000,
    ) -> QueuedChatRun | None:
        """Recover one unacknowledged job after a worker crashes or is restarted."""
        self._ensure_group()
        try:
            result = self._redis.xautoclaim(
                self.queue_key,
                self.group_name,
                consumer,
                min_idle_time=max(1, int(min_idle_ms)),
                start_id="0-0",
                count=1,
            )
        except Exception as exc:
            raise QueueUnavailableError("could not recover pending chat run") from exc
        messages = result[1] if len(result) > 1 else []
        if not messages:
            return None
        message_id, fields = messages[0]
        return self._decode_job(message_id, fields)

    def acknowledge(self, message_id: str) -> None:
        try:
            # Acknowledge protects crash recovery; delete immediately after so
            # request text is not retained in the Redis Stream after execution.
            pipe = self._redis.pipeline()
            pipe.xack(self.queue_key, self.group_name, message_id)
            pipe.xdel(self.queue_key, message_id)
            pipe.execute()
        except Exception as exc:
            raise QueueUnavailableError("could not acknowledge chat run") from exc

    def publish(self, run_id: str, event: dict[str, Any]) -> str:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        try:
            pipe = self._redis.pipeline()
            pipe.xadd(self.event_key(run_id), {"payload": payload}, maxlen=1_000, approximate=True)
            pipe.expire(self.event_key(run_id), self.event_ttl_seconds)
            result = pipe.execute()
            return str(result[0])
        except Exception as exc:
            raise QueueUnavailableError("could not publish chat-run event") from exc

    def events(
        self,
        run_id: str,
        cursor: str = "0-0",
        *,
        block_ms: int = 1_000,
    ) -> tuple[list[dict[str, Any]], str]:
        try:
            rows = self._redis.xread(
                {self.event_key(run_id): cursor}, count=100, block=max(1, int(block_ms)),
            )
        except Exception as exc:
            raise QueueUnavailableError("could not read chat-run events") from exc
        if not rows:
            return [], cursor
        _stream, messages = rows[0]
        events: list[dict[str, Any]] = []
        next_cursor = cursor
        for message_id, fields in messages:
            next_cursor = str(message_id)
            try:
                event = json.loads(str(fields.get("payload", "{}")))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events, next_cursor

    def request_cancel(self, run_id: str) -> None:
        try:
            self._redis.set(self.cancel_key(run_id), "1", ex=self.event_ttl_seconds)
        except Exception as exc:
            raise QueueUnavailableError("could not request chat-run cancellation") from exc

    def cancel_requested(self, run_id: str) -> bool:
        try:
            return bool(self._redis.exists(self.cancel_key(run_id)))
        except Exception as exc:
            raise QueueUnavailableError("could not read chat-run cancellation") from exc

    def clear_cancel(self, run_id: str) -> None:
        try:
            self._redis.delete(self.cancel_key(run_id))
        except Exception:
            # Expiry is sufficient; failure to delete a terminal marker must not
            # change the worker's final result.
            return


class RedisChatRunManager:
    """HTTP-side manager: persist metadata, enqueue work, stream Redis events."""

    def __init__(self, agent, broker: RedisChatRunBroker) -> None:
        self.agent = agent
        self.broker = broker

    def start(self, session_id: str, message: str) -> dict[str, Any]:
        if not self.agent.sessions.get(session_id):
            raise KeyError(session_id)
        run = self.agent.sessions.create_chat_run(session_id, self.agent.model, status="queued")
        run_id = str(run["run_id"])
        try:
            self.broker.enqueue(run_id, session_id, message)
            self.broker.publish(run_id, {"type": "status", "status": "queued", "run_id": run_id})
        except QueueUnavailableError:
            self.agent.sessions.update_chat_run(run_id, status="failed", error_type="QueueUnavailableError")
            raise
        self.agent.sessions.add_run_event(
            session_id, run_id, "chat", "api", "queue", "queued",
            summary="API chat run queued for a durable worker",
        )
        return self.get(run_id) or run

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.agent.sessions.get_chat_run(run_id)

    def list_for_session(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.agent.sessions.list_chat_runs(session_id, limit=limit)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        run = self.get(run_id)
        if not run or run.get("status") in _TERMINAL_STATUSES:
            return run
        self.broker.request_cancel(run_id)
        updated = self.agent.sessions.update_chat_run(run_id, status="cancelling")
        if updated:
            self.broker.publish(run_id, {"type": "status", "status": "cancelling", "run_id": run_id})
            self.agent.sessions.add_run_event(
                updated["thread_id"], run_id, "chat", "api", "cancel", "cancelling",
                summary="Cancellation requested by API client",
            )
        return updated

    def cancel_for_session(self, session_id: str) -> None:
        for run in self.list_for_session(session_id, limit=100):
            if run.get("status") not in _TERMINAL_STATUSES:
                self.cancel(str(run["run_id"]))

    def stream(self, run_id: str) -> Iterator[str]:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        yield self._sse({"type": "status", "status": run["status"], "run_id": run_id})
        cursor = "0-0"
        while True:
            events, cursor = self.broker.events(run_id, cursor)
            for event in events:
                yield self._sse(event)
                if event.get("type") == "done":
                    return
            run = self.get(run_id)
            if not run:
                return
            if run.get("status") in _TERMINAL_STATUSES:
                yield self._sse({
                    "type": "done", "status": run["status"], "answer": run.get("answer", ""),
                })
                return
            yield ": keep-alive\n\n"

    @staticmethod
    def _sse(event: dict[str, Any]) -> str:
        event_type = str(event.get("type") or "message")
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {data}\n\n"


class RedisChatRunWorker:
    """The single durable worker that executes queued chat runs against the agent."""

    def __init__(
        self,
        agent,
        broker: RedisChatRunBroker,
        *,
        consumer: str | None = None,
        claim_idle_ms: int = 120_000,
    ) -> None:
        self.agent = agent
        self.broker = broker
        self.consumer = consumer or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.claim_idle_ms = max(1_000, int(claim_idle_ms))

    def run_once(self, *, block_ms: int = 1_000) -> bool:
        job = self.broker.claim_stale(self.consumer, min_idle_ms=self.claim_idle_ms)
        if job is None:
            job = self.broker.reserve(self.consumer, block_ms=block_ms)
        if job is None:
            return False
        try:
            self._process(job)
        finally:
            self.broker.acknowledge(job.message_id)
        return True

    def run_forever(self) -> None:
        while True:
            self.run_once(block_ms=5_000)

    def _process(self, job: QueuedChatRun) -> None:
        run = self.agent.sessions.get_chat_run(job.run_id)
        if not run or run.get("thread_id") != job.session_id:
            return  # The session was deleted after enqueueing.
        if run.get("status") in _TERMINAL_STATUSES:
            return
        if self.broker.cancel_requested(job.run_id):
            self._finish_cancelled(job)
            return

        self.agent.sessions.update_chat_run(job.run_id, status="running")
        self.broker.publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "chat", "api_worker", "run", "running",
            summary="Durable API worker started chat run",
        )

        cancel_event = threading.Event()
        stop_monitor = threading.Event()

        def monitor_cancel() -> None:
            while not stop_monitor.wait(0.1):
                if self.broker.cancel_requested(job.run_id):
                    cancel_event.set()
                    return

        monitor = threading.Thread(target=monitor_cancel, name=f"api-cancel-{job.run_id[-8:]}", daemon=True)
        monitor.start()

        def on_token(token: str) -> None:
            if token and not cancel_event.is_set():
                self.broker.publish(job.run_id, {"type": "token", "text": token})

        try:
            answer = self.agent.step(
                job.message,
                session_id=job.session_id,
                run_id=job.run_id,
                cancel_event=cancel_event,
                on_token=on_token,
            )
            trace = self.agent.get_last_trace(job.session_id) or {}
            current = self.agent.sessions.get_chat_run(job.run_id) or run
            outcome = trace.get("outcome")
            result_status = "cancelled" if cancel_event.is_set() or outcome == "cancelled" else (
                "completed" if outcome == "success" else "failed"
            )
            if current.get("status") not in _TERMINAL_STATUSES:
                self.agent.sessions.update_chat_run(job.run_id, status=result_status, answer=answer)
            self.broker.publish(job.run_id, {"type": "done", "status": result_status, "answer": answer})
        except Exception as exc:
            self.agent.sessions.update_chat_run(
                job.run_id, status="failed", answer="", error_type=type(exc).__name__,
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "chat", "api_worker", "run", "failed",
                summary="Durable API worker failed", error_type=type(exc).__name__,
            )
            self.broker.publish(job.run_id, {"type": "error", "error_type": type(exc).__name__})
            self.broker.publish(job.run_id, {"type": "done", "status": "failed", "answer": ""})
        finally:
            stop_monitor.set()
            monitor.join(timeout=0.2)
            self.broker.clear_cancel(job.run_id)

    def _finish_cancelled(self, job: QueuedChatRun) -> None:
        self.agent.sessions.update_chat_run(job.run_id, status="cancelled", answer="")
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "chat", "api_worker", "run", "cancelled",
            summary="Queued API chat run was cancelled before execution",
        )
        self.broker.publish(job.run_id, {"type": "done", "status": "cancelled", "answer": ""})
        self.broker.clear_cancel(job.run_id)

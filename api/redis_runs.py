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

from api.sse import format_sse
from cancellation import RequestCancelledError
from papers.metrics import load_published as load_published_retrieval_metrics
from papers.metrics import publish as publish_retrieval_metrics
from run_contract import bounded_timing_metrics, execution_metadata, safe_error_details, latest_error_details


_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "partial_failed"}


class QueueUnavailableError(RuntimeError):
    """The API could not durably enqueue a run."""


class RunOwnershipLostError(QueueUnavailableError):
    """An observer must not acknowledge or publish for a different owner."""


@dataclass(frozen=True)
class QueuedChatRun:
    message_id: str
    run_id: str
    session_id: str
    message: str


class RedisChatRunBroker:
    """A synchronous Redis Streams adapter for one durable run kind.

    Subclasses only change the stream/group names and job encoding.  Event and
    cancellation keys deliberately use ``run_id`` alone, so every public run
    type has the same SSE/cancellation protocol.
    """

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
            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=5)
        self._redis = client
        self.prefix = prefix.strip(":") or "research-agent"
        self.event_ttl_seconds = max(60, int(event_ttl_seconds))
        self._claim_cursors: dict[str, str] = {}

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
                start_id=self._claim_cursors.get(consumer, "0-0"),
                count=1,
            )
        except Exception as exc:
            raise QueueUnavailableError("could not recover pending chat run") from exc
        self._claim_cursors[consumer] = str(result[0])
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

    def renew(self, consumer: str, message_ids: list[str]) -> set[str]:
        """Refresh PEL idle timers only while this consumer still owns them.

        A separate worker heartbeat does not refresh Redis pending messages.
        The ownership check and XCLAIM must be atomic to avoid stealing back
        an entry that has already been recovered by another consumer.
        """
        if not message_ids:
            return set()
        script = """
        local owned = {}
        for i = 3, #ARGV do
            local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[i], ARGV[i], 1)
            if #pending > 0 and pending[1][2] == ARGV[2] then
                local ids = redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0, ARGV[i], 'JUSTID')
                if #ids > 0 then table.insert(owned, ARGV[i]) end
            end
        end
        return owned
        """
        try:
            return set(self._redis.eval(script, 1, self.queue_key, self.group_name, consumer, *message_ids))
        except Exception as exc:
            raise QueueUnavailableError("could not renew pending runs") from exc

    def acknowledge_owned(self, consumer: str, message_id: str) -> bool:
        script = """
        local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1)
        if #pending == 0 or pending[1][2] ~= ARGV[2] then return 0 end
        redis.call('XACK', KEYS[1], ARGV[1], ARGV[3])
        redis.call('XDEL', KEYS[1], ARGV[3])
        return 1
        """
        try:
            return bool(self._redis.eval(script, 1, self.queue_key, self.group_name, consumer, message_id))
        except Exception as exc:
            raise QueueUnavailableError("could not acknowledge owned run") from exc

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

    def _worker_heartbeat_key(self, consumer: str) -> str:
        return f"{self.queue_key}:worker:{consumer}"

    def heartbeat(self, consumer: str, *, ttl_seconds: int = 15) -> None:
        """Advertise a worker without retaining requests or worker internals."""
        try:
            self._redis.set(
                self._worker_heartbeat_key(consumer), "1", ex=max(5, int(ttl_seconds)),
            )
        except Exception as exc:
            raise QueueUnavailableError("could not record worker heartbeat") from exc

    def queue_depth(self) -> int:
        """Return unacknowledged stream entries; request payloads stay unread."""
        try:
            return int(self._redis.xlen(self.queue_key))
        except Exception as exc:
            raise QueueUnavailableError("could not read queue depth") from exc

    def live_worker_count(self) -> int:
        """Count only short-lived heartbeat keys for this run-kind queue."""
        try:
            return sum(1 for _ in self._redis.scan_iter(match=f"{self.queue_key}:worker:*", count=100))
        except Exception as exc:
            raise QueueUnavailableError("could not read worker heartbeats") from exc

    def publish_retrieval_metrics(self) -> None:
        """Mirror this worker's retrieval aggregates for the web process to serve.

        Retrieval runs in the worker, but ``/metrics`` is rendered by the web
        process, so the numbers have to cross the process boundary.  A TTL'd
        key (same shape as the heartbeat) means a dead worker stops being
        reported instead of freezing stale latency in the dashboard forever.

        Unlike the heartbeat this one does *not* raise on failure: losing a
        telemetry sample must never take down a run that is otherwise healthy.
        """
        try:
            publish_retrieval_metrics(self._redis)
        except Exception:  # noqa: BLE001 - telemetry is best-effort by design
            pass

    def load_retrieval_metrics(self) -> dict | None:
        """Read published retrieval aggregates, or None when unavailable."""
        try:
            return load_published_retrieval_metrics(self._redis)
        except Exception:  # noqa: BLE001
            return None


@dataclass(frozen=True)
class QueuedResearchRun:
    message_id: str
    run_id: str
    session_id: str
    query: str
    scope: str
    resume: bool = False
    job_id: str = ""


class RedisResearchRunBroker(RedisChatRunBroker):
    """Redis stream for durable deep-research work."""

    @property
    def queue_key(self) -> str:
        return f"{self.prefix}:research-runs"

    @property
    def group_name(self) -> str:
        return f"{self.prefix}:research-workers"

    def enqueue(
        self, run_id: str, session_id: str, query: str, scope: str, *, resume: bool = False,
        job_id: str = "",
    ) -> str:
        try:
            item_id = self._redis.xadd(
                self.queue_key,
                {
                    "run_id": run_id, "session_id": session_id, "query": query,
                    "scope": scope, "resume": "1" if resume else "0",
                    "job_id": job_id,
                },
            )
            return str(item_id)
        except Exception as exc:
            raise QueueUnavailableError("could not enqueue research run") from exc

    @staticmethod
    def _decode_job(message_id: str, fields: dict[str, Any]) -> QueuedResearchRun | None:
        try:
            return QueuedResearchRun(
                message_id=str(message_id), run_id=str(fields["run_id"]),
                session_id=str(fields["session_id"]), query=str(fields["query"]),
                scope=str(fields.get("scope") or "both"),
                resume=str(fields.get("resume") or "").strip().lower() in {"1", "true", "yes"},
                job_id=str(fields.get("job_id") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class QueuedDailyRun:
    message_id: str
    run_id: str
    kind: str


class RedisDailyRunBroker(RedisChatRunBroker):
    """Redis stream for scheduled/daily discovery work."""

    @property
    def queue_key(self) -> str:
        return f"{self.prefix}:daily-runs"

    @property
    def group_name(self) -> str:
        return f"{self.prefix}:daily-workers"

    def enqueue(self, run_id: str, kind: str) -> str:
        try:
            return str(self._redis.xadd(self.queue_key, {"run_id": run_id, "kind": kind}))
        except Exception as exc:
            raise QueueUnavailableError("could not enqueue daily run") from exc

    @staticmethod
    def _decode_job(message_id: str, fields: dict[str, Any]) -> QueuedDailyRun | None:
        try:
            return QueuedDailyRun(
                message_id=str(message_id), run_id=str(fields["run_id"]), kind=str(fields["kind"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


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
            event_type="status",
            metadata=execution_metadata(
                "chat", runner="redis-worker", model=str(self.agent.model), toolset="chat-default",
            ),
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
        yield format_sse({"type": "status", "status": run["status"], "run_id": run_id})
        cursor = "0-0"
        while True:
            events, cursor = self.broker.events(run_id, cursor)
            for event in events:
                yield format_sse(event)
                if event.get("type") == "done":
                    return
            run = self.get(run_id)
            if not run:
                return
            if run.get("status") in _TERMINAL_STATUSES:
                details = latest_error_details(self.agent.sessions.get_run_events(run_id)) if run["status"] == "failed" else {}
                yield format_sse({
                    "type": "done", "status": run["status"], "answer": run.get("answer", ""),
                    **({"error_details": details} if details else {}),
                })
                return
            yield ": keep-alive\n\n"

class _RedisRunWorker:
    """Shared Redis Streams polling and cooperative-cancellation plumbing."""

    def __init__(self, broker: RedisChatRunBroker, *, consumer: str, claim_idle_ms: int) -> None:
        self.broker = broker
        self.consumer = consumer
        self.claim_idle_ms = max(1_000, int(claim_idle_ms))
        self._execution_local = threading.local()

    def _check_execution(self) -> None:
        check = getattr(self._execution_local, "check", None)
        if check is not None:
            check()

    def _publish(self, run_id: str, event: dict) -> None:
        self._check_execution()
        self.broker.publish(run_id, event)

    def session_ready(self, job) -> bool:
        if getattr(job, "job_id", ""):
            return True
        session_id = getattr(job, "session_id", None)
        if not session_id:
            return True
        store = getattr(self, "sessions", None) or self.agent.sessions
        first = store.first_pending_run(session_id)
        return first is None or first == job.run_id

    def execute(self, job, *, check, cancel_event: threading.Event) -> None:
        self._execution_local.check = check
        self._execution_local.cancel_event = cancel_event
        try:
            check()
            self._process(job)
            check()
        finally:
            self._execution_local.__dict__.clear()

    def run_once(self, *, block_ms: int = 1_000) -> bool:
        job = self.broker.claim_stale(self.consumer, min_idle_ms=self.claim_idle_ms)
        if job is None:
            job = self.broker.reserve(self.consumer, block_ms=block_ms)
        if job is None:
            return False
        self._process(job)
        # An escaping infrastructure/storage error leaves the item pending.
        self.broker.acknowledge(job.message_id)
        self.broker.clear_cancel(job.run_id)
        return True

    def publish_retrieval_metrics(self) -> None:
        """Push this process's retrieval aggregates so the web process can serve them.

        Retrieval runs here, in the worker; ``/metrics`` is served by the web
        process.  Without this hop the endpoint reports a permanent zero for
        every retrieval stage.  Uses the same TTL'd-key transport as the
        worker heartbeat, and swallows every failure for the same reason the
        heartbeat does: instrumentation must not take down the run.
        """
        self.broker.publish_retrieval_metrics()

    def run_forever(self) -> None:
        from api.run_dispatcher import RunDispatcher
        RunDispatcher({"run": (self, 1)}).run_forever()

    def _cancel_monitor(self, run_id: str) -> tuple[threading.Event, threading.Event, threading.Thread]:
        cancel_event = getattr(self._execution_local, "cancel_event", None) or threading.Event()
        stop_monitor = threading.Event()

        def monitor_cancel() -> None:
            while not stop_monitor.wait(0.1):
                try:
                    cancelled = self.broker.cancel_requested(run_id)
                except QueueUnavailableError:
                    cancel_event.set()
                    return
                if stop_monitor.is_set():
                    return
                if cancelled:
                    cancel_event.set()
                    return

        monitor = threading.Thread(
            target=monitor_cancel,
            name=f"api-cancel-{run_id[-8:]}",
            daemon=True,
        )
        monitor.start()
        return cancel_event, stop_monitor, monitor


class RedisChatRunWorker(_RedisRunWorker):
    """The durable worker that executes queued chat runs against the agent."""

    def __init__(
        self,
        agent,
        broker: RedisChatRunBroker,
        *,
        consumer: str | None = None,
        claim_idle_ms: int = 120_000,
    ) -> None:
        self.agent = agent
        super().__init__(
            broker,
            consumer=consumer or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}",
            claim_idle_ms=claim_idle_ms,
        )

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
        self._publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "chat", "api_worker", "run", "running",
            summary="Durable API worker started chat run",
            event_type="status",
        )

        cancel_event, stop_monitor, monitor = self._cancel_monitor(job.run_id)

        def on_token(token: str) -> None:
            if token and not cancel_event.is_set():
                self._publish(job.run_id, {"type": "token", "text": token})

        def consume_steers(stage: str) -> list[dict[str, Any]]:
            return self.agent.sessions.consume_run_steers(
                job.run_id, job.session_id, stage=stage,
            )

        def on_steer(_stage: str, _count: int) -> None:
            self._publish(job.run_id, {
                "type": "status", "status": "running", "stage": "steer_consumed",
                "run_id": job.run_id,
            })

        try:
            answer = self.agent.step(
                job.message,
                session_id=job.session_id,
                run_id=job.run_id,
                cancel_event=cancel_event,
                on_token=on_token,
                steer_provider=consume_steers,
                on_steer=on_steer,
            )
            self._check_execution()
            trace = self.agent.get_last_trace(job.session_id) or {}
            # Only the existing trace's tool identifier and its lifecycle are
            # exposed.  Arguments, search results and model text never enter
            # the Redis event log.
            for tool_event in trace.get("tool_trace", []):
                tool_name = str(tool_event.get("tool") or "")[:80]
                if tool_name:
                    duration_ms = tool_event.get("duration_ms")
                    metrics = {"duration_ms": duration_ms} if isinstance(duration_ms, (int, float)) else None
                    self.agent.sessions.add_run_event(
                        job.session_id, job.run_id, "chat", "single_agent", "tool", "completed",
                        summary="工具调用已完成", metrics=metrics, event_type="tool",
                    )
                    self._publish(job.run_id, {
                        "type": "tool", "tool": tool_name, "status": "completed",
                        **({"duration_ms": round(float(duration_ms), 1)} if isinstance(duration_ms, (int, float)) else {}),
                    })
            current = self.agent.sessions.get_chat_run(job.run_id) or run
            outcome = trace.get("outcome")
            result_status = "cancelled" if cancel_event.is_set() or outcome == "cancelled" else (
                "completed" if outcome == "success" else "failed"
            )
            if current.get("status") not in _TERMINAL_STATUSES:
                self.agent.sessions.update_chat_run(job.run_id, status=result_status, answer=answer)
            details = safe_error_details(trace.get("error_details")) if result_status == "failed" else {}
            self._publish(job.run_id, {"type": "done", "status": result_status, "answer": answer, **({"error_details": details} if details else {})})
        except QueueUnavailableError:
            raise
        except Exception as exc:
            self._check_execution()
            details = safe_error_details(getattr(exc, "error_details", None))
            self.agent.sessions.update_chat_run(
                job.run_id, status="failed", answer="", error_type=type(exc).__name__,
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "chat", "api_worker", "run", "failed",
                summary="Durable API worker failed", error_type=type(exc).__name__, event_type="error", error_details=details,
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "chat", "api_worker", "run", "failed",
                summary="聊天运行已结束", event_type="done",
            )
            self._publish(job.run_id, {"type": "error", "error_type": type(exc).__name__, **({"error_details": details} if details else {})})
            self._publish(job.run_id, {"type": "done", "status": "failed", "answer": "", **({"error_details": details} if details else {})})
        finally:
            stop_monitor.set()
            monitor.join(timeout=0.2)

    def _finish_cancelled(self, job: QueuedChatRun) -> None:
        self.agent.sessions.update_chat_run(job.run_id, status="cancelled", answer="")
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "chat", "api_worker", "run", "cancelled",
            summary="Queued API chat run was cancelled before execution", event_type="done",
        )
        self._publish(job.run_id, {"type": "done", "status": "cancelled", "answer": ""})
        self.broker.clear_cancel(job.run_id)


def _safe_status_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep only de-identified stage/status fields from existing progress traces.

    ``at_ms``/``duration_ms`` are two bounded numbers and are forwarded so the
    live timeline can draw a stage the moment it finishes, instead of waiting
    for a page reload.  Everything else is dropped as before.
    """
    payload: dict[str, Any] = {
        "type": "status",
        "stage": str(event.get("stage") or "run")[:80],
        "status": str(event.get("status") or "running")[:40],
    }
    payload.update(bounded_timing_metrics(event))
    details = safe_error_details(event.get("error_details"))
    if details:
        payload["error_details"] = details
    return payload


class _RedisRunStream:
    """Shared read/cancel/SSE behavior for persisted Redis-backed run records."""

    broker: RedisChatRunBroker

    def _stream(self, run_id: str, *, answer_key: str = "answer") -> Iterator[str]:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        yield format_sse({"type": "status", "status": run["status"], "run_id": run_id})
        cursor = "0-0"
        while True:
            events, cursor = self.broker.events(run_id, cursor)
            for event in events:
                yield format_sse(event)
                if event.get("type") == "done":
                    return
            run = self.get(run_id)
            if not run:
                return
            if run.get("status") in _TERMINAL_STATUSES:
                done = {"type": "done", "status": run["status"]}
                if answer_key and run.get(answer_key):
                    done["answer"] = run[answer_key]
                yield format_sse(done)
                return
            yield ": keep-alive\\n\\n"


class RedisResearchRunManager(_RedisRunStream):
    """HTTP-side coordinator for deep-research runs."""

    def __init__(self, agent, broker: RedisResearchRunBroker, *, persistent: bool = True) -> None:
        self.agent = agent
        self.broker = broker
        from research_projects import ResearchProjects
        self.projects = ResearchProjects(agent.sessions.db_path)
        self.persistent = persistent

    def flush_outbox(self):
        for job in self.projects.outbox():
            project = self.projects.get(job["project_id"])
            if project and project["execution_state"] == "active":
                self.broker.enqueue(job["project_id"], project["origin_session_id"], "", project["scope"], job_id=job["job_id"])
                self.projects.mark_sent(job["job_id"])

    @staticmethod
    def project_run(project):
        state = project["execution_state"]
        status = {"active": "running", "stopped": "cancelled", "paused": "paused",
                  "waiting": "waiting", "needs_input": "needs_input", "budget_exhausted": "partial_failed"}.get(state)
        if not status:
            status = "completed" if project["end_reason"] == "fulfilled" else "partial_failed"
        return {"run_id": project["project_id"], "project_id": project["project_id"], "thread_id": project["origin_session_id"],
                "query": project["question"], "status": status, "final_answer": project["report"],
                "evidence": project["evidence"], "plan": {"completion_criteria": project["criteria"]},
                "created_at": project["created_at"], "updated_at": project["updated_at"],
                "trace": {"usage": {"calls": project["budget"]["calls"], "total": project["budget"]["tokens"]}},
                "research": project}

    def sync_project(self, project):
        run = self.project_run(project)
        self.agent.sessions.update_research_run(run["run_id"], status=run["status"],
            final_answer=run["final_answer"], evidence=run["evidence"], plan=run["plan"], trace=run["trace"])
        return run

    def control(self, project_id, action, *, message="", event_id=None):
        project = self.projects.control(project_id, action, message=message, event_id=event_id)
        self.sync_project(project)
        self.flush_outbox()
        self.broker.publish(project_id, {"type": "status", "status": self.project_run(project)["status"],
            "stage": "research_" + action, "goal_version": project["goal_version"]})
        return project

    def start(
        self, session_id: str, query: str, scope: str = "both", *, resume: bool = False,
    ) -> dict[str, Any]:
        if not self.agent.sessions.get(session_id):
            raise KeyError(session_id)
        if scope not in {"both", "local", "public"}:
            raise ValueError("invalid research scope")
        if self.persistent:
            if resume:
                projects = self.projects.list(session_id)
                if projects:
                    project = self.control(projects[0]["project_id"], "resume")
                    return self.project_run(project)
                # Older finite runs remain resumable through their old executor.
            else:
                if not query.strip():
                    raise ValueError("research query is blank")
                run = self.agent.sessions.create_research_run(session_id, query, status="queued")
                project = self.projects.create(run["run_id"], session_id, query, scope)
                self.flush_outbox()
                return self.sync_project(project)
        if resume:
            run = self.agent.sessions.get_latest_research_run(session_id)
            if run is None:
                raise ValueError("当前会话没有可继续的深度研究")
            if run.get("status") == "completed":
                return run
            run = self.agent.sessions.update_research_run(str(run["run_id"]), status="queued") or run
            query = str(run.get("query") or "")
        else:
            if not query.strip():
                raise ValueError("research query is blank")
            run = self.agent.sessions.create_research_run(session_id, query, status="queued")
        run_id = str(run["run_id"])
        try:
            self.broker.enqueue(run_id, session_id, query, scope, resume=resume)
            self.broker.publish(run_id, {"type": "status", "status": "queued", "run_id": run_id})
        except QueueUnavailableError:
            self.agent.sessions.update_research_run(run_id, status="failed")
            raise
        self.agent.sessions.add_run_event(
            session_id, run_id, "research", "api", "queue", "queued",
            summary="API research run queued for a durable worker",
            event_type="status",
            metadata=execution_metadata(
                "research",
                runner="redis-worker",
                model=str(self.agent.model),
                scope=scope,
                toolset="research-resume" if resume else "research-bounded",
            ),
        )
        return self.get(run_id) or run

    def get(self, run_id: str) -> dict[str, Any] | None:
        project = self.projects.get(run_id)
        if project:
            return self.project_run(project)
        return self.agent.sessions.get_research_run(run_id)

    def list_for_session(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        runs = self.agent.sessions.list_research_runs(session_id, limit=limit)
        return [self.get(str(run["run_id"])) or run for run in runs]

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        if self.projects.get(run_id):
            return self.project_run(self.control(run_id, "stop"))
        run = self.get(run_id)
        if not run or run.get("status") in _TERMINAL_STATUSES:
            return run
        self.broker.request_cancel(run_id)
        updated = self.agent.sessions.update_research_run(run_id, status="cancelling")
        if updated:
            self.broker.publish(run_id, {"type": "status", "status": "cancelling", "run_id": run_id})
            self.agent.sessions.add_run_event(
                updated["thread_id"], run_id, "research", "api", "cancel", "cancelling",
                summary="Cancellation requested by API client",
            )
        return updated

    def cancel_for_session(self, session_id: str) -> None:
        for run in self.list_for_session(session_id, limit=100):
            if self.projects.get(str(run["run_id"])):
                continue  # Research questions outlive their originating conversation.
            if run.get("status") not in _TERMINAL_STATUSES:
                self.cancel(str(run["run_id"]))

    def stream(self, run_id: str) -> Iterator[str]:
        if self.projects.get(run_id):
            return self._project_stream(run_id)
        return self._stream(run_id, answer_key="final_answer")

    def _project_stream(self, run_id):
        cursor = "0-0"
        while True:
            project = self.projects.get(run_id)
            run = self.project_run(project)
            events, cursor = self.broker.events(run_id, cursor)
            for event in events:
                if event.get("goal_version") == project["goal_version"] and event.get("type") != "done":
                    yield format_sse(event)
            yield format_sse({"type": "status", "status": run["status"], "stage": "research"})
            if project["execution_state"] != "active":
                yield format_sse({"type": "done", "status": run["status"], "answer": run["final_answer"]})
                return


class RedisResearchRunWorker(_RedisRunWorker):
    """Durably execute one persisted deep-research run at a time."""

    def __init__(
        self, agent, broker: RedisResearchRunBroker, *, consumer: str | None = None,
        claim_idle_ms: int = 120_000,
    ) -> None:
        self.agent = agent
        self.project_manager = RedisResearchRunManager(agent, broker)
        super().__init__(
            broker,
            consumer=consumer or f"research-worker-{os.getpid()}-{uuid.uuid4().hex[:8]}",
            claim_idle_ms=claim_idle_ms,
        )

    def _process(self, job: QueuedResearchRun) -> None:
        if job.job_id:
            from autonomous_research import AutonomousResearch
            manager = self.project_manager
            task = AutonomousResearch(self.agent, manager.projects).execute(
                job.job_id, getattr(self._execution_local, "cancel_event", None), check=self._check_execution)
            self._check_execution()
            project = manager.projects.get(job.run_id)
            if project:
                current = manager.sync_project(project)
                if task:
                    self.agent.sessions.add_run_event(project["origin_session_id"], job.run_id, "research",
                        task["role"], task["role"], current["status"], summary="研究任务状态已更新", event_type="status")
                self._publish(job.run_id, {"type": "status", "status": current["status"],
                    "stage": task["role"] if task else "research", "goal_version": project["goal_version"]})
            manager.flush_outbox()
            return
        run = self.agent.sessions.get_research_run(job.run_id)
        if not run or run.get("thread_id") != job.session_id or run.get("status") in _TERMINAL_STATUSES:
            return
        if self.broker.cancel_requested(job.run_id):
            self._finish_cancelled(job)
            return
        self.agent.sessions.update_research_run(job.run_id, status="running")
        self._publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "research", "api_worker", "run", "running",
            summary="Durable API worker started research run", event_type="status",
        )
        cancel_event, stop_monitor, monitor = self._cancel_monitor(job.run_id)

        def consume_steers(stage: str) -> list[dict[str, Any]]:
            return self.agent.sessions.consume_run_steers(
                job.run_id, job.session_id, stage=stage,
            )

        def on_steer(_stage: str, _count: int) -> None:
            self._publish(job.run_id, {
                "type": "status", "status": "running", "stage": "steer_consumed",
                "run_id": job.run_id,
            })

        try:
            self.agent.research(
                job.query, scope=job.scope, session_id=job.session_id, run_id=job.run_id,
                resume=job.resume, cancel_event=cancel_event,
                on_progress=lambda event: self._publish(job.run_id, _safe_status_event(event)),
                steer_provider=consume_steers,
                on_steer=on_steer,
            )
            self._check_execution()
            current = self.agent.sessions.get_research_run(job.run_id) or run
            result_status = str(current.get("status") or "failed")
            if result_status not in _TERMINAL_STATUSES:
                result_status = "cancelled" if cancel_event.is_set() else "failed"
                self.agent.sessions.update_research_run(job.run_id, status=result_status)
            self._publish(job.run_id, {
                "type": "done", "status": result_status,
                "answer": str(current.get("final_answer") or ""),
            })
        except QueueUnavailableError:
            raise
        except Exception as exc:
            self._check_execution()
            self.agent.sessions.update_research_run(job.run_id, status="failed")
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "research", "api_worker", "run", "failed",
                summary="Durable API worker failed", error_type=type(exc).__name__, event_type="error",
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "research", "api_worker", "run", "failed",
                summary="深度研究运行已结束", event_type="done",
            )
            self._publish(job.run_id, {"type": "error", "error_type": type(exc).__name__})
            self._publish(job.run_id, {"type": "done", "status": "failed"})
        finally:
            stop_monitor.set()
            monitor.join(timeout=0.2)

    def maintain(self):
        # Detect a concrete paper becoming available without waking an LLM to
        # poll. Explicit material events use the same idempotent control path.
        waiting = [p for p in self.project_manager.projects.list() if p["execution_state"] == "waiting"]
        if waiting:
            available = {"paper:" + str(p["paper_id"]) for p in self.agent._paper_store.list_papers()}
            for project in waiting:
                for material in set(project["waiting_for"]) & available:
                    self.project_manager.control(project["project_id"], "material", message=material,
                        event_id=f"material:{project['project_id']}:{project['goal_version']}:{material}")
        self.project_manager.flush_outbox()

    def _finish_cancelled(self, job: QueuedResearchRun) -> None:
        if job.job_id:
            self.project_manager.control(job.run_id, "stop")
            return
        self.agent.sessions.update_research_run(job.run_id, status="cancelled", final_answer="")
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "research", "api_worker", "run", "cancelled",
            summary="Queued API research run was cancelled before execution", event_type="done",
        )
        self._publish(job.run_id, {"type": "done", "status": "cancelled"})
        self.broker.clear_cancel(job.run_id)


class RedisDailyRunManager(_RedisRunStream):
    """HTTP-side coordinator for daily and ad-hoc paper-discovery runs."""

    def __init__(self, scheduler, broker: RedisDailyRunBroker) -> None:
        self.scheduler = scheduler
        self.broker = broker

    def start(self, kind: str, keyword: str | None = None) -> dict[str, Any]:
        if kind not in {"daily", "retry", "search", "resume"}:
            raise ValueError("invalid daily run kind")
        run_kind = "daily" if kind == "resume" else kind
        if kind == "resume":
            run = self.scheduler.get_latest_resumable_run("daily")
            if not run:
                raise ValueError("no resumable daily run")
            run_id = str(run["run_id"])
            self.scheduler.update_daily_run(run_id, status="queued")
            run = self.get(run_id) or run
            try:
                self.broker.enqueue(run_id, run_kind)
                self.broker.publish(run_id, {"type": "status", "status": "queued", "run_id": run_id})
            except QueueUnavailableError:
                self.scheduler.update_daily_run(run_id, status="failed", error_text="QueueUnavailableError")
                raise
            self.scheduler.add_daily_agent_event(
                run_id, "orchestrator", "queued", "",
                event_type="status",
                metadata=execution_metadata(
                    "daily", runner="redis-worker", toolset="daily-curated",
                ),
            )
            return run
        if kind == "search":
            error = self.scheduler.validate_keyword(keyword or "")
            if error:
                raise ValueError(error)
            keywords = [str(keyword).strip()]
        else:
            keywords = self.scheduler.prepare_daily_keywords(retry=kind == "retry")
        run = self.scheduler.create_daily_run(run_kind, keywords, status="queued")
        run_id = str(run["run_id"])
        try:
            self.broker.enqueue(run_id, run_kind)
            self.broker.publish(run_id, {"type": "status", "status": "queued", "run_id": run_id})
        except QueueUnavailableError:
            self.scheduler.update_daily_run(run_id, status="failed", error_text="QueueUnavailableError")
            raise
        self.scheduler.add_daily_agent_event(
            run_id, "orchestrator", "queued", "",
            event_type="status",
            metadata=execution_metadata(
                "daily", runner="redis-worker", toolset="daily-curated",
            ),
        )
        return self.get(run_id) or run

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.scheduler.get_daily_run(run_id)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.scheduler.list_daily_runs(limit=limit)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        run = self.get(run_id)
        if not run or run.get("status") in _TERMINAL_STATUSES:
            return run
        self.broker.request_cancel(run_id)
        updated = self.scheduler.update_daily_run(run_id, status="cancelling")
        if updated:
            self.broker.publish(run_id, {"type": "status", "status": "cancelling", "run_id": run_id})
            self.scheduler.add_daily_agent_event(
                run_id, "orchestrator", "cancelling", "", event_type="status",
            )
        return updated

    def stream(self, run_id: str) -> Iterator[str]:
        return self._stream(run_id, answer_key="")


class RedisDailyRunWorker(_RedisRunWorker):
    """Execute daily discovery work from the shared low-priority Redis stream."""

    def __init__(
        self, orchestrator, scheduler, broker: RedisDailyRunBroker, *, consumer: str | None = None,
        claim_idle_ms: int = 120_000, paper_store=None,
    ) -> None:
        self.orchestrator = orchestrator
        self.scheduler = scheduler
        self.paper_store = paper_store
        super().__init__(
            broker,
            consumer=consumer or f"daily-worker-{os.getpid()}-{uuid.uuid4().hex[:8]}",
            claim_idle_ms=claim_idle_ms,
        )

    def _process(self, job: QueuedDailyRun) -> None:
        run = self.scheduler.get_daily_run(job.run_id)
        if not run or run.get("kind") != job.kind or run.get("status") in _TERMINAL_STATUSES:
            return
        if self.broker.cancel_requested(job.run_id):
            self._finish_cancelled(job)
            return
        self.scheduler.update_daily_run(job.run_id, status="running")
        self._publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.scheduler.add_daily_agent_event(
            job.run_id, "orchestrator", "running", "", event_type="status",
        )
        cancel_event, stop_monitor, monitor = self._cancel_monitor(job.run_id)
        try:
            result = self.orchestrator.run(
                job.kind, run_id=job.run_id, paper_store=self.paper_store, cancel_event=cancel_event,
                on_progress=lambda event: self._publish(job.run_id, _safe_status_event(event)),
            )
            self._check_execution()
            self._publish(job.run_id, {"type": "done", "status": result.status})
        except QueueUnavailableError:
            raise
        except Exception as exc:
            self._check_execution()
            self.scheduler.update_daily_run(job.run_id, status="failed", error_text=type(exc).__name__)
            self.scheduler.add_daily_agent_event(
                job.run_id, "orchestrator", "failed", "",
                event_type="error", metadata=None,
            )
            self.scheduler.add_daily_agent_event(
                job.run_id, "orchestrator", "failed", "", event_type="done",
            )
            self._publish(job.run_id, {"type": "error", "error_type": type(exc).__name__})
            self._publish(job.run_id, {"type": "done", "status": "failed"})
        finally:
            stop_monitor.set()
            monitor.join(timeout=0.2)

    def _finish_cancelled(self, job: QueuedDailyRun) -> None:
        self.scheduler.update_daily_run(job.run_id, status="cancelled")
        self.scheduler.add_daily_agent_event(
            job.run_id, "orchestrator", "cancelled", "", event_type="done",
        )
        self._publish(job.run_id, {"type": "done", "status": "cancelled"})
        self.broker.clear_cancel(job.run_id)

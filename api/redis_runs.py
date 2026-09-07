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
from run_contract import execution_metadata


_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "partial_failed"}


class QueueUnavailableError(RuntimeError):
    """The API could not durably enqueue a run."""


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


@dataclass(frozen=True)
class QueuedResearchRun:
    message_id: str
    run_id: str
    session_id: str
    query: str
    scope: str
    resume: bool = False


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
    ) -> str:
        try:
            item_id = self._redis.xadd(
                self.queue_key,
                {
                    "run_id": run_id, "session_id": session_id, "query": query,
                    "scope": scope, "resume": "1" if resume else "0",
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
                yield format_sse({
                    "type": "done", "status": run["status"], "answer": run.get("answer", ""),
                })
                return
            yield ": keep-alive\n\n"

class _RedisRunWorker:
    """Shared Redis Streams polling and cooperative-cancellation plumbing."""

    def __init__(self, broker: RedisChatRunBroker, *, consumer: str, claim_idle_ms: int) -> None:
        self.broker = broker
        self.consumer = consumer
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
            self.broker.heartbeat(self.consumer)
            self.run_once(block_ms=5_000)

    def _cancel_monitor(self, run_id: str) -> tuple[threading.Event, threading.Event, threading.Thread]:
        cancel_event = threading.Event()
        stop_monitor = threading.Event()

        def monitor_cancel() -> None:
            while not stop_monitor.wait(0.1):
                if self.broker.cancel_requested(run_id):
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
        self.broker.publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "chat", "api_worker", "run", "running",
            summary="Durable API worker started chat run",
            event_type="status",
        )

        cancel_event, stop_monitor, monitor = self._cancel_monitor(job.run_id)

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
                    self.broker.publish(job.run_id, {
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
            self.broker.publish(job.run_id, {"type": "done", "status": result_status, "answer": answer})
        except Exception as exc:
            self.agent.sessions.update_chat_run(
                job.run_id, status="failed", answer="", error_type=type(exc).__name__,
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "chat", "api_worker", "run", "failed",
                summary="Durable API worker failed", error_type=type(exc).__name__, event_type="error",
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "chat", "api_worker", "run", "failed",
                summary="聊天运行已结束", event_type="done",
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
            summary="Queued API chat run was cancelled before execution", event_type="done",
        )
        self.broker.publish(job.run_id, {"type": "done", "status": "cancelled", "answer": ""})
        self.broker.clear_cancel(job.run_id)


def _safe_status_event(event: dict[str, Any]) -> dict[str, str]:
    """Keep only de-identified stage/status fields from existing progress traces."""
    return {
        "type": "status",
        "stage": str(event.get("stage") or "run")[:80],
        "status": str(event.get("status") or "running")[:40],
    }


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

    def __init__(self, agent, broker: RedisResearchRunBroker) -> None:
        self.agent = agent
        self.broker = broker

    def start(
        self, session_id: str, query: str, scope: str = "both", *, resume: bool = False,
    ) -> dict[str, Any]:
        if not self.agent.sessions.get(session_id):
            raise KeyError(session_id)
        if scope not in {"both", "local", "public"}:
            raise ValueError("invalid research scope")
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
        return self.agent.sessions.get_research_run(run_id)

    def list_for_session(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.agent.sessions.list_research_runs(session_id, limit=limit)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
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
            if run.get("status") not in _TERMINAL_STATUSES:
                self.cancel(str(run["run_id"]))

    def stream(self, run_id: str) -> Iterator[str]:
        return self._stream(run_id, answer_key="final_answer")


class RedisResearchRunWorker(_RedisRunWorker):
    """Durably execute one persisted deep-research run at a time."""

    def __init__(
        self, agent, broker: RedisResearchRunBroker, *, consumer: str | None = None,
        claim_idle_ms: int = 120_000,
    ) -> None:
        self.agent = agent
        super().__init__(
            broker,
            consumer=consumer or f"research-worker-{os.getpid()}-{uuid.uuid4().hex[:8]}",
            claim_idle_ms=claim_idle_ms,
        )

    def _process(self, job: QueuedResearchRun) -> None:
        run = self.agent.sessions.get_research_run(job.run_id)
        if not run or run.get("thread_id") != job.session_id or run.get("status") in _TERMINAL_STATUSES:
            return
        if self.broker.cancel_requested(job.run_id):
            self._finish_cancelled(job)
            return
        self.agent.sessions.update_research_run(job.run_id, status="running")
        self.broker.publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "research", "api_worker", "run", "running",
            summary="Durable API worker started research run", event_type="status",
        )
        cancel_event, stop_monitor, monitor = self._cancel_monitor(job.run_id)
        try:
            self.agent.research(
                job.query, scope=job.scope, session_id=job.session_id, run_id=job.run_id,
                resume=job.resume, cancel_event=cancel_event,
                on_progress=lambda event: self.broker.publish(job.run_id, _safe_status_event(event)),
            )
            current = self.agent.sessions.get_research_run(job.run_id) or run
            result_status = str(current.get("status") or "failed")
            if result_status not in _TERMINAL_STATUSES:
                result_status = "cancelled" if cancel_event.is_set() else "failed"
                self.agent.sessions.update_research_run(job.run_id, status=result_status)
            self.broker.publish(job.run_id, {
                "type": "done", "status": result_status,
                "answer": str(current.get("final_answer") or ""),
            })
        except Exception as exc:
            self.agent.sessions.update_research_run(job.run_id, status="failed")
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "research", "api_worker", "run", "failed",
                summary="Durable API worker failed", error_type=type(exc).__name__, event_type="error",
            )
            self.agent.sessions.add_run_event(
                job.session_id, job.run_id, "research", "api_worker", "run", "failed",
                summary="深度研究运行已结束", event_type="done",
            )
            self.broker.publish(job.run_id, {"type": "error", "error_type": type(exc).__name__})
            self.broker.publish(job.run_id, {"type": "done", "status": "failed"})
        finally:
            stop_monitor.set()
            monitor.join(timeout=0.2)
            self.broker.clear_cancel(job.run_id)

    def _finish_cancelled(self, job: QueuedResearchRun) -> None:
        self.agent.sessions.update_research_run(job.run_id, status="cancelled", final_answer="")
        self.agent.sessions.add_run_event(
            job.session_id, job.run_id, "research", "api_worker", "run", "cancelled",
            summary="Queued API research run was cancelled before execution", event_type="done",
        )
        self.broker.publish(job.run_id, {"type": "done", "status": "cancelled"})
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
        self.broker.publish(job.run_id, {"type": "status", "status": "running", "run_id": job.run_id})
        self.scheduler.add_daily_agent_event(
            job.run_id, "orchestrator", "running", "", event_type="status",
        )
        cancel_event, stop_monitor, monitor = self._cancel_monitor(job.run_id)
        try:
            result = self.orchestrator.run(
                job.kind, run_id=job.run_id, paper_store=self.paper_store, cancel_event=cancel_event,
                on_progress=lambda event: self.broker.publish(job.run_id, _safe_status_event(event)),
            )
            self.broker.publish(job.run_id, {"type": "done", "status": result.status})
        except Exception as exc:
            self.scheduler.update_daily_run(job.run_id, status="failed", error_text=type(exc).__name__)
            self.scheduler.add_daily_agent_event(
                job.run_id, "orchestrator", "failed", "",
                event_type="error", metadata=None,
            )
            self.scheduler.add_daily_agent_event(
                job.run_id, "orchestrator", "failed", "", event_type="done",
            )
            self.broker.publish(job.run_id, {"type": "error", "error_type": type(exc).__name__})
            self.broker.publish(job.run_id, {"type": "done", "status": "failed"})
        finally:
            stop_monitor.set()
            monitor.join(timeout=0.2)
            self.broker.clear_cancel(job.run_id)

    def _finish_cancelled(self, job: QueuedDailyRun) -> None:
        self.scheduler.update_daily_run(job.run_id, status="cancelled")
        self.scheduler.add_daily_agent_event(
            job.run_id, "orchestrator", "cancelled", "", event_type="done",
        )
        self.broker.publish(job.run_id, {"type": "done", "status": "cancelled"})
        self.broker.clear_cancel(job.run_id)

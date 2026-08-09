"""In-process chat-run coordinator used by the first FastAPI delivery.

The coordinator deliberately has one process-local responsibility: bridge a
request's cancellation token and token stream to the existing ResearchAgent.
Run metadata and final answers remain in SessionStore, so status survives an
HTTP reconnect.  Moving work to a durable cross-process queue is a later
deployment step, not something FastAPI BackgroundTasks can provide.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class _ActiveChatRun:
    run_id: str
    session_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def append(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)

    def after(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        with self.lock:
            next_cursor = len(self.events)
            return list(self.events[cursor:]), next_cursor


class ChatRunManager:
    """Create, stream, inspect and cooperatively cancel interactive chat runs."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveChatRun] = {}

    def start(self, session_id: str, message: str) -> dict[str, Any]:
        if not self.agent.sessions.get(session_id):
            raise KeyError(session_id)
        run = self.agent.sessions.create_chat_run(session_id, self.agent.model)
        active = _ActiveChatRun(run_id=str(run["run_id"]), session_id=session_id)
        with self._lock:
            self._active[active.run_id] = active
        active.append({"type": "status", "status": "running"})
        worker = threading.Thread(
            target=self._run,
            args=(active, message),
            name=f"api-chat-{active.run_id[-8:]}",
            daemon=True,
        )
        worker.start()
        return self.get(active.run_id) or run

    def _run(self, active: _ActiveChatRun, message: str) -> None:
        def on_token(token: str) -> None:
            if token and not active.cancel_event.is_set():
                active.append({"type": "token", "text": token})

        try:
            answer = self.agent.step(
                message,
                session_id=active.session_id,
                run_id=active.run_id,
                cancel_event=active.cancel_event,
                on_token=on_token,
            )
            trace = self.agent.get_last_trace(active.session_id) or {}
            status = self._status_from_trace(trace, active.cancel_event)
            stored = self.get(active.run_id)
            if stored and (
                stored.get("status") not in _TERMINAL_STATUSES
                or (status == "cancelled" and stored.get("status") != "cancelled")
            ):
                self.agent.sessions.update_chat_run(
                    active.run_id, status=status, answer=answer,
                )
            active.append({"type": "done", "status": status, "answer": answer})
        except Exception as exc:  # Keep one bad API request from killing the ASGI process.
            status = "failed"
            answer = ""
            self.agent.sessions.update_chat_run(
                active.run_id, status=status, answer=answer, error_type=type(exc).__name__,
            )
            self.agent.sessions.add_run_event(
                active.session_id, active.run_id, "chat", "api", "run", status,
                summary="API chat run failed", error_type=type(exc).__name__,
            )
            active.append({"type": "error", "error_type": type(exc).__name__})
            active.append({"type": "done", "status": status, "answer": answer})
        finally:
            active.done_event.set()

    @staticmethod
    def _status_from_trace(trace: dict[str, Any], cancel_event: threading.Event) -> str:
        if cancel_event.is_set() or trace.get("outcome") == "cancelled":
            return "cancelled"
        if trace.get("outcome") == "success":
            return "completed"
        return "failed"

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.agent.sessions.get_chat_run(run_id)

    def list_for_session(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.agent.sessions.list_chat_runs(session_id, limit=limit)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        run = self.get(run_id)
        if not run:
            return None
        if run.get("status") in _TERMINAL_STATUSES:
            return run
        with self._lock:
            active = self._active.get(run_id)
        if active is None:
            # A process-local worker cannot truthfully cancel a run created by
            # another process; the API exposes this as a conflict to callers.
            return None
        active.cancel_event.set()
        active.append({"type": "status", "status": "cancelling"})
        self.agent.sessions.update_chat_run(run_id, status="cancelling")
        self.agent.sessions.add_run_event(
            active.session_id, run_id, "chat", "api", "cancel", "cancelling",
            summary="Cancellation requested by API client",
        )
        return self.get(run_id)

    def cancel_for_session(self, session_id: str) -> None:
        """Signal active work before a session's durable state is deleted."""
        with self._lock:
            active_runs = [run for run in self._active.values() if run.session_id == session_id]
        for active in active_runs:
            active.cancel_event.set()
            active.append({"type": "status", "status": "cancelling"})

    def stream(self, run_id: str) -> Iterator[str]:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        yield self._sse({"type": "status", "status": run["status"], "run_id": run_id})

        with self._lock:
            active = self._active.get(run_id)
        if active is None:
            yield self._sse({"type": "done", "status": run["status"], "answer": run.get("answer", "")})
            return

        cursor = 0
        while True:
            events, cursor = active.after(cursor)
            for event in events:
                yield self._sse(event)
            if active.done_event.wait(timeout=0.25):
                events, cursor = active.after(cursor)
                for event in events:
                    yield self._sse(event)
                return
            yield ": keep-alive\n\n"

    @staticmethod
    def _sse(event: dict[str, Any]) -> str:
        event_type = str(event.get("type") or "status")
        if event_type not in {"status", "token", "tool", "done", "error"}:
            event_type = "status"
            event = {"type": "status", "status": "running"}
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {data}\n\n"

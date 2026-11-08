"""Temporary React/API server used only by the browser E2E test.

It deliberately avoids external model services and user runtime data.  When
``E2E_REDIS_URL`` is supplied (as it is in CI), it also starts a test-only
Redis-backed worker so the browser exercises the durable run boundary.  Without
that variable, it uses the in-process manager for a lightweight local fixture.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn

from api.app import create_app, mount_react_frontend
from api.run_dispatcher import RunDispatcher
from api.redis_runs import RedisChatRunBroker, RedisChatRunManager, RedisChatRunWorker
from config import Config
from llm_client import LLMRequestFailedError
from session_store import SessionStore


class _E2EAgent:
    """Minimal deterministic agent surface required by the React workbench."""

    model = "e2e-fake-model"
    paper_store = None

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.sessions = SessionStore(cfg.checkpoint_db)
        self._thread_id = str(self.sessions.create("浏览器 E2E 会话")["thread_id"])
        self._traces: dict[str, dict] = {}
        self.calls: list[dict[str, str]] = []
        self._histories: dict[str, list[dict]] = {}
        self.profile = SimpleNamespace(read=lambda: "")
        self.runtime_status = {"daily_ready": True, "daily_progress": "", "daily_message": ""}

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def create_session(self, title: str | None = None) -> dict:
        session = self.sessions.create(title)
        self._thread_id = str(session["thread_id"])
        return session

    def list_sessions(self) -> list[dict]:
        return self.sessions.list()

    def delete_session(self, session_id: str) -> bool:
        deleted = self.sessions.delete(session_id)
        if deleted and session_id == self._thread_id:
            remaining = self.sessions.list()
            if remaining:
                self._thread_id = str(remaining[0]["thread_id"])
        return deleted

    def get_history(self, session_id: str) -> list[dict]:
        return list(self._histories.get(session_id, []))

    @staticmethod
    def get_usage(_session_id: str) -> dict[str, int | float]:
        return {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cost": 0.0, "context_limit": 131072}

    @staticmethod
    def get_retry_input(_session_id: str):
        return None

    def get_last_trace(self, session_id: str) -> dict:
        return self._traces.get(session_id, {})

    def step(self, message: str, *, session_id: str, cancel_event, on_token, **_kwargs) -> str:
        self.calls.append({"message": message, "session_id": session_id})
        history = self._histories.setdefault(session_id, [])
        history.append({"role": "user", "content": message})
        if message == "__error__":
            raise RuntimeError("浏览器 E2E 受控失败")
        if message == "__http401__":
            # Exercise the real broker/SSE/API diagnostic path without any
            # external request. Unknown fields must not reach persisted runs.
            error = LLMRequestFailedError("模型 API 返回 401：受控认证失败")
            error.error_details = {
                "code": "LLM_HTTP_ERROR", "phase": "response_headers",
                "exception_type": "LLMRequestFailedError", "cause_type": "HTTPError",
                "http_status": 401, "elapsed_ms": 512.4, "deadline_ms": 45000,
                "queue_wait_ms": 0, "attempts": 1, "retries_used": 0,
                "request_id": "req-browser-fixture-401",
                "response_body": "E2E_PRIVATE_RESPONSE_DO_NOT_RENDER",
                "api_key": "E2E_PRIVATE_KEY_DO_NOT_RENDER",
            }
            raise error
        if message == "__slow__":
            on_token("旧会话片段")
            cancel_event.wait(timeout=4)
            if cancel_event.is_set():
                self._traces[session_id] = {"outcome": "cancelled"}
                return "已取消"
            self._traces[session_id] = {"outcome": "success"}
            history.append({"role": "assistant", "content": "慢请求完成"})
            return "慢请求完成"

        prefix = "来自 FastAPI："
        on_token(prefix)
        on_token(message)
        self._traces[session_id] = {"outcome": "success"}
        history.append({"role": "assistant", "content": f"{prefix}{message}"})
        return f"{prefix}{message}"


def create_e2e_app(data_dir: str, redis_url: str = ""):
    """Build an ASGI app serving the real React client against a fake agent."""
    cfg = Config.load({"data_dir": data_dir, "rag_enabled": False})
    agent = _E2EAgent(cfg)
    manager = None
    if redis_url:
        broker = RedisChatRunBroker(redis_url, prefix=f"research-agent-e2e-{uuid.uuid4().hex}")
        broker.ping()
        manager = RedisChatRunManager(agent, broker)
        worker = RedisChatRunWorker(agent, broker, consumer="browser-e2e-worker", claim_idle_ms=100)
        dispatcher = RunDispatcher({"chat": (worker, 2)})
        worker_thread = threading.Thread(target=dispatcher.run_forever, name="browser-e2e-worker", daemon=True)
        worker_thread.start()
    app = create_app(agent=agent, title="科研助手 API 浏览器 E2E", chat_run_manager=manager)
    app.state.e2e_uses_redis = bool(redis_url)

    @app.post("/__e2e__/documents", include_in_schema=False)
    def e2e_create_document(payload: dict) -> dict:
        # This fixture-only route seeds the same isolated store read by the
        # production document API; no model or user runtime is involved.
        return app.state.research_document_store.save(payload["title"], payload["content"])

    @app.get("/__e2e__/state", include_in_schema=False)
    def e2e_state() -> dict:
        runs = []
        for session in agent.list_sessions():
            for run in agent.sessions.list_chat_runs(str(session["thread_id"]), limit=100):
                runs.append({
                    "run_id": run["run_id"], "session_id": run["thread_id"],
                    "status": run["status"], "answer": run.get("answer", ""),
                })
        return {"calls": list(agent.calls), "runs": runs, "uses_redis": app.state.e2e_uses_redis}

    dist = ROOT / "frontend" / "dist"
    if not mount_react_frontend(app, dist):
        raise RuntimeError("请先执行: cd frontend && npm run build")
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--redis-url", default=os.getenv("E2E_REDIS_URL", ""))
    args = parser.parse_args()
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    app = create_e2e_app(args.data_dir, args.redis_url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

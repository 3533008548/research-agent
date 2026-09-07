"""Temporary mounted-UI server used only by the browser E2E test.

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

import gradio as gr
import uvicorn

from api.app import create_app
from api.redis_runs import RedisChatRunBroker, RedisChatRunManager, RedisChatRunWorker
from config import Config
from session_store import SessionStore
from web_ui import UI_CSS, UI_JS, UI_THEME, build_ui


class _E2EAgent:
    """Minimal deterministic agent surface required by the mounted Gradio UI."""

    model = "e2e-fake-model"
    paper_store = None

    def __init__(self, checkpoint_db: str) -> None:
        self.sessions = SessionStore(checkpoint_db)
        self._thread_id = str(self.sessions.create("浏览器 E2E 会话")["thread_id"])
        self._traces: dict[str, dict] = {}
        self.calls: list[dict[str, str]] = []
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

    def get_history(self, _session_id: str) -> list[dict]:
        return []

    @staticmethod
    def get_usage(_session_id: str) -> dict[str, int | float]:
        return {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cost": 0.0}

    @staticmethod
    def get_retry_input(_session_id: str):
        return None

    def get_last_trace(self, session_id: str) -> dict:
        return self._traces.get(session_id, {})

    def step(self, message: str, *, session_id: str, cancel_event, on_token, **_kwargs) -> str:
        self.calls.append({"message": message, "session_id": session_id})
        if message == "__slow__":
            on_token("旧会话片段")
            cancel_event.wait(timeout=4)
            if cancel_event.is_set():
                self._traces[session_id] = {"outcome": "cancelled"}
                return "已取消"
            self._traces[session_id] = {"outcome": "success"}
            return "慢请求完成"

        prefix = "来自 FastAPI："
        on_token(prefix)
        on_token(message)
        self._traces[session_id] = {"outcome": "success"}
        return f"{prefix}{message}"


def create_e2e_app(data_dir: str, public_url: str, redis_url: str = ""):
    """Build an ASGI app whose mounted Gradio client is forced through HTTP."""
    os.environ["API_RUN_CLIENT_ENABLED"] = "1"
    os.environ["INTERNAL_API_URL"] = public_url
    cfg = Config.load({"data_dir": data_dir, "rag_enabled": False})
    agent = _E2EAgent(cfg.checkpoint_db)
    manager = None
    if redis_url:
        broker = RedisChatRunBroker(redis_url, prefix=f"research-agent-e2e-{uuid.uuid4().hex}")
        broker.ping()
        manager = RedisChatRunManager(agent, broker)
        worker = RedisChatRunWorker(agent, broker, consumer="browser-e2e-worker", claim_idle_ms=100)
        worker_thread = threading.Thread(target=worker.run_forever, name="browser-e2e-worker", daemon=True)
        worker_thread.start()
    app = create_app(agent=agent, title="科研助手 API 浏览器 E2E", chat_run_manager=manager)
    app.state.e2e_uses_redis = bool(redis_url)

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

    ui = build_ui(cfg=cfg, agent=agent, launch=False)
    return gr.mount_gradio_app(app, ui, path="/", theme=UI_THEME, css=UI_CSS, js=UI_JS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--redis-url", default=os.getenv("E2E_REDIS_URL", ""))
    args = parser.parse_args()
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    app = create_e2e_app(args.data_dir, f"http://127.0.0.1:{args.port}", args.redis_url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

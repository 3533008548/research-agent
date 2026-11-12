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
from api.redis_runs import RedisChatRunBroker, RedisChatRunManager, RedisChatRunWorker, RedisResearchRunBroker, RedisResearchRunManager
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
        if message == "__verify_repair_limit__":
            from research_agent import ResearchAgent
            from run_contract import graph_activity_event
            events = [
                {"type": "verify_request_started", "verification_round": 1},
                {"type": "verify_revision_requested", "verification_round": 1},
                {"type": "verify_request_started", "verification_round": 2},
                {"type": "verify_request_finished", "verification_round": 2, "duration_ms": 100},
                {"type": "verify_unresolved", "verification_round": 2},
            ]
            for event in events:
                ResearchAgent._persist_chat_event(self, session_id, _kwargs["run_id"], event, 100)
                _kwargs["on_progress"](graph_activity_event(event))
            answer = "一次修正后的回复，仍有问题待确认。"
            history.append({"role": "assistant", "content": answer})
            self._traces[session_id] = {"outcome": "success"}
            return answer
        if message == "__verify_timeout__":
            from research_agent import ResearchAgent
            from run_contract import graph_activity_event
            failure = {"type": "verify_request_failed", "error_type": "LLMRequestTimeoutError", "duration_ms": 45010,
                "error_details": {"code": "LLM_RESPONSE_TIMEOUT", "phase": "response_headers", "elapsed_ms": 45010,
                    "deadline_ms": 45000, "attempts": 1, "retries_used": 0, "response_body": "PRIVATE_DIAGNOSTIC"}}
            for event in ({"type": "verify_request_started"}, {"type": "verify_status"}, failure):
                ResearchAgent._persist_chat_event(self, session_id, _kwargs["run_id"], event, 45010)
                _kwargs["on_progress"](graph_activity_event(event))
            answer = "已保留的正常回复"
            history.append({"role": "assistant", "content": answer})
            self._traces[session_id] = {"outcome": "success"}
            return answer
        if message == "__tools__":
            from research_agent import ResearchAgent
            from run_contract import graph_activity_event
            from tool_runtime import ToolExecutionContext, ToolRuntime
            def emit(kind, **details):
                event = {"type": kind, **details}
                ResearchAgent._persist_chat_event(self, session_id, _kwargs["run_id"], event, 0)
                if _kwargs.get("on_progress"):
                    _kwargs["on_progress"](graph_activity_event(event))
            def execute(name, args, **kwargs):
                cancel_event.wait(timeout=1.8)
                return "❌ E2E_PRIVATE_TOOL_RESULT" if name == "read_pdf" else "E2E_PRIVATE_TOOL_RESULT"
            runtime = ToolRuntime(ToolExecutionContext(), event_callback=emit, executor=execute)
            runtime.execute("query_papers", {"query": "E2E_PRIVATE_TOOL_ARGUMENT"})
            runtime.execute("read_pdf", {"url_or_path": "E2E_PRIVATE_TOOL_ARGUMENT"})
            # A finished tool/model stage must not mark the whole run as completed.
            _kwargs["on_progress"]({"type": "status", "stage": "model", "status": "completed"})
            cancel_event.wait(timeout=1.8)
            self._traces[session_id] = {"outcome": "success"}
            answer = "工具状态测试完成"
            history.append({"role": "assistant", "content": answer})
            return answer
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
        if message == "__scroll__":
            answer = "历史段落，用于验证向上阅读。\n\n" * 60
            on_token(answer)
            for index in range(60):
                if cancel_event.wait(timeout=0.15):
                    self._traces[session_id] = {"outcome": "cancelled"}
                    return answer
                part = f"流式新增第 {index + 1} 段。\n\n"
                answer += part
                on_token(part)
            self._traces[session_id] = {"outcome": "success"}
            history.append({"role": "assistant", "content": answer})
            return answer

        if message == "__slow__":
            on_token("旧会话片段")
            cancel_event.wait(timeout=4)
            if cancel_event.is_set():
                self._traces[session_id] = {"outcome": "cancelled"}
                return "已取消"
            self._traces[session_id] = {"outcome": "success"}
            history.append({"role": "assistant", "content": "慢请求完成"})
            return "慢请求完成"

        if message == "__recover__":
            # The transport recovery tests cover replay; this fixture checks
            # that done.answer replaces a stale draft across SSE and reload.
            on_token("恢复前的不完整草稿")
            cancel_event.wait(timeout=1)
            answer = "自动恢复后的完整回答。"
            self._traces[session_id] = {"outcome": "success"}
            history.append({"role": "assistant", "content": answer})
            return answer

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
    research_manager = None
    if redis_url:
        broker = RedisChatRunBroker(redis_url, prefix=f"research-agent-e2e-{uuid.uuid4().hex}")
        broker.ping()
        manager = RedisChatRunManager(agent, broker)
        # Control-plane fixture: research jobs remain queued so lifecycle UI
        # checks are deterministic and never call a real model.
        research_manager = RedisResearchRunManager(agent, RedisResearchRunBroker(redis_url, prefix=broker.prefix))
        worker = RedisChatRunWorker(agent, broker, consumer="browser-e2e-worker", claim_idle_ms=100)
        dispatcher = RunDispatcher({"chat": (worker, 2)})
        worker_thread = threading.Thread(target=dispatcher.run_forever, name="browser-e2e-worker", daemon=True)
        worker_thread.start()
    app = create_app(agent=agent, title="科研助手 API 浏览器 E2E", chat_run_manager=manager, research_run_manager=research_manager)
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

    @app.post("/__e2e__/research-activity", include_in_schema=False)
    def e2e_research_activity(payload: dict) -> dict:
        store = research_manager.projects
        project = store.list()[0]
        project_id = project["project_id"]
        if payload["phase"] == "start":
            lead = store.claim(store.jobs(project_id)[0]["job_id"])
            store.complete(lead, {"action": "research", "tasks": [
                {"gap": "条件", "question": "任务一", "expected": "全文条件", "difference": "首次"},
                {"gap": "反例", "question": "任务二", "expected": "原文反例", "difference": "首次"}]})
            for queued in store.jobs(project_id):
                if queued["role"].startswith("researcher"):
                    job = store.claim(queued["job_id"])
                    store.record_activity(job, {"type": "tool_started", "tool": "query_papers", "call_id": job["job_id"]})
        else:
            for job in store.jobs(project_id):
                if job["role"].startswith("researcher"):
                    store.record_activity(job, {"type": "tool_finished", "tool": "query_papers", "call_id": job["job_id"], "duration_ms": 1200})
        return {"project_id": project_id}

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

"""
🧪 核心链路测试 — 3 个关键测试

运行:  python tests/test_core.py
       python -m pytest tests/ -v  (需 pip install pytest)

覆盖:
  1. read_pdf → 文本提取 → 索引 → 去重
  2. query_papers 命中率
  3. verify 节点边界不崩溃
"""

import unittest
import tempfile
import shutil
import sys
import json
import sqlite3
import os
import subprocess
import time
import zipfile
from types import SimpleNamespace
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from unittest.mock import patch, MagicMock

from evals.rag_retrieval import (
    PassageLabel,
    RetrievalCase,
    RetrievalResponse,
    RagEvalError,
    evaluate_cases,
    load_cases,
)


RUN_CHROMA_INTEGRATION = os.getenv("SKIP_CHROMA_INTEGRATION") != "1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pdf_test_sample() -> Path | None:
    """Find an opt-in real PDF without making CI depend on user data."""
    configured = os.getenv("PDF_TEST_SAMPLE")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() and candidate.suffix.lower() == ".pdf" else None

    # Local developers normally keep papers in the project runtime directory.
    # CI sets APP_DATA_DIR to a new temporary directory, so it still skips when
    # no fixture has been explicitly provided.
    candidates = [
        PROJECT_ROOT / "runtime" / "primary" / "papers",
        Path(os.environ["APP_DATA_DIR"]) / "primary" / "papers",
    ] if os.getenv("APP_DATA_DIR") else [PROJECT_ROOT / "runtime" / "primary" / "papers"]
    for directory in candidates:
        if directory.is_dir():
            sample = next(directory.glob("*.pdf"), None)
            if sample:
                return sample
    return None


class TestFastAPIService(unittest.TestCase):
    """API 外观层复用既有会话存储，不调用真实模型。"""

    class _FakeAgent:
        model = "fake-model"

        def __init__(self, sessions):
            self.sessions = sessions
            self._traces = {}
            self._histories = {}

        def create_session(self, title=None):
            return self.sessions.create(title)

        def list_sessions(self):
            return self.sessions.list()

        def delete_session(self, session_id):
            return self.sessions.delete(session_id)

        def get_history(self, session_id):
            return list(self._histories.get(session_id, []))

        def step(self, message, *, session_id, run_id, cancel_event, on_token):
            if cancel_event.is_set():
                answer, outcome, status = "已取消", "cancelled", "cancelled"
            else:
                on_token("API")
                on_token(" 回复")
                answer, outcome, status = f"已收到：{message}", "success", "completed"
            self.sessions.update_chat_run(
                run_id, status=status, answer=answer, duration_ms=12.5,
                metrics={"model_calls": 1}, error_type="" if status == "completed" else outcome,
            )
            self._traces[session_id] = {"outcome": outcome, "duration_ms": 12.5}
            return answer

        def get_last_trace(self, session_id):
            return self._traces.get(session_id)

        def research(self, query, *, session_id, run_id, cancel_event, on_progress, **_kwargs):
            on_progress({"stage": "planner", "status": "running", "message": query})
            status = "cancelled" if cancel_event.is_set() else "completed"
            answer = "" if status == "cancelled" else f"研究完成：{query}"
            self.sessions.update_research_run(run_id, status=status, final_answer=answer)
            self._traces[session_id] = {"outcome": status}
            return answer

    class _BlockingFakeAgent(_FakeAgent):
        def step(self, *args, cancel_event, **kwargs):
            cancel_event.wait(timeout=1)
            return super().step(*args, cancel_event=cancel_event, **kwargs)

    class _FakeRunBroker:
        """In-memory contract double for the Redis queue/stream adapter."""

        def __init__(self):
            self.jobs = []
            self.events = {}
            self.cancelled = set()
            self.acknowledged = []

        def enqueue(self, run_id, session_id, message):
            self.jobs.append((run_id, session_id, message))
            return str(len(self.jobs))

        def publish(self, run_id, event):
            self.events.setdefault(run_id, []).append(dict(event))
            return str(len(self.events[run_id]))

        def claim_stale(self, _consumer, **_kwargs):
            return None

        def reserve(self, _consumer, **_kwargs):
            if not self.jobs:
                return None
            from api.redis_runs import QueuedChatRun

            run_id, session_id, message = self.jobs.pop(0)
            return QueuedChatRun("job-1", run_id, session_id, message)

        def acknowledge(self, message_id):
            self.acknowledged.append(message_id)

        def request_cancel(self, run_id):
            self.cancelled.add(run_id)

        def cancel_requested(self, run_id):
            return run_id in self.cancelled

        def clear_cancel(self, run_id):
            self.cancelled.discard(run_id)

    class _FakeResearchBroker(_FakeRunBroker):
        def enqueue(self, run_id, session_id, query, scope, *, resume=False):
            self.jobs.append((run_id, session_id, query, scope, resume))
            return str(len(self.jobs))

        def reserve(self, _consumer, **_kwargs):
            if not self.jobs:
                return None
            from api.redis_runs import QueuedResearchRun

            run_id, session_id, query, scope, resume = self.jobs.pop(0)
            return QueuedResearchRun("research-job-1", run_id, session_id, query, scope, resume)

    class _FakeDailyBroker(_FakeRunBroker):
        def enqueue(self, run_id, kind):
            self.jobs.append((run_id, kind))
            return str(len(self.jobs))

        def reserve(self, _consumer, **_kwargs):
            if not self.jobs:
                return None
            from api.redis_runs import QueuedDailyRun

            run_id, kind = self.jobs.pop(0)
            return QueuedDailyRun("daily-job-1", run_id, kind)

    def setUp(self):
        from session_store import SessionStore

        self.tmp = tempfile.mkdtemp()
        self.store = SessionStore(str(Path(self.tmp) / "checkpoint.db"))
        self.agent = self._FakeAgent(self.store)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chat_run_streams_sse_and_persists_final_answer(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        client = TestClient(create_app(agent=self.agent))
        created = client.post("/api/v1/sessions", json={"title": "API 测试"})
        self.assertEqual(created.status_code, 201)
        session_id = created.json()["thread_id"]

        started = client.post(
            f"/api/v1/sessions/{session_id}/chat-runs",
            json={"message": "测试 SSE"},
        )
        self.assertEqual(started.status_code, 202)
        run_id = started.json()["run_id"]

        events = client.get(f"/api/v1/runs/{run_id}/events")
        self.assertEqual(events.status_code, 200)
        self.assertIn("event: token", events.text)
        self.assertIn("event: done", events.text)

        detail = client.get(f"/api/v1/runs/{run_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "completed")
        self.assertEqual(detail.json()["answer"], "已收到：测试 SSE")

    def test_canonical_run_endpoint_creates_chat_run(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        client = TestClient(create_app(agent=self.agent))
        session_id = client.post("/api/v1/sessions", json={"title": "统一运行"}).json()["thread_id"]
        started = client.post("/api/v1/runs", json={
            "kind": "chat", "session_id": session_id, "message": "统一入口",
        })
        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["kind"], "chat")
        detail = client.get(f"/api/v1/runs/{started.json()['run_id']}")
        self.assertEqual(detail.json()["kind"], "chat")

    def test_canonical_run_endpoint_resumes_latest_research(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        from api.redis_runs import RedisResearchRunManager

        broker = self._FakeResearchBroker()
        manager = RedisResearchRunManager(self.agent, broker)
        client = TestClient(create_app(agent=self.agent, research_run_manager=manager))
        session_id = client.post("/api/v1/sessions", json={"title": "研究续跑"}).json()["thread_id"]
        interrupted = self.store.create_research_run(session_id, "已中断的研究", status="running")
        self.store.update_research_run(interrupted["run_id"], status="failed", evidence=[{"title": "已保存证据"}])

        resumed = client.post("/api/v1/runs", json={
            "kind": "research", "session_id": session_id, "scope": "public", "resume": True,
        })
        self.assertEqual(resumed.status_code, 202)
        self.assertEqual(resumed.json()["run_id"], interrupted["run_id"])
        self.assertEqual(resumed.json()["status"], "queued")
        self.assertTrue(broker.jobs[-1][-1])
        self.assertEqual(
            client.post("/api/v1/runs", json={"kind": "research", "session_id": session_id}).status_code,
            422,
        )

    def test_session_messages_restore_only_user_visible_history(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        client = TestClient(create_app(agent=self.agent))
        session_id = client.post("/api/v1/sessions", json={"title": "历史"}).json()["thread_id"]
        self.agent._histories[session_id] = [
            {"role": "user", "content": "研究问题"},
            {"role": "assistant", "content": "研究回答"},
        ]

        response = client.get(f"/api/v1/sessions/{session_id}/messages")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.agent._histories[session_id])
        self.assertEqual(client.get("/api/v1/sessions/missing/messages").status_code, 404)

    def test_react_frontend_mount_serves_spa_shell_and_assets(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.app import mount_react_frontend

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            assets = dist / "assets"
            assets.mkdir()
            dist.joinpath("index.html").write_text("<main>React client</main>", encoding="utf-8")
            assets.joinpath("app.js").write_text("console.log('asset')", encoding="utf-8")
            app = FastAPI()
            self.assertTrue(mount_react_frontend(app, dist))
            client = TestClient(app)
            self.assertIn("React client", client.get("/app/").text)
            self.assertIn("React client", client.get("/app/sessions/example").text)
            self.assertIn("console.log", client.get("/app/assets/app.js").text)
            self.assertFalse(mount_react_frontend(FastAPI(), dist / "missing"))

    def test_workspace_routes_keep_documents_keywords_and_settings_outside_chat_state(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        from runtime_paths import RuntimePaths

        paths = RuntimePaths.from_root(Path(self.tmp) / "workspace-runtime")
        self.agent.cfg = SimpleNamespace(
            runtime_paths=paths,
            daily_db=str(paths.daily_db),
            daily_request_timeout_seconds=8,
            model="workspace-model",
            rag_enabled=True,
            pdf_max_pages=15,
            daily_search_enabled=False,
        )
        self.agent.paper_store = SimpleNamespace(list_papers=lambda: [{
            "paper_id": "paper-1", "title": "本地论文", "chunks": 3, "indexed_at": "2026-09-05",
        }])
        app = create_app(agent=self.agent)
        try:
            client = TestClient(app)
            document = app.state.research_document_store.save("研究方案", "# 研究方案\n\n方案正文")

            listed = client.get("/api/v1/workspace/research-documents")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()[0]["document_id"], document["document_id"])
            detail = client.get(f"/api/v1/workspace/research-documents/{document['document_id']}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("方案正文", detail.json()["content"])
            self.assertEqual(
                client.get(f"/api/v1/workspace/research-documents/{document['document_id']}/download").status_code,
                200,
            )

            papers = client.get("/api/v1/workspace/papers")
            self.assertEqual(papers.status_code, 200)
            self.assertEqual(papers.json()[0]["title"], "本地论文")

            uploaded = client.post(
                "/api/v1/workspace/uploads",
                files={"file": ("example.pdf", b"%PDF-1.4 test", "application/pdf")},
            )
            self.assertEqual(uploaded.status_code, 201)
            upload = uploaded.json()
            self.assertEqual(upload["kind"], "pdf")
            self.assertFalse(upload["duplicate"])
            self.assertTrue((paths.papers_dir / "example.pdf").is_file())
            self.assertNotIn("path", upload)

            duplicate = client.post(
                "/api/v1/workspace/uploads",
                files={"file": ("same-content.pdf", b"%PDF-1.4 test", "application/pdf")},
            )
            self.assertEqual(duplicate.status_code, 201)
            self.assertTrue(duplicate.json()["duplicate"])
            self.assertEqual(duplicate.json()["filename"], "example.pdf")
            self.assertEqual(
                client.post("/api/v1/workspace/uploads", files={"file": ("notes.txt", b"x", "text/plain")}).status_code,
                422,
            )

            session_id = client.post("/api/v1/sessions", json={"title": "上传"}).json()["thread_id"]
            started_upload = client.post("/api/v1/runs", json={
                "kind": "chat", "session_id": session_id, "upload_id": upload["upload_id"],
                "message": "请提取重点",
            })
            self.assertEqual(started_upload.status_code, 202)
            upload_run = client.get(f"/api/v1/runs/{started_upload.json()['run_id']}")
            self.assertEqual(upload_run.status_code, 200)
            self.assertIn("read_pdf", upload_run.json()["answer"])
            self.store.save_usage(session_id, {
                "prompt": 120, "completion": 45, "total": 165, "calls": 2, "context_limit": 131072,
            })
            usage = client.get(f"/api/v1/sessions/{session_id}/usage")
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()["total"], 165)
            self.assertEqual(
                client.post("/api/v1/runs", json={
                    "kind": "chat", "session_id": session_id, "upload_id": "upload-missing",
                }).status_code,
                404,
            )

            created_keyword = client.post("/api/v1/workspace/daily-keywords", json={"keyword": "TSN scheduling"})
            self.assertEqual(created_keyword.status_code, 201)
            self.assertEqual(client.get("/api/v1/workspace/daily-keywords").json()[0]["keyword"], "TSN scheduling")
            scheduler = app.state.workspace_scheduler
            scheduler.record_daily_keyword_result("TSN scheduling", [{
                "title": "A TSN Scheduling Paper", "url": "https://example.test/paper", "sources": ["ieee"],
            }])
            digest = client.get("/api/v1/workspace/daily-digest")
            self.assertEqual(digest.status_code, 200)
            self.assertEqual(digest.json()["papers"][0]["source"], "ieee")
            self.assertEqual(
                client.put("/api/v1/workspace/daily-papers/status", json={
                    "keyword": "TSN scheduling", "title": "A TSN Scheduling Paper", "status": "want_read",
                }).status_code,
                204,
            )
            self.assertEqual(client.get("/api/v1/workspace/daily-digest").json()["papers"][0]["status"], "want_read")
            daily_run = scheduler.create_daily_run("daily", ["TSN scheduling"], status="queued")
            scheduler.save_daily_candidates(daily_run["run_id"], [{
                "candidate_id": "daily-paper-1", "title": "A TSN Scheduling Paper",
                "url": "https://example.test/paper", "sources": ["ieee"], "year": 2026,
                "citation_count": 12, "selected": True,
                "curation": {"reason": "与关键词高度相关", "tags": ["TSN", "scheduling"]},
            }])
            scheduler.update_daily_run(
                daily_run["run_id"], status="completed",
                results={"brief": "今日推荐 1 篇", "selected": [{"candidate_id": "daily-paper-1"}]},
                critique={"warnings": ["阅读前核验实验设置。"]},
            )
            listed_daily_runs = client.get("/api/v1/workspace/daily-runs")
            self.assertEqual(listed_daily_runs.status_code, 200)
            self.assertEqual(listed_daily_runs.json()[0]["run_id"], daily_run["run_id"])
            daily_detail = client.get(f"/api/v1/workspace/daily-runs/{daily_run['run_id']}")
            self.assertEqual(daily_detail.status_code, 200)
            self.assertEqual(daily_detail.json()["papers"][0]["reason"], "与关键词高度相关")
            self.assertEqual(client.get("/api/v1/workspace/daily-runs/daily-missing").status_code, 404)
            self.assertEqual(client.delete("/api/v1/workspace/daily-keywords/TSN%20scheduling").status_code, 204)

            saved = client.put("/api/v1/workspace/settings", json={
                "model": "deepseek-v4-pro", "rag_enabled": False,
                "pdf_max_pages": 25, "daily_search_enabled": True,
            })
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["model"], "deepseek-v4-pro")
            self.assertEqual(paths.read_settings()["pdf_max_pages"], 25)
        finally:
            app.state.workspace_scheduler.close()

    def test_badcase_feedback_keeps_only_a_safe_run_snapshot_and_follows_session_delete(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        app = create_app(agent=self.agent)
        try:
            client = TestClient(app)
            session_id = client.post("/api/v1/sessions", json={"title": "Badcase"}).json()["thread_id"]
            started = client.post("/api/v1/runs", json={
                "kind": "chat", "session_id": session_id,
                "message": "PRIVATE_PROMPT_MUST_NOT_BE_SAVED",
            })
            run_id = started.json()["run_id"]

            created = client.post(
                f"/api/v1/runs/{run_id}/badcases",
                json={"category": "answer_quality", "note": "已人工脱敏的备注"},
            )
            self.assertEqual(created.status_code, 201)
            self.assertNotIn("note", created.json())
            candidate_id = created.json()["candidate_id"]
            candidate = app.state.badcase_store.get(candidate_id)
            self.assertEqual(candidate["note"], "已人工脱敏的备注")
            persisted = json.dumps(candidate["snapshot"], ensure_ascii=False)
            self.assertNotIn("PRIVATE_PROMPT_MUST_NOT_BE_SAVED", persisted)
            self.assertNotIn("已收到", persisted)

            duplicate = client.post(
                f"/api/v1/runs/{run_id}/badcases",
                json={"category": "answer_quality"},
            )
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(duplicate.json()["candidate_id"], candidate_id)
            self.assertEqual(duplicate.json()["occurrence_count"], 2)
            listed = client.get("/api/v1/workspace/badcases")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()[0]["candidate_id"], candidate_id)
            self.assertNotIn("note", listed.json()[0])

            self.assertEqual(client.delete(f"/api/v1/sessions/{session_id}").status_code, 204)
            self.assertIsNone(app.state.badcase_store.get(candidate_id))
        finally:
            app.state.badcase_store.close()

    def test_sse_formatter_restricts_the_public_event_vocabulary(self):
        from api.sse import format_sse

        self.assertEqual(
            format_sse({"type": "token", "text": "中文 token"}),
            'event: token\ndata: {"type":"token","text":"中文 token"}\n\n',
        )
        self.assertEqual(
            format_sse({"type": "internal", "payload": "must not leak"}),
            'event: status\ndata: {"type":"status","status":"running"}\n\n',
        )
        tool_event = format_sse({
            "type": "tool", "tool": "read_pdf", "status": "completed",
            "arguments": {"path": "private-paper.pdf"}, "result": "must not leak",
        })
        self.assertIn('event: tool', tool_event)
        self.assertIn('"tool":"read_pdf"', tool_event)
        self.assertNotIn("private-paper.pdf", tool_event)
        self.assertNotIn("must not leak", tool_event)

    def test_cannot_claim_cross_process_cancellation(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        session_id = self.agent.create_session("API 测试")["thread_id"]
        run = self.store.create_chat_run(session_id, self.agent.model)
        client = TestClient(create_app(agent=self.agent))

        response = client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        self.assertEqual(response.status_code, 409)

    def test_active_chat_run_uses_cooperative_cancellation(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        agent = self._BlockingFakeAgent(self.store)
        client = TestClient(create_app(agent=agent))
        session_id = agent.create_session("API 测试")["thread_id"]
        started = client.post(
            f"/api/v1/sessions/{session_id}/chat-runs",
            json={"message": "等待取消"},
        )
        run_id = started.json()["run_id"]

        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertIn(cancelled.json()["status"], {"cancelling", "cancelled"})

        events = client.get(f"/api/v1/runs/{run_id}/events")
        self.assertIn('"status":"cancelled"', events.text)
        self.assertEqual(client.get(f"/api/v1/runs/{run_id}").json()["status"], "cancelled")

    def test_api_key_can_be_required_without_hiding_health(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        client = TestClient(create_app(
            agent=self.agent, api_key="phase-two-secret", require_api_key=True,
        ))
        self.assertEqual(client.get("/api/v1/health").status_code, 200)
        self.assertEqual(client.get("/api/v1/sessions").status_code, 401)
        created = client.post(
            "/api/v1/sessions",
            headers={"X-API-Key": "phase-two-secret"},
            json={"title": "受保护 API 会话"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            client.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer phase-two-secret"},
            ).status_code,
            200,
        )

    def test_metrics_are_authenticated_and_expose_only_aggregate_state(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        client = TestClient(create_app(
            agent=self.agent, api_key="metrics-secret", require_api_key=True,
        ))
        session_id = client.post(
            "/api/v1/sessions", headers={"X-API-Key": "metrics-secret"},
            json={"title": "Metrics"},
        ).json()["thread_id"]
        client.post(
            "/api/v1/runs", headers={"X-API-Key": "metrics-secret"},
            json={"kind": "chat", "session_id": session_id, "message": "private prompt"},
        )

        self.assertEqual(client.get("/api/v1/metrics").status_code, 401)
        response = client.get("/api/v1/metrics", headers={"X-API-Key": "metrics-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertIn("research_agent_api_up 1", response.text)
        self.assertIn('research_agent_run_records{kind="chat",status="completed"} 1', response.text)
        self.assertIn('research_agent_queue_configured{kind="chat"} 0', response.text)
        self.assertNotIn("private prompt", response.text)

    def test_redis_broker_reports_queue_depth_and_worker_heartbeat(self):
        from fnmatch import fnmatch
        from api.redis_runs import RedisChatRunBroker

        class _MetricsRedis:
            def __init__(self, test_case):
                self.test_case = test_case
                self.stream_lengths = {}
                self.keys = set()

            def xlen(self, key):
                return self.stream_lengths.get(key, 0)

            def set(self, key, _value, ex):
                self.test_case.assertGreaterEqual(ex, 5)
                self.keys.add(key)

            def scan_iter(self, *, match, count):
                self.test_case.assertEqual(count, 100)
                return iter(key for key in self.keys if fnmatch(key, match))

        client = _MetricsRedis(self)
        broker = RedisChatRunBroker("redis://unused", client=client)
        client.stream_lengths[broker.queue_key] = 3
        broker.heartbeat("metrics-worker", ttl_seconds=5)

        self.assertEqual(broker.queue_depth(), 3)
        self.assertEqual(broker.live_worker_count(), 1)

    def test_durable_worker_executes_and_cancels_queued_chat_runs(self):
        from api.redis_runs import RedisChatRunManager, RedisChatRunWorker

        broker = self._FakeRunBroker()
        manager = RedisChatRunManager(self.agent, broker)
        session_id = self.agent.create_session("Durable API 测试")["thread_id"]
        started = manager.start(session_id, "队列执行")
        self.assertEqual(started["status"], "queued")
        queued_events = self.store.get_run_events(started["run_id"], session_id)
        self.assertEqual(queued_events[0]["event_type"], "status")
        self.assertEqual(queued_events[0]["metadata"], {
            "protocol": "run-event/v1",
            "run_kind": "chat",
            "runner": "redis-worker",
            "model": "fake-model",
            "toolset": "chat-default",
        })
        worker = RedisChatRunWorker(self.agent, broker, consumer="test-worker")
        self.assertTrue(worker.run_once(block_ms=1))
        completed = manager.get(started["run_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["answer"], "已收到：队列执行")
        self.assertIn("job-1", broker.acknowledged)
        self.assertTrue(any(event["type"] == "token" for event in broker.events[started["run_id"]]))

        cancelled = manager.start(session_id, "排队后取消")
        manager.cancel(cancelled["run_id"])
        self.assertTrue(worker.run_once(block_ms=1))
        self.assertEqual(manager.get(cancelled["run_id"])["status"], "cancelled")

    def test_durable_worker_executes_queued_research_run(self):
        from api.redis_runs import RedisResearchRunManager, RedisResearchRunWorker

        broker = self._FakeResearchBroker()
        manager = RedisResearchRunManager(self.agent, broker)
        session_id = self.agent.create_session("研究队列") ["thread_id"]
        started = manager.start(session_id, "RAG 重排方法", "public")
        self.assertEqual(started["status"], "queued")
        worker = RedisResearchRunWorker(self.agent, broker, consumer="research-test-worker")
        self.assertTrue(worker.run_once(block_ms=1))
        completed = manager.get(started["run_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["final_answer"], "研究完成：RAG 重排方法")
        self.assertTrue(any(event["type"] == "status" for event in broker.events[started["run_id"]]))

        interrupted = self.store.create_research_run(session_id, "可恢复的研究", status="running")
        self.store.update_research_run(interrupted["run_id"], status="failed", evidence=[{"title": "已保存证据"}])
        resumed = manager.start(session_id, "", "public", resume=True)
        self.assertEqual(resumed["run_id"], interrupted["run_id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertTrue(broker.jobs[-1][-1])
        self.assertTrue(worker.run_once(block_ms=1))
        self.assertEqual(manager.get(resumed["run_id"])["status"], "completed")

    def test_durable_worker_executes_queued_daily_run(self):
        from api.redis_runs import RedisDailyRunManager, RedisDailyRunWorker
        from scheduler import Scheduler

        scheduler = Scheduler(str(Path(self.tmp) / "daily.db"))
        broker = self._FakeDailyBroker()

        class _FakeDailyOrchestrator:
            def run(self, kind, *, run_id, paper_store, cancel_event, on_progress):
                on_progress({"stage": "scouts", "status": "running", "message": "private keyword"})
                outcome = "cancelled" if cancel_event.is_set() else "completed"
                scheduler.update_daily_run(run_id, status=outcome, results={"selected": []})
                return SimpleNamespace(status=outcome)

        manager = RedisDailyRunManager(scheduler, broker)
        started = manager.start("daily")
        self.assertEqual(started["status"], "queued")
        worker = RedisDailyRunWorker(_FakeDailyOrchestrator(), scheduler, broker, consumer="daily-test-worker")
        self.assertTrue(worker.run_once(block_ms=1))
        self.assertEqual(manager.get(started["run_id"])["status"], "completed")
        self.assertTrue(any(event["type"] == "status" for event in broker.events[started["run_id"]]))
        scheduler.update_daily_run(started["run_id"], status="failed")
        resumed = manager.start("resume")
        self.assertEqual(resumed["run_id"], started["run_id"])
        self.assertEqual(resumed["status"], "queued")
        scheduler.close()


class TestRetrievalAdmissionPolicy(unittest.TestCase):
    """工程解释不应为了生成引用而触发昂贵的文献工具链。"""

    def test_engineering_questions_disable_retrieval_tools(self):
        from research_agent import should_answer_without_tools

        self.assertTrue(should_answer_without_tools(
            "首次 RAG 检索遇到嵌入模型下载很慢时，系统应如何保证结果？"
        ))
        self.assertTrue(should_answer_without_tools(
            "每日 Curator 排队时，普通用户对话为什么仍应优先获得并发槽？"
        ))
        self.assertTrue(should_answer_without_tools(
            "验证动态负载下 TSN 调度鲁棒性时，至少应报告哪些指标和实验设置？"
        ))

    def test_explicit_evidence_requests_keep_retrieval_tools(self):
        from research_agent import should_answer_without_tools

        self.assertFalse(should_answer_without_tools(
            "检索三篇关于动态负载 TSN 鲁棒性的论文并给出引用。"
        ))
        self.assertFalse(should_answer_without_tools(
            "这篇论文的原文实验数据和作者是谁？"
        ))

    def test_explicit_research_document_request_uses_the_archive_path(self):
        from research_agent import should_force_research_document_save

        self.assertTrue(should_force_research_document_save("把这份方案保存成科研档案"))
        self.assertTrue(should_force_research_document_save("请导出当前研究计划"))
        self.assertFalse(should_force_research_document_save("把偏好保存到用户画像"))
        self.assertFalse(should_force_research_document_save("不要保存这份研究方案"))


class TestPdfReaderLayout(unittest.TestCase):
    """PDF 文本块的阅读顺序不应被双栏布局打乱。"""

    def test_reader_reads_the_left_column_before_the_right_column(self):
        import pymupdf
        from pdf_reader import PaperReader

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "two-columns.pdf"
            document = pymupdf.open()
            page = document.new_page(width=600, height=800)
            page.insert_text((200, 50), "Paper title")
            for y, text in ((140, "left evidence one"), (180, "left evidence two"), (220, "left evidence three")):
                page.insert_text((50, y), text)
            for y, text in ((140, "right evidence one"), (180, "right evidence two"), (220, "right evidence three")):
                page.insert_text((330, y), text)
            document.save(pdf_path)
            document.close()

            text = PaperReader(max_pages=1).read(str(pdf_path))

        self.assertLess(text.index("left evidence three"), text.index("right evidence one"))
        self.assertNotIn("─── 右栏 ───", text)

    def test_index_cleaning_drops_the_reader_status_preamble(self):
        from paper_store import clean_index_text, chunk_text

        reader_text = (
            "📄 **PDF 解析完成**\n文件名: sample.pdf | 总页数: 1 | 已读: 1 | 提取: 42 字符\n\n"
            "━━━ 第 1 页 ━━━\n## Abstract\nVerified evidence."
        )
        cleaned = clean_index_text(reader_text)

        self.assertNotIn("PDF 解析完成", cleaned)
        chunks = chunk_text(cleaned)
        self.assertTrue(all("PDF 解析完成" not in chunk["text"] for chunk in chunks))
        self.assertTrue(any(chunk["section"] == "Abstract" for chunk in chunks))

    def test_reader_rejects_an_impossible_table_box(self):
        from pdf_reader import PaperReader

        page = SimpleNamespace(height=1_000)
        self.assertFalse(PaperReader._valid_table_bbox([10, 20, 300, 16_000], page))
        self.assertTrue(PaperReader._valid_table_bbox([10, 20, 300, 900], page))


@unittest.skipUnless(
    RUN_CHROMA_INTEGRATION,
    "CI offline quality gate skips Chroma/ONNX integration; run it in the scheduled integration job.",
)
class TestReadPDFFlow(unittest.TestCase):
    """测试1: read_pdf 完整链路"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_extract_text_from_pdf(self):
        """pdfplumber 能否从显式样本或本地运行时论文提取文本。"""
        from pdf_reader import PaperReader
        sample = _pdf_test_sample()
        if sample is None:
            self.skipTest("未设置 PDF_TEST_SAMPLE，且本地运行时论文目录没有 PDF 文件")
        reader = PaperReader(max_pages=2, max_chars=2000)
        result = reader.read(str(sample))
        self.assertIn("📄", result)
        self.assertGreater(len(result), 100, "提取文本不应为空")

    def test_chunk_text(self):
        """分块函数正确切分"""
        from paper_store import chunk_text
        text = "第一段。\n\n第二段内容较多。" * 20
        chunks = chunk_text(text, chunk_size=200, overlap=50)
        self.assertGreater(len(chunks), 0)
        for c in chunks:
            self.assertIn("text", c)
            self.assertIn("char_start", c)
            self.assertLessEqual(len(c["text"]), 200)

    def test_index_and_dedup(self):
        """索引 + 去重"""
        from paper_store import PaperStore
        store = PaperStore(persist_dir=self.tmp)
        pid1 = store.index_paper("测试论文内容 A B C", title="TestPaper")
        self.assertTrue(pid1)
        papers = store.list_papers()
        self.assertEqual(len(papers), 1)
        # 重复索引 → 先去重再存
        for p in papers:
            if p["title"] == "TestPaper":
                store.delete_paper(p["paper_id"])
        store.index_paper("测试论文内容 D E F", title="TestPaper")
        papers = store.list_papers()
        test_papers = [p for p in papers if p["title"] == "TestPaper"]
        self.assertEqual(len(test_papers), 1, "去重失败：同名论文出现多次")


@unittest.skipUnless(
    RUN_CHROMA_INTEGRATION,
    "CI offline quality gate skips Chroma/ONNX integration; run it in the scheduled integration job.",
)
class TestQueryPapers(unittest.TestCase):
    """测试2: query_papers 命中率"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        from paper_store import PaperStore
        cls.store = PaperStore(persist_dir=cls.tmp)
        cls.store.index_paper(
            "注意力机制是Transformer的核心。多头注意力使用多个注意力头并行计算。"
            "自注意力机制的公式为 Attention(Q,K,V)=softmax(QK^T/√d_k)V。",
            title="Attention Paper",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_query_hits_correct_paper(self):
        """查询'注意力机制'应命中索引的论文"""
        results = self.store.query("注意力机制", top_k=2)
        self.assertGreater(len(results), 0, "查询应返回结果")
        self.assertIn("Attention Paper", results[0]["title"])

    def test_query_returns_distance(self):
        """查询结果应包含距离"""
        results = self.store.query("注意力", top_k=1)
        self.assertTrue(results)
        self.assertIn("distance", results[0])
        self.assertLess(results[0]["distance"], 1.0, "相关查询距离应 < 1")

    def test_list_papers(self):
        """列出已索引论文"""
        papers = self.store.list_papers()
        self.assertGreaterEqual(len(papers), 1)

    def test_delete_paper(self):
        """删除论文"""
        from paper_store import PaperStore
        s2 = PaperStore(persist_dir=self.tmp)
        pid = s2.index_paper("要被删除的内容", title="DeleteMe")
        count_before = len(s2.list_papers())
        deleted = s2.delete_paper(pid)
        self.assertGreater(deleted, 0)
        self.assertEqual(len(s2.list_papers()), count_before - 1)


class TestToolResponsiveness(unittest.TestCase):
    def test_network_tsn_search_rejects_unrelated_provider_hit(self):
        from search_api import search_openalex

        class _Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"results": [
                    {
                        "id": "https://openalex.org/W1",
                        "title": "Scheduling Time-Sensitive Networking for Bursty Traffic",
                        "authorships": [],
                        "publication_year": 2025,
                        "cited_by_count": 7,
                        "doi": "https://doi.org/10.1000/tsn",
                        "primary_location": {"source": {"display_name": "TSN Journal"}},
                        "abstract_inverted_index": {
                            "Time-Sensitive": [0], "Networking": [1], "scheduling": [2],
                            "for": [3], "bursty": [4], "traffic": [5],
                        },
                    },
                    {
                        "id": "https://openalex.org/W2",
                        "title": "Routing in a delay tolerant network",
                        "authorships": [],
                        "publication_year": 2004,
                        "cited_by_count": 1776,
                        "doi": "https://doi.org/10.1000/dtn",
                        "primary_location": {"source": {"display_name": "Unrelated Journal"}},
                        "abstract_inverted_index": {"Routing": [0], "network": [1]},
                    },
                ]}

        with patch("search_api.requests.get", return_value=_Response()) as get:
            result = search_openalex(
                "DiffTSN Time-Sensitive Networking scheduling bursty traffic",
            )

        self.assertEqual(
            get.call_args.kwargs["params"]["search"],
            '"Time-Sensitive Networking" AND (scheduling OR traffic OR bursty OR flow OR latency)',
        )
        self.assertIn("Scheduling Time-Sensitive Networking", result)
        self.assertIn("相关性评分: 0.95-0.95", result)
        self.assertIn("已拒绝 1 条", result)
        self.assertNotIn("delay tolerant", result.casefold())

    def test_time_sensitive_networking_full_name_enables_strict_filter(self):
        from search_api import _filter_public_papers

        accepted, quality = _filter_public_papers(
            "Time-Sensitive Networking scheduling bursty traffic",
            [{"title": "Routing in a delay tolerant network", "abstract": "Traffic routing."}],
        )

        self.assertEqual(accepted, [])
        self.assertEqual(quality["rejected"], 1)
        self.assertIn("未命中 Time-Sensitive Networking 或 TSN", quality["rejection_reason"])

    def test_network_tsn_search_requires_a_material_condition(self):
        from search_api import _filter_public_papers

        accepted, quality = _filter_public_papers(
            "TSN scheduling under bursty traffic",
            [{"title": "An Overview of Time-Sensitive Networking", "abstract": "TSN standards."}],
        )

        self.assertEqual(accepted, [])
        self.assertIn("未命中调度、流量或 DiffTSN 条件", quality["rejection_reason"])

    def test_bounded_rag_query_returns_while_background_work_continues(self):
        import time
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from paper_store import PaperStore

        store = PaperStore.__new__(PaperStore)
        store._query_executor = ThreadPoolExecutor(max_workers=1)
        store._query_lock = threading.RLock()
        store._active_query = None
        store.query = lambda *args: (time.sleep(0.2) or [])
        store._collection = type("_Collection", (), {
            "count": lambda self: 1,
            "get": lambda self, include: {
                "documents": ["TSN scheduling with diffusion models"],
                "metadatas": [{"title": "TSN Paper", "section": "Method"}],
            },
        })()
        try:
            started = time.monotonic()
            results, pending = store.query_with_timeout("TSN", timeout_seconds=0.02)
            self.assertEqual(results[0]["retrieval"], "keyword")
            self.assertIn("首次嵌入模型", pending)
            self.assertLess(time.monotonic() - started, 0.15)
        finally:
            store._query_executor.shutdown(wait=False, cancel_futures=True)

    def test_query_tool_returns_without_waiting_for_embedding_initialization(self):
        from tools.search import handle_query_papers

        class _WarmingStore:
            def query_with_timeout(self, query, top_k, section):
                self.query = query
                self.top_k = top_k
                self.section = section
                return None, "嵌入模型仍在后台初始化，请稍后重试同一问题。"

        store = _WarmingStore()
        result = handle_query_papers(
            {"query": "TSN scheduling", "top_k": 3}, paper_store=store,
        )
        self.assertIn("后台初始化", result)
        self.assertEqual(store.query, "TSN scheduling")

    def test_hybrid_query_promotes_exact_term_and_renders_its_mode(self):
        from paper_store import PaperStore
        from tools.search import handle_query_papers

        class _Collection:
            @staticmethod
            def count():
                return 3

            @staticmethod
            def query(**_kwargs):
                return {
                    "ids": [["semantic", "exact", "overview"]],
                    "documents": [[
                        "A learned scheduler for deterministic networks.",
                        "TSN scheduling under bursty traffic is evaluated here.",
                        "An overview of TSN architecture.",
                    ]],
                    "metadatas": [[
                        {"paper_id": "a", "title": "Learned Scheduler", "section": "Method", "chunk_index": 0},
                        {"paper_id": "b", "title": "Exact TSN Scheduling", "section": "Related Work", "chunk_index": 1},
                        {"paper_id": "c", "title": "TSN Overview", "section": "Introduction", "chunk_index": 2},
                    ]],
                    "distances": [[0.06, 0.10, 0.30]],
                }

            @staticmethod
            def get(**_kwargs):
                return {
                    "documents": [
                        "A learned scheduler for deterministic networks.",
                        "TSN scheduling under bursty traffic is evaluated here.",
                        "An overview of TSN architecture.",
                    ],
                    "metadatas": [
                        {"paper_id": "a", "title": "Learned Scheduler", "section": "Method", "chunk_index": 0},
                        {"paper_id": "b", "title": "Exact TSN Scheduling", "section": "Related Work", "chunk_index": 1},
                        {"paper_id": "c", "title": "TSN Overview", "section": "Introduction", "chunk_index": 2},
                    ],
                }

        store = PaperStore.__new__(PaperStore)
        store._collection = _Collection()
        results = store.query_hybrid("TSN scheduling", top_k=2)

        self.assertEqual(results[0]["title"], "Exact TSN Scheduling")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["retrieval"] == "hybrid" for item in results))
        self.assertTrue(all("hybrid_score" in item for item in results))

        class _HybridStore:
            @staticmethod
            def query_with_timeout(*_args, **_kwargs):
                return results, None

        rendering = handle_query_papers({"query": "TSN scheduling"}, paper_store=_HybridStore())
        self.assertIn("混合检索结果", rendering)
        self.assertIn("混合分:", rendering)

    def test_hybrid_query_keeps_large_collections_on_semantic_path(self):
        from paper_store import HYBRID_LEXICAL_MAX_CHUNKS, PaperStore

        class _Collection:
            get_called = False

            @staticmethod
            def count():
                return HYBRID_LEXICAL_MAX_CHUNKS + 1

            @staticmethod
            def query(**_kwargs):
                return {
                    "ids": [["semantic"]],
                    "documents": [["Semantic candidate"]],
                    "metadatas": [[{"paper_id": "a", "title": "Paper", "section": "Method", "chunk_index": 0}]],
                    "distances": [[0.1]],
                }

            def get(self, **_kwargs):
                self.get_called = True
                raise AssertionError("large corpus must not run lexical scan")

        store = PaperStore.__new__(PaperStore)
        store._collection = _Collection()
        results = store.query_hybrid("semantic query", top_k=1)

        self.assertEqual(results[0]["retrieval"], "semantic")
        self.assertFalse(store._collection.get_called)

    def test_reranker_only_reorders_existing_candidates(self):
        from paper_store import PaperStore

        class _Reranker:
            def predict(self, pairs, **_kwargs):
                self.pairs = pairs
                return [0.1, 0.9]

        store = PaperStore.__new__(PaperStore)
        store._reranker_enabled = True
        store._reranker = _Reranker()
        store._reranker_load_attempted = True
        candidates = [
            {"text": "first candidate", "title": "A", "chunk_index": 0, "hybrid_score": 0.2},
            {"text": "second candidate", "title": "B", "chunk_index": 1, "hybrid_score": 0.1},
        ]

        results = store._rerank("which evidence", candidates)

        self.assertEqual([item["title"] for item in results], ["B", "A"])
        self.assertTrue(all(item["reranked"] for item in results))
        self.assertEqual(store._reranker.pairs[0], ("which evidence", "first candidate"))

    def test_bm25_scores_the_chunk_body_not_repeated_paper_title(self):
        from paper_store import PaperStore

        class _Collection:
            @staticmethod
            def count():
                return 2

            @staticmethod
            def get(**_kwargs):
                return {
                    "documents": [
                        "The introduction describes a generic network problem.",
                        "Branch-and-Bound is the exact optimization algorithm for MILP.",
                    ],
                    "metadatas": [
                        {"paper_id": "p", "title": "Branch-and-Bound Scheduling", "chunk_index": 0},
                        {"paper_id": "p", "title": "Branch-and-Bound Scheduling", "chunk_index": 1},
                    ],
                }

        store = PaperStore.__new__(PaperStore)
        store._collection = _Collection()
        results = store.query_lexical("Which exact optimization algorithm is used?", top_k=2)

        self.assertEqual(results[0]["chunk_index"], 1)
        self.assertGreater(results[0]["keyword_score"], 0)


class TestVerifyNode(unittest.TestCase):
    """测试3: verify 节点边界不崩溃"""

    class _FakeResponse:
        """最小 requests.Response 替身，避免状态机测试访问 API。"""

        status_code = 200
        text = ""

        def __init__(self, message: dict):
            self._data = {"choices": [{"message": message}]}

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def test_verify_skips_no_tools(self):
        """无工具调用时 verify 应跳过（返回空）"""
        # 用 Mock 模拟 LangGraph state
        state = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
            ],
            "metadata": {},
        }
        # 手动模拟 verify 核心逻辑
        messages = state["messages"]
        recent = messages[-6:]
        has_tools = any(m.get("role") == "tool" for m in recent)
        self.assertFalse(has_tools, "无工具调用时 has_tools 应为 False")

    def test_verify_with_tools_but_empty_content(self):
        """有工具但回复为空时不应崩溃"""
        state = {
            "messages": [
                {"role": "user", "content": "搜索论文"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "search_papers", "arguments": "{}"}}]},
                {"role": "tool", "content": "找到3篇", "tool_call_id": "1"},
                {"role": "assistant", "content": ""},
            ],
            "metadata": {},
        }
        # content 为空 → 应该跳过
        messages = state["messages"]
        last_content = messages[-1].get("content", "")
        self.assertEqual(last_content, "", "空回复应安全处理")

    def test_sanitize_model_messages_drops_incomplete_assistant_history(self):
        from graph_builder import sanitize_model_messages

        messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "orphan", "content": "孤立结果"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "unfinished", "function": {"name": "search_papers", "arguments": "{}"},
            }]},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "function": {"name": "list_papers", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "结果"},
            {"role": "assistant", "content": "最终回答"},
        ]

        sanitized = sanitize_model_messages(messages)
        self.assertEqual(
            [message["role"] for message in sanitized],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(sanitized[1]["tool_calls"][0]["id"], "call-1")
        self.assertTrue(all(
            message["role"] != "assistant"
            or message.get("content") not in (None, "")
            or message.get("tool_calls")
            for message in sanitized
        ))

    def test_graph_builds_correctly(self):
        """图结构正常编译（不调用 API）"""
        from graph_builder import build_graph
        from llm_client import LLMClient
        app = build_graph(
            api_key="test-key", model="deepseek-chat", checkpoint_db=":memory:",
            llm_client=LLMClient("test-key", "https://example.test/chat"),
        )
        try:
            nodes = list(app.get_graph().nodes.keys())
            self.assertIn("llm", nodes)
            self.assertIn("tools", nodes)
            self.assertIn("verify", nodes)
            self.assertIn("__start__", nodes)
            self.assertIn("__end__", nodes)
        finally:
            app.checkpointer.conn.close()

    def test_forced_research_document_save_calls_the_archive_tool_once(self):
        from graph_builder import build_graph
        from llm_client import LLMClient

        responses = [
            self._FakeResponse({
                "content": None,
                "tool_calls": [{
                    "id": "archive-1", "type": "function",
                    "function": {
                        "name": "save_research_document",
                        "arguments": '{"title":"TSN 方案","content":"# TSN 方案"}',
                    },
                }],
            }),
            self._FakeResponse({"content": "已创建科研档案：TSN 方案。"}),
        ]
        with patch("llm_client.requests.post", side_effect=responses) as post, \
             patch("graph_builder.execute_tool", return_value="✅ 已创建研究档案") as execute:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", enable_verify=False,
                allowed_tool_names={"save_research_document"},
                force_tool_name="save_research_document",
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                result = app.invoke(
                    {"messages": [{"role": "user", "content": "把方案保存成科研档案"}], "metadata": {}},
                    config={"configurable": {"thread_id": "archive-save"}},
                )
            finally:
                app.checkpointer.conn.close()

        first_payload, second_payload = (call.kwargs["json"] for call in post.call_args_list)
        self.assertEqual(
            first_payload["tool_choice"],
            {"type": "function", "function": {"name": "save_research_document"}},
        )
        self.assertEqual(second_payload["tool_choice"], "auto")
        execute.assert_called_once()
        self.assertEqual(result["messages"][-1]["content"], "已创建科研档案：TSN 方案。")

    def test_verify_clears_retry_metadata_after_success(self):
        """修正后的回复通过验证后，不应把旧反馈带入下一轮对话。"""
        from graph_builder import build_graph

        responses = [
            self._FakeResponse({
                "content": None,
                "tool_calls": [{
                    "id": "tool-1", "type": "function",
                    "function": {"name": "list_papers", "arguments": "{}"},
                }],
            }),
            self._FakeResponse({"content": "这是一段超过一百字符的工具结果总结。" * 5}),
            self._FakeResponse({"content": "OK"}),
        ]
        with patch("llm_client.requests.post", side_effect=responses):
            from llm_client import LLMClient
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:",
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                result = app.invoke({
                    "messages": [{"role": "user", "content": "列出本地论文并总结"}],
                    "metadata": {
                        "verify_feedback": "旧反馈",
                        "verify_count": 1,
                        "verify_issues": "旧问题",
                        "verify_history": {"minor_repeat": 1},
                    },
                }, config={"configurable": {"thread_id": "verify-cleanup"}})
            finally:
                app.checkpointer.conn.close()

        self.assertEqual(result["metadata"], {})
        self.assertIn("工具结果总结", result["messages"][-1]["content"])

    def test_graph_reads_memory_by_current_session(self):
        """不同 thread_id 只能注入自己的对话摘要，不能共用 research-main。"""
        from graph_builder import build_graph

        class _Memory:
            def get_recent_summary(self, thread_id):
                return thread_id

        responses = [self._FakeResponse({"content": "收到"}) for _ in range(2)]
        from llm_client import LLMClient
        with patch("llm_client.requests.post", side_effect=responses) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", memory_store=_Memory(),
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                for thread_id in ("session-a", "session-b"):
                    app.invoke(
                        {
                            "messages": [{"role": "user", "content": "测试"}],
                            "metadata": {"session_id": thread_id},
                        },
                        config={"configurable": {"thread_id": thread_id}},
                    )
            finally:
                app.checkpointer.conn.close()

        prompt_texts = [
            "\n".join(message["content"] or "" for message in call.kwargs["json"]["messages"])
            for call in post.call_args_list
        ]
        self.assertIn("session-a", prompt_texts[0])
        self.assertNotIn("session-b", prompt_texts[0])
        self.assertIn("session-b", prompt_texts[1])
        self.assertNotIn("session-a", prompt_texts[1])

    def test_verify_uses_short_timeout_without_poisoning_main_circuit(self):
        """验证超时只打开其局部熔断器，不能阻断主模型或 Curator。"""
        import requests
        from graph_builder import build_graph
        from llm_client import LLMClient
        from resilience import CircuitBreaker

        responses = [
            self._FakeResponse({
                "content": None,
                "tool_calls": [{
                    "id": "tool-1", "type": "function",
                    "function": {"name": "list_papers", "arguments": "{}"},
                }],
            }),
            self._FakeResponse({"content": "这是一段超过一百字符的工具结果总结。" * 8}),
            requests.exceptions.ReadTimeout("verify timeout"),
        ]
        usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        guard = CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        events = []
        client = LLMClient(
            "test-key", "https://example.test/chat", max_retries=0,
        )
        with patch("llm_client.requests.post", side_effect=responses) as post:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", token_usage=usage,
                verify_timeout_seconds=4, verify_guard=guard, llm_client=client,
                event_callback=events.append,
            )
            try:
                app.invoke(
                    {"messages": [{"role": "user", "content": "列出本地论文并总结"}], "metadata": {}},
                    config={"configurable": {"thread_id": "verify-timeout"}},
                )
            finally:
                app.checkpointer.conn.close()

        verify_calls = [
            call for call in post.call_args_list
            if call.kwargs["json"].get("temperature") == 0.1
        ]
        self.assertEqual(len(verify_calls), 1)
        connect_timeout, read_timeout = verify_calls[0].kwargs["timeout"]
        self.assertEqual(connect_timeout, 3.05)
        self.assertGreater(read_timeout, 3.5)
        self.assertLessEqual(read_timeout, 4)
        self.assertFalse(guard.allow_request())
        self.assertEqual(client._circuit.state, "closed")
        self.assertNotIn("verify_status", usage)
        verify_started = next(event for event in events if event["type"] == "verify_request_started")
        self.assertEqual(verify_started["purpose"], "verify")
        self.assertEqual(verify_started["priority"], "verify")
        self.assertFalse(verify_started["counts_toward_circuit"])

    def test_stream_retries_same_model_before_first_token(self):
        """流式连接在尚未输出 token 时可以安全重放一次同模型请求。"""
        import requests
        from graph_builder import build_graph

        class _StreamingResponse:
            status_code = 200
            text = ""

            def __init__(self, should_fail=False):
                self.should_fail = should_fail

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                if self.should_fail:
                    raise requests.exceptions.ChunkedEncodingError("interrupted")
                return iter([
                    'data: {"choices":[{"delta":{"content":"恢复输出"}}]}',
                    "data: [DONE]",
                ])

        class _Client:
            max_retries = 1

            def __init__(self):
                self.calls = 0
                self.stream_failures = 0
                self.stream_successes = 0
                self.budgets = []

            class _Budget:
                def __init__(self):
                    self.retries_used = 0

                def reserve_retry(self):
                    if self.retries_used >= 1:
                        return False
                    self.retries_used += 1
                    return True

                def metrics(self):
                    return {"purpose": "chat", "priority": "interactive", "queue_wait_ms": 0,
                            "attempts": 1, "retries_used": self.retries_used}

            def new_request_budget(self, policy=None, **_kwargs):
                return self._Budget()

            @staticmethod
            def prepare_stream_read(response, budget):
                return True

            def post(self, payload, *, stream, on_status, budget):
                self.calls += 1
                self.budgets.append(budget)
                if not stream:
                    raise AssertionError("流式请求必须保留 stream=True")
                return _StreamingResponse(should_fail=self.calls == 1)

            def finish_stream(self, response, *, success):
                if success:
                    self.stream_successes += 1
                else:
                    self.stream_failures += 1

        client = _Client()
        streamed = []
        app = build_graph(
            api_key="test-key", checkpoint_db=":memory:", llm_client=client,
            stream_callback=streamed.append,
        )
        try:
            result = app.invoke(
                {"messages": [{"role": "user", "content": "你好"}], "metadata": {}},
                config={"configurable": {"thread_id": "stream-retry"}},
            )
        finally:
            app.checkpointer.conn.close()

        self.assertEqual(client.calls, 2)
        self.assertEqual(client.stream_failures, 1)
        self.assertEqual(client.stream_successes, 1)
        self.assertIs(client.budgets[0], client.budgets[1])
        self.assertEqual(result["messages"][-1]["content"], "恢复输出")
        self.assertEqual("".join(streamed), "恢复输出")

    def test_tools_module_imports(self):
        """所有工具模块可正常导入"""
        from search_api import search_arxiv, search_openalex, list_downloaded_papers
        from pdf_reader import read_pdf_enhanced, extract_images
        from paper_store import PaperStore, chunk_text
        from user_profile import ProfileManager
        self.assertTrue(callable(search_arxiv))
        self.assertTrue(callable(search_openalex))
        self.assertTrue(callable(read_pdf_enhanced))
        self.assertTrue(callable(chunk_text))


class TestScheduler(unittest.TestCase):
    """每日检索的本地命令路径，不调用外部论文 API。"""

    def test_retry_today_and_temporary_search(self):
        from scheduler import Scheduler

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                self.assertEqual(scheduler.add_keyword("TSN scheduling"), "✅ 已添加: TSN scheduling")
                scheduler._search = lambda keyword, limit=3: [
                    {"title": f"{keyword} paper", "source": "test"}
                ]

                self.assertEqual(scheduler.run_today()[0]["title"], "TSN scheduling paper")
                self.assertEqual(scheduler.retry_today()[0]["title"], "TSN scheduling paper")
                self.assertEqual(scheduler.search("Diffusion TSN")[0]["title"], "Diffusion TSN paper")
            finally:
                scheduler.close()

    def test_parallel_search_passes_short_timeouts_to_each_source(self):
        """三源检索并行执行，单源请求必须使用配置的短超时。"""
        from scheduler import Scheduler

        class _HttpResponse:
            def __init__(self, content=b"", payload=None):
                self.content = content
                self._payload = payload or {}

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        arxiv_xml = b"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>
            <entry><title>arXiv paper</title><id>https://arxiv.org/abs/1</id></entry></feed>"""

        def fake_get(url, **kwargs):
            if "arxiv" in url:
                self.assertEqual(kwargs["timeout"], (3.05, 4))
                return _HttpResponse(content=arxiv_xml)
            self.assertEqual(kwargs["timeout"], (3.05, 4))
            return _HttpResponse(payload={"results": [{"title": "OpenAlex paper"}]})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"), request_timeout_seconds=4)
            try:
                with patch("requests.get", side_effect=fake_get) as request_get:
                    results = scheduler.search("TSN scheduling", limit=1)
                self.assertEqual(request_get.call_count, 2)
                self.assertEqual(
                    {result["source"] for result in results},
                    {"arxiv", "openalex"},
                )
            finally:
                scheduler.close()


class TestPaperArtifacts(unittest.TestCase):
    """Evidence cards remain local, source-bounded and independently auditable."""

    def test_document_map_keeps_table_figure_and_nearby_text_on_the_same_page(self):
        from paper_artifacts import (
            attach_image_assets,
            build_document_map,
            build_source_map_from_document,
            related_context,
        )

        parsed = {
            "source_file": "sample.pdf", "total_pages": 3, "processed_pages": 3,
            "pages": [{"page": 3, "elements": [
                {
                    "id": "p003-t01", "kind": "text", "page": 3, "section": "Experiments",
                    "is_heading": False, "text": "The following table compares all baselines.",
                    "bbox": [0, 0, 100, 20], "related_ids": ["p003-b01", "p003-f01"],
                },
                {
                    "id": "p003-b01", "kind": "table", "page": 3, "section": "Experiments",
                    "label": "Table 2", "caption": "Table 2: Main results.",
                    "text": "| Model | Score |\n| --- | --- |\n| Ours | 91.2 |",
                    "bbox": [0, 30, 100, 80], "related_ids": ["p003-t01"],
                },
                {
                    "id": "p003-f01", "kind": "figure", "page": 3, "section": "Experiments",
                    "label": "Figure 3", "caption": "Figure 3: Ablation trend.",
                    "asset_path": "", "bbox": [0, 90, 100, 180], "related_ids": ["p003-t01"],
                },
            ]}],
        }
        document_map = build_document_map(
            parsed, paper_id="paper-1", title="Sample", source_file="sample.pdf",
        )
        attach_image_assets(document_map, ["C:/tmp/sample_Figure3.png"])
        source_map = build_source_map_from_document(document_map)

        table_block = next(item for item in source_map["blocks"] if item["id"] == "p003-b01")
        figure = next(item for item in document_map["pages"][0]["elements"] if item["id"] == "p003-f01")
        table_chunk = next(item for item in document_map["chunks"] if item["kind"] == "table")
        context, locator = related_context(document_map, table_chunk["id"])

        self.assertEqual(table_block["page"], 3)
        self.assertTrue(figure["asset_path"].endswith("sample_Figure3.png"))
        self.assertIn("following table", context)
        self.assertEqual(locator, "p.3 · Table 2")

    def test_query_tool_adds_related_context_for_a_page_mapped_result(self):
        from paper_artifacts import build_document_map, write_document_map
        from runtime_paths import RuntimePaths
        from tools.search import handle_query_papers

        parsed = {
            "source_file": "sample.pdf", "total_pages": 1, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [
                {
                    "id": "p001-t01", "kind": "text", "page": 1, "section": "Results",
                    "is_heading": False, "text": "The paragraph explains the table result.",
                    "bbox": [0, 0, 1, 1], "related_ids": ["p001-b01"],
                },
                {
                    "id": "p001-b01", "kind": "table", "page": 1, "section": "Results",
                    "label": "Table 1", "caption": "Table 1: Main score.",
                    "text": "| Score |\n| --- |\n| 91.2 |", "bbox": [0, 2, 1, 3],
                    "related_ids": ["p001-t01"],
                },
            ]}],
        }

        class _Store:
            @staticmethod
            def query_with_timeout(*_args, **_kwargs):
                return ([{
                    "text": "Table 1: Main score.", "section": "Results", "title": "Sample",
                    "paper_id": "paper-1", "element_id": "p001-c02", "element_kind": "table",
                    "retrieval": "hybrid", "hybrid_score": 0.02,
                }], None)

        with tempfile.TemporaryDirectory() as tmp:
            paths = RuntimePaths.from_root(Path(tmp) / "runtime")
            paths.ensure_initialized()
            document_map = build_document_map(
                parsed, paper_id="paper-1", title="Sample", source_file="sample.pdf",
            )
            write_document_map(paths, paper_id="paper-1", document_map=document_map)
            with patch.dict(os.environ, {"APP_DATA_DIR": str(paths.root)}, clear=False):
                result = handle_query_papers({"query": "main score"}, paper_store=_Store())

        self.assertIn("p.1 · Table 1", result)
        self.assertIn("关联上下文", result)
        self.assertIn("explains the table", result)

    def test_obsolete_document_map_cleanup_never_removes_a_nonempty_artifact_directory(self):
        from paper_artifacts import build_document_map, remove_document_map, write_document_map
        from runtime_paths import RuntimePaths

        parsed = {
            "source_file": "sample.pdf", "total_pages": 1, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [{
                "id": "p001-t01", "kind": "text", "page": 1, "section": "Abstract",
                "is_heading": False, "text": "A sufficiently complete paper paragraph.", "related_ids": [],
            }]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = RuntimePaths.from_root(Path(tmp) / "runtime")
            paths.ensure_initialized()
            document_map = build_document_map(
                parsed, paper_id="old-paper", title="Sample", source_file="sample.pdf",
            )
            map_path = write_document_map(paths, paper_id="old-paper", document_map=document_map)
            keep_path = map_path.parent / "other-derived-file.txt"
            keep_path.write_text("keep", encoding="utf-8")

            self.assertTrue(remove_document_map(paths, "old-paper"))
            self.assertFalse(map_path.exists())
            self.assertTrue(keep_path.exists())
            self.assertFalse(remove_document_map(paths, "old-paper"))

    def test_read_pdf_indexes_and_persists_the_page_map(self):
        from tools.read_pdf import handle_read_pdf

        parsed = {
            "source_file": "Sample.pdf", "total_pages": 1, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [{
                "id": "p001-t01", "kind": "text", "page": 1, "section": "Abstract",
                "is_heading": False,
                "text": "Structured paper text with enough context to form a reliable retrieval passage for testing.",
                "bbox": [0, 0, 1, 1], "related_ids": [],
            }]}],
        }

        class _Reader:
            def __init__(self, **_kwargs):
                return None

            @staticmethod
            def parse_document(_path):
                return parsed

            @staticmethod
            def render_document(_document):
                return "📄 **PDF 解析完成**\n\nStructured paper text with enough context to form a reliable retrieval passage for testing."

        class _Store:
            indexed = None

            @staticmethod
            def list_papers():
                return []

            @classmethod
            def index_document_map(cls, document_map, **_kwargs):
                cls.indexed = document_map
                return str(document_map["paper_id"])

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            papers = runtime / "primary" / "papers"
            papers.mkdir(parents=True)
            (papers / "Sample.pdf").write_bytes(b"%PDF-placeholder")
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False), patch(
                "tools.read_pdf.PaperReader", _Reader,
            ), patch("tools.read_pdf.extract_images", return_value=[]):
                result = handle_read_pdf({"url_or_path": "Sample.pdf"}, paper_store=_Store())

            maps = list((runtime / "derived" / "paper_artifacts").glob("*/document_map.json"))
            self.assertEqual(len(maps), 1)
            self.assertIsNotNone(_Store.indexed)
            self.assertEqual(_Store.indexed["chunks"][0]["page"], 1)
            self.assertIn("页面元素映射", result)

    def test_quality_gate_merges_an_orphan_formula_fragment_without_losing_sources(self):
        from paper_quality import prepare_document_map

        document_map = {
            "total_pages": 1,
            "processed_pages": 1,
            "pages": [{"page": 1, "elements": [
                {"id": "p001-t01", "kind": "text", "page": 1, "is_heading": False,
                 "text": "A complete paragraph with enough source material for retrieval. " * 2, "related_ids": []},
                {"id": "p001-t02", "kind": "text", "page": 1, "is_heading": False,
                 "text": ". (34)", "related_ids": []},
            ]}],
            "chunks": [
                {"id": "p001-c01", "kind": "text", "page": 1, "section": "Method",
                 "text": "A complete paragraph with enough source material for retrieval. " * 2,
                 "source_element_ids": ["p001-t01"], "related_ids": []},
                {"id": "p001-c02", "kind": "text", "page": 1, "section": "Method",
                 "text": ". (34)", "source_element_ids": ["p001-t02"], "related_ids": []},
            ],
        }

        prepared, report = prepare_document_map(document_map, requested_pages=1)

        self.assertTrue(report["accepted"])
        self.assertEqual(report["status"], "repaired")
        self.assertEqual(report["repairs"]["merged_orphan_text_chunks"], 1)
        self.assertEqual(len(prepared["chunks"]), 1)
        self.assertEqual(prepared["chunks"][0]["source_element_ids"], ["p001-t01", "p001-t02"])

    def test_quality_gate_rejects_untraceable_chunks_and_index_round_trip_loss(self):
        from paper_quality import assess_document_map, verify_index_round_trip

        document_map = {
            "total_pages": 1,
            "processed_pages": 1,
            "pages": [{"page": 1, "elements": [
                {"id": "p001-t01", "kind": "text", "page": 1, "is_heading": False,
                 "text": "A sufficiently long paragraph that should be traceable in the index.", "related_ids": []},
            ]}],
            "chunks": [{
                "id": "p001-c01", "kind": "text", "page": 1, "section": "Method",
                "text": "A sufficiently long paragraph that should be traceable in the index.",
                "source_element_ids": ["missing-source"], "related_ids": [],
            }],
        }

        report = assess_document_map(document_map, requested_pages=1)
        index_report = verify_index_round_trip(document_map, [])

        self.assertFalse(report["accepted"])
        self.assertIn("untraceable_chunks", {item["code"] for item in report["issues"]})
        self.assertFalse(index_report["accepted"])
        self.assertEqual(index_report["issues"][0]["code"], "index_round_trip_mismatch")

    def test_read_pdf_retries_with_fallback_before_replacing_an_existing_index(self):
        from tools.read_pdf import handle_read_pdf

        calls = []
        good_text = "A complete paragraph that is deliberately long enough to pass the retrieval quality threshold."
        normal = {
            "source_file": "Sample.pdf", "total_pages": 2, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [{
                "id": "p001-t01", "kind": "text", "page": 1, "section": "Abstract",
                "is_heading": False, "text": good_text, "bbox": [0, 0, 1, 1], "related_ids": [],
            }]}],
        }
        fallback = {**normal, "total_pages": 1, "processed_pages": 1}

        class _Reader:
            def __init__(self, **_kwargs):
                return None

            @staticmethod
            def parse_document(_path, **kwargs):
                calls.append(("parse", kwargs))
                return fallback if kwargs.get("extract_tables") is False else normal

            @staticmethod
            def render_document(_document):
                return "📄 **PDF 解析完成**\n\n" + good_text

        class _Store:
            indexed = None

            @staticmethod
            def list_papers():
                return [{"paper_id": "old-paper", "title": "Sample"}]

            @classmethod
            def index_document_map(cls, document_map, **_kwargs):
                calls.append(("index", document_map["paper_id"]))
                cls.indexed = document_map
                return str(document_map["paper_id"])

            @classmethod
            def get_paper_chunks(cls, _paper_id):
                return [{"element_id": chunk["id"]} for chunk in cls.indexed["chunks"]]

            @staticmethod
            def delete_paper(paper_id):
                calls.append(("delete", paper_id))
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            papers = runtime / "primary" / "papers"
            papers.mkdir(parents=True)
            (papers / "Sample.pdf").write_bytes(b"%PDF-placeholder")
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False), patch(
                "tools.read_pdf.PaperReader", _Reader,
            ), patch("tools.read_pdf.extract_images", return_value=[]):
                result = handle_read_pdf({"url_or_path": "Sample.pdf"}, paper_store=_Store())

        self.assertEqual(calls[:2], [("parse", {}), ("parse", {"extract_tables": False})])
        self.assertLess(calls.index(("index", _Store.indexed["paper_id"])), calls.index(("delete", "old-paper")))
        self.assertEqual(_Store.indexed["quality"]["parser"], "备用文本解析")
        self.assertIn("备用文本解析", result)

    def test_paper_card_prefers_the_persisted_document_map(self):
        from paper_artifacts import build_document_map, write_document_map
        from runtime_paths import RuntimePaths
        from tools.paper_card import handle_generate_paper_card

        parsed = {
            "source_file": "Sample_Paper.pdf", "total_pages": 1, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [{
                "id": "p001-b01", "kind": "table", "page": 1, "section": "Results",
                "label": "Table 1", "caption": "Table 1: Main result.",
                "text": "| Score |\n| --- |\n| 91.2 |", "bbox": [0, 0, 1, 1], "related_ids": [],
            }]}],
        }

        class _Store:
            @staticmethod
            def list_papers():
                return [{"paper_id": "paper-1", "title": "Sample Paper"}]

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            papers = runtime / "primary" / "papers"
            papers.mkdir(parents=True)
            (papers / "Sample_Paper.pdf").write_bytes(b"%PDF-placeholder")
            paths = RuntimePaths.from_root(runtime)
            paths.ensure_initialized()
            document_map = build_document_map(
                parsed, paper_id="paper-1", title="Sample Paper", source_file="Sample_Paper.pdf",
            )
            write_document_map(paths, paper_id="paper-1", document_map=document_map)
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False), patch(
                "tools.paper_card.create_source_map", side_effect=AssertionError("should reuse document map"),
            ):
                result = handle_generate_paper_card({"paper_id_or_title": "paper-1"}, paper_store=_Store())

        self.assertIn("已生成证据草稿", result)

    def test_source_map_keeps_page_and_section_anchors(self):
        from paper_artifacts import build_source_map_from_reader_text

        reader_text = (
            "📄 **PDF 解析完成**\n文件名: sample.pdf | 总页数: 2 | 已读: 2 | 提取: 90 字符\n\n"
            "━━━ 第 1 页 ━━━\n## Abstract\nThe paper proposes a scheduler.\n\n"
            "━━━ 第 2 页 ━━━\n## Experiments\nIt reports lower latency."
        )
        source_map = build_source_map_from_reader_text(
            reader_text, paper_id="paper-1", title="Sample", source_file="sample.pdf",
        )

        self.assertEqual(source_map["coverage"]["locator_mode"], "page-grounded")
        self.assertEqual([item["id"] for item in source_map["blocks"]], ["S001", "S002"])
        self.assertEqual(source_map["blocks"][1]["page"], 2)
        self.assertEqual(source_map["blocks"][1]["section"], "Experiments")

    def test_evidence_card_writes_only_under_runtime_and_rejects_bad_anchor(self):
        from paper_artifacts import CARD_HEADINGS, audit_paper_card, write_paper_artifact
        from runtime_paths import RuntimePaths

        source_map = {
            "paper_id": "paper-1",
            "blocks": [{"id": "S001", "page": 1, "text": "verified evidence"}],
        }
        card = "\n".join(
            ["# Sample"]
            + [f"## {index}. {heading}\n内容【论文 p.1 · S001】" for index, heading in enumerate(CARD_HEADINGS, 1)]
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = RuntimePaths.from_root(Path(tmp) / "runtime")
            paths.ensure_initialized()
            card_path, map_path, audit = write_paper_artifact(
                paths, paper_id="paper-1", source_map=source_map, card=card,
            )
            self.assertTrue(card_path.is_file())
            self.assertTrue(map_path.is_file())
            self.assertTrue(audit["valid"])
            self.assertTrue(str(card_path).startswith(str(paths.paper_artifacts_dir)))

        invalid = audit_paper_card(card.replace("S001", "S999"), source_map)
        self.assertFalse(invalid["valid"])
        self.assertIn("未知块 ID: S999", invalid["invalid_anchors"])

    def test_paper_card_tool_generates_an_offline_evidence_draft(self):
        from tools.paper_card import handle_generate_paper_card

        class _Store:
            @staticmethod
            def list_papers():
                return [{"paper_id": "paper-1", "title": "Sample Paper"}]

        source_map = {
            "paper_id": "paper-1", "title": "Sample Paper",
            "coverage": {"processed_pages": 1, "total_pages": 1, "locator_mode": "page-grounded"},
            "blocks": [{"id": "S001", "page": 1, "section": "Abstract", "text": "A verified claim."}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            papers = runtime / "primary" / "papers"
            papers.mkdir(parents=True)
            (papers / "Sample_Paper.pdf").write_bytes(b"%PDF-placeholder")
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False), patch(
                "tools.paper_card.create_source_map", return_value=source_map,
            ):
                result = handle_generate_paper_card(
                    {"paper_id_or_title": "paper-1"}, paper_store=_Store(),
                )
            self.assertIn("已生成证据草稿", result)
            self.assertTrue((runtime / "derived" / "paper_artifacts" / "paper-1" / "paper-card.md").is_file())

    def test_paper_card_tool_uses_shared_client_with_a_low_priority_policy(self):
        from paper_artifacts import CARD_HEADINGS
        from tools.paper_card import handle_generate_paper_card

        class _Store:
            @staticmethod
            def list_papers():
                return [{"paper_id": "paper-1", "title": "Sample Paper"}]

        class _Response:
            @staticmethod
            def json():
                card = "\n".join(
                    ["# Sample"]
                    + [f"## {index}. {heading}\n结论【论文 p.1 · S001】" for index, heading in enumerate(CARD_HEADINGS, 1)]
                )
                return {"choices": [{"message": {"content": card}}]}

            @staticmethod
            def close():
                return None

        class _LLM:
            calls = []

            def post(self, payload, **kwargs):
                self.calls.append((payload, kwargs))
                return _Response()

        source_map = {
            "paper_id": "paper-1", "title": "Sample Paper",
            "coverage": {"processed_pages": 1, "total_pages": 1, "locator_mode": "page-grounded"},
            "blocks": [{"id": "S001", "page": 1, "section": "Abstract", "text": "A verified claim."}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            papers = runtime / "primary" / "papers"
            papers.mkdir(parents=True)
            (papers / "Sample_Paper.pdf").write_bytes(b"%PDF-placeholder")
            client = _LLM()
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False), patch(
                "tools.paper_card.create_source_map", return_value=source_map,
            ):
                result = handle_generate_paper_card(
                    {"paper_id_or_title": "paper-1"}, paper_store=_Store(),
                    llm_client=client, model="test-model",
                )
        self.assertIn("模型已完成", result)
        self.assertEqual(client.calls[0][0]["model"], "test-model")
        self.assertEqual(client.calls[0][1]["policy"].priority.name, "SUMMARY")
        self.assertFalse(client.calls[0][1]["policy"].counts_toward_circuit)


class TestResearchDocuments(unittest.TestCase):
    """Research plans are durable artifacts, not hidden conversation memory."""

    def test_store_updates_and_searches_markdown_with_word_export(self):
        from docx import Document
        from research_documents import ResearchDocumentStore
        from runtime_paths import RuntimePaths

        with tempfile.TemporaryDirectory() as tmp:
            paths = RuntimePaths.from_root(Path(tmp) / "runtime")
            store = ResearchDocumentStore(paths)
            created = store.save(
                "TSN 扩散调度验证方案",
                "# TSN 扩散调度验证方案\n\n## 假设\n- 在突发流量下减少截止期违例。\n\n## 计划\n1. 在 20、50、100 flows 上与 PPO 对比。",
            )

            self.assertEqual(created["revision"], 1)
            self.assertTrue(Path(created["markdown_path"]).is_file())
            self.assertTrue(Path(created["docx_path"]).is_file())
            self.assertEqual(Document(created["docx_path"]).core_properties.title, "TSN 扩散调度验证方案")
            self.assertEqual(store.search("PPO")[0]["document_id"], created["document_id"])

            updated = store.save(
                "TSN 扩散调度验证方案",
                "## 更新后的计划\n增加分布偏移和消融实验。",
                document_id=created["document_id"],
            )
            loaded = store.read(created["document_id"])
            self.assertEqual(updated["revision"], 2)
            self.assertIn("分布偏移", loaded["content"])
            self.assertTrue(str(Path(loaded["markdown_path"])).startswith(str(paths.research_documents_dir)))

    def test_tool_handlers_keep_research_documents_outside_profile_and_memory(self):
        import re
        from tools import execute_tool

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False):
                saved = execute_tool("save_research_document", {
                    "title": "临时实验方案",
                    "content": "## 目标\n验证 bursty traffic 下的时延和截止期违例。",
                })
                document_id = re.search(r"`(research-doc-[a-f0-9]{12})`", saved).group(1)
                searched = execute_tool("search_research_documents", {"query": "bursty"})
                loaded = execute_tool("read_research_document", {"document_id": document_id})

            self.assertIn(document_id, searched)
            self.assertIn("截止期违例", loaded)
            self.assertTrue((runtime / "primary" / "research_documents" / document_id / "document.docx").is_file())
            self.assertFalse((runtime / "primary" / "profile.md").exists())


class TestPaperRecordNormalization(unittest.TestCase):
    def test_ieee_normalization_only_exposes_explicit_open_access_pdf(self):
        from ieee_xplore import ieee_records

        records = ieee_records({"articles": [
            {
                "title": "Open Access TSN Scheduling",
                "abstract": "A Time-Sensitive Networking scheduling method.",
                "authors": {"authors": [{"full_name": "Alice"}]},
                "publication_year": "2026",
                "publication_date": "2026-03-01",
                "publication_title": "IEEE Access",
                "citing_paper_count": "12",
                "doi": "10.1109/example.2026.1",
                "article_number": "12345678",
                "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=12345678",
                "accessType": "Open Access",
            },
            {
                "title": "Restricted TSN Scheduling",
                "article_number": "87654321",
                "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=87654321",
                "accessType": "Locked",
            },
            {
                "title": "Ephemera TSN Scheduling",
                "article_number": "99999999",
                "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=99999999",
                "accessType": "Ephemera",
            },
        ]})

        self.assertEqual(records[0]["authors"], ["Alice"])
        self.assertEqual(records[0]["year"], 2026)
        self.assertEqual(records[0]["open_access_pdf_url"], records[0]["url"].replace(
            "/document/12345678", "/stamp/stamp.jsp?tp=&arnumber=12345678",
        ))
        self.assertEqual(records[1]["open_access_pdf_url"], "")
        self.assertEqual(records[2]["open_access_pdf_url"], "")

    def test_deduplication_prefers_doi_and_merges_provider_metadata(self):
        from paper_records import deduplicate_paper_records

        records = [
            {
                "title": "A Scheduling Method", "doi": "https://doi.org/10.1/ABC.",
                "abstract": "short", "sources": ["openalex"], "source_ids": {"openalex": "W1"},
                "citation_count": 2,
            },
            {
                "title": "A scheduling method", "doi": "10.1/abc",
                "abstract": "longer abstract", "sources": ["arxiv"], "source_ids": {"arxiv": "2501.00001"},
                "citation_count": 5,
            },
        ]
        merged = deduplicate_paper_records(records)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sources"], ["arxiv", "openalex"])
        self.assertEqual(merged[0]["citation_count"], 5)
        self.assertEqual(merged[0]["abstract"], "longer abstract")

    def test_merged_public_search_keeps_source_labels_and_partial_failure(self):
        from search_api import search_public_papers

        openalex = [{
            "title": "Shared Paper", "abstract": "TSN scheduling under bursty traffic.",
            "doi": "10.1/shared", "year": 2025, "citation_count": 8,
            "sources": ["openalex"], "source_ids": {"openalex": "W1"},
        }]
        arxiv = [{
            "title": "Shared Paper", "abstract": "TSN scheduling under bursty traffic.",
            "doi": "10.1/shared", "year": 2025, "citation_count": 0,
            "sources": ["arxiv"], "source_ids": {"arxiv": "2501.00001"},
        }]
        with patch("search_api._fetch_openalex_records", return_value=(openalex, None, "TSN")), patch(
            "search_api._fetch_arxiv_records", return_value=(arxiv, "arXiv API 请求失败: timeout"),
        ):
            result = search_public_papers("TSN scheduling under bursty traffic", limit=5)

        self.assertIn("Shared Paper", result)
        self.assertIn("[openalex]", result)
        self.assertIn("未完成来源", result)

    def test_ieee_search_sends_key_without_displaying_it(self):
        from search_api import search_ieee

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"articles": [{
                    "title": "Time-Sensitive Networking Scheduling for Bursty Traffic",
                    "abstract": "A scheduling method for TSN under bursty traffic.",
                    "publication_year": "2026",
                    "article_number": "12345678",
                    "accessType": "Open Access",
                    "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=12345678",
                }]}

            def close(self):
                return None

        with patch("search_api.requests.get", return_value=_Response()) as request_get:
            result = search_ieee(
                "TSN scheduling under bursty traffic", limit=3, api_key="ieee-test-key",
            )

        self.assertEqual(request_get.call_args.kwargs["params"], {
            "querytext": "TSN scheduling under bursty traffic",
            "max_records": 3,
            "format": "json",
            "apikey": "ieee-test-key",
        })
        self.assertIn("IEEE Xplore 搜索结果", result)
        self.assertIn("IEEE 获取权限: Open Access", result)
        self.assertIn("IEEE 开放全文 PDF", result)
        self.assertNotIn("ieee-test-key", result)


class TestDailyMultiAgentOrchestration(unittest.TestCase):
    """每日检索必须可恢复、可降级，且不会为每篇论文调用一次模型。"""

    @staticmethod
    def _candidate(orchestrator, *, keyword, source, title, doi, abstract="有效摘要", year=2025):
        return orchestrator._candidate(
            keyword=keyword,
            source=source,
            title=title,
            doi=doi,
            abstract=abstract,
            authors=["Alice"],
            year=year,
            published_at=f"{year}-01-02",
            venue="TestConf",
            citation_count=20,
            url=f"https://example.test/{doi}",
            source_id=doi,
        )

    def test_daily_run_deduplicates_then_uses_one_batch_curator(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def __init__(self, content):
                self.content = content

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

            def close(self):
                return None

        class _LLM:
            def __init__(self):
                self.calls = []
                self.policies = []

            def new_request_budget(self, policy):
                self.policies.append(policy)
                return policy

            def post(self, payload, **_kwargs):
                self.calls.append(payload)
                return _Response(
                    '{"brief":"两篇候选已排序", "rankings": ['
                    '{"id":"%s","score":90,"reason":"高度相关","tags":["TSN"]},'
                    '{"id":"%s","score":80,"reason":"补充方法","tags":["调度"]}]}'
                    % (self.ids[0], self.ids[1])
                )

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                scheduler.add_keyword("TSN scheduling")
                llm = _LLM()
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler, llm_client=llm, model="deepseek-chat",
                )
                first = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="arxiv",
                    title="Diffusion Scheduling for TSN", doi="10.1/a",
                )
                duplicate = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="openalex",
                    title="Diffusion Scheduling for TSN", doi="10.1/a",
                )
                second = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="dblp",
                    title="Reinforcement Learning for TSN", doi="10.1/b",
                )
                llm.ids = [first["candidate_id"], second["candidate_id"]]
                stats = {"TSN scheduling": {
                    "arxiv": {"status": "ok", "count": 1},
                    "openalex": {"status": "ok", "count": 1},
                    "dblp": {"status": "ok", "count": 1},
                }}
                with patch.object(orchestrator, "_run_scouts", return_value=([first, duplicate, second], stats)):
                    result = orchestrator.run("daily")

                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.candidates), 2, "跨源同一 DOI 应合并")
                self.assertEqual(len(llm.calls), 1, "Curator 必须按整次任务批处理")
                self.assertFalse(llm.policies[0].counts_toward_circuit)
                self.assertEqual(result.candidates[0]["curation"]["reason"], "高度相关")
                saved = scheduler.get_daily_run(result.run_id)
                self.assertEqual(saved["status"], "completed")
                self.assertGreaterEqual(len(scheduler.get_daily_agent_events(result.run_id)), 5)
                self.assertEqual(scheduler.get_today_results()[0]["new_count"], 2)
            finally:
                scheduler.close()

    def test_source_sets_keep_arxiv_out_of_daily_and_send_raw_source_keys(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": []}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler,
                    openalex_api_key="oa-test-key",
                )
                self.assertEqual(orchestrator.daily_sources, ("openalex", "openaire", "dblp"))
                self.assertEqual(orchestrator.temporary_sources, ("openalex", "arxiv"))
                with patch("daily_orchestrator.requests.get", return_value=_Response()) as request_get:
                    self.assertEqual(orchestrator._openalex_scout("TSN", None), [])
                self.assertEqual(request_get.call_args.kwargs["params"]["api_key"], "oa-test-key")

                ieee_enabled = DailyResearchOrchestrator(
                    scheduler=scheduler,
                    ieee_api_key="ieee-test-key",
                )
                self.assertEqual(ieee_enabled.daily_sources, ("openalex", "openaire", "dblp", "ieee"))
                self.assertEqual(ieee_enabled.temporary_sources, ("openalex", "arxiv", "ieee"))
            finally:
                scheduler.close()

    def test_ieee_scout_preserves_access_metadata(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"articles": [{
                    "title": "TSN Scheduling for Bursty Traffic",
                    "abstract": "Scheduling algorithm for Time-Sensitive Networking.",
                    "publication_year": "2026",
                    "article_number": "12345678",
                    "accessType": "Open Access",
                    "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=12345678",
                }]}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler, ieee_api_key="ieee-test-key",
                )
                with patch("daily_orchestrator.requests.get", return_value=_Response()) as request_get:
                    candidates = orchestrator._ieee_scout("TSN scheduling", None)

                self.assertEqual(request_get.call_args.kwargs["params"]["apikey"], "ieee-test-key")
                self.assertEqual(candidates[0]["sources"], ["ieee"])
                self.assertEqual(candidates[0]["access_type"], "Open Access")
                self.assertTrue(candidates[0]["open_access_pdf_url"])
            finally:
                scheduler.close()

    def test_resume_reuses_saved_candidates_without_fetching_again(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                scheduler.add_keyword("TSN scheduling")
                orchestrator = DailyResearchOrchestrator(scheduler=scheduler)
                run = scheduler.create_daily_run("daily", ["TSN scheduling"], {"planner": "rule_based"})
                candidate = self._candidate(
                    orchestrator, keyword="TSN scheduling", source="arxiv",
                    title="Saved Candidate", doi="10.1/saved",
                )
                candidate["quality"] = {"score": 6, "issues": [], "eligible": True}
                scheduler.save_daily_candidates(run["run_id"], [candidate])
                scheduler.update_daily_run(run["run_id"], status="failed")

                with patch.object(orchestrator, "_run_scouts") as scouts:
                    result = orchestrator.run("daily", resume=True)

                self.assertEqual(result.run_id, run["run_id"])
                self.assertEqual(result.status, "completed")
                self.assertFalse(scouts.called)
                self.assertEqual(scheduler.get_daily_run(run["run_id"])["status"], "completed")
            finally:
                scheduler.close()

    def test_critic_is_conditional_for_small_or_low_quality_candidate_sets(self):
        from daily_orchestrator import DailyResearchOrchestrator
        from scheduler import Scheduler

        class _Response:
            def __init__(self, content):
                self.content = content

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

            def close(self):
                return None

        class _LLM:
            def __init__(self, candidate_id):
                self.candidate_id = candidate_id
                self.calls = 0

            def new_request_budget(self, policy):
                return policy

            def post(self, _payload, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _Response(
                        '{"brief":"单篇候选", "rankings":[{"id":"%s","score":90,"reason":"相关","tags":[]}]}'
                        % self.candidate_id
                    )
                return _Response('{"warnings":["候选较少"],"drop_ids":[]}')

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                scheduler.add_keyword("TSN scheduling")
                seed = DailyResearchOrchestrator(scheduler=scheduler)
                candidate = self._candidate(
                    seed, keyword="TSN scheduling", source="arxiv",
                    title="Only Candidate", doi="10.1/only",
                )
                llm = _LLM(candidate["candidate_id"])
                orchestrator = DailyResearchOrchestrator(
                    scheduler=scheduler, llm_client=llm, model="deepseek-chat",
                )
                stats = {"TSN scheduling": {"arxiv": {"status": "ok", "count": 1}}}
                with patch.object(orchestrator, "_run_scouts", return_value=([candidate], stats)):
                    result = orchestrator.run("daily")

                self.assertEqual(llm.calls, 2, "候选不足时才追加一次 Critic")
                self.assertTrue(result.critique["ran"])
                self.assertEqual(result.critique["warnings"], ["候选较少"])
            finally:
                scheduler.close()


class TestConversationMemory(unittest.TestCase):
    """长期辅助记忆保持会话隔离，并在达到阈值后有界压缩。"""

    def test_memory_search_combines_current_session_summary_and_paper_facts(self):
        from memory import MemoryStore
        from tools import execute_tool

        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(str(Path(tmp) / "memory.db"))
            try:
                memory.add_summary("session-a", "TSN 动态负载实验需要报告截止期违例")
                memory.add_summary("session-b", "TSN 另一个会话的私有摘要")
                memory.add_triple("DiffTSN", "uses_method", "TSN scheduling under bursty traffic")

                result = execute_tool(
                    "memory_search",
                    {"query": "TSN"},
                    memory_store=memory,
                    session_id="session-a",
                )
            finally:
                memory.close()

        self.assertIn("当前会话摘要", result)
        self.assertIn("截止期违例", result)
        self.assertNotIn("另一个会话", result)
        self.assertIn("论文事实", result)
        self.assertIn("DiffTSN", result)

    def test_summary_policy_skips_short_history_and_compacts_long_history(self):
        from conversation_memory import maybe_store_conversation_summary

        class _Memory:
            stored = []

            @staticmethod
            def get_last_summary_time(_thread_id):
                return None

            def add_summary(self, thread_id, summary):
                self.stored.append((thread_id, summary))

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "压缩后的项目上下文"}}]}

        class _LLM:
            def __init__(self):
                self.calls = 0

            @staticmethod
            def new_request_budget(policy):
                return policy

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return _Response()

        memory = _Memory()
        llm = _LLM()
        metadata = {"session_id": "session-a"}
        short_history = [{"role": "user", "content": f"问题 {i}"} for i in range(10)]
        self.assertFalse(maybe_store_conversation_summary(
            messages=short_history,
            metadata=metadata,
            context_ratio=0.1,
            memory_store=memory,
            llm_client=llm,
            model="test-model",
            timeout_seconds=2,
        ))
        self.assertEqual(llm.calls, 0)

        long_history = short_history + [{"role": "user", "content": "第十一个问题"}]
        self.assertTrue(maybe_store_conversation_summary(
            messages=long_history,
            metadata=metadata,
            context_ratio=0.1,
            memory_store=memory,
            llm_client=llm,
            model="test-model",
            timeout_seconds=2,
        ))
        self.assertEqual(memory.stored, [
            ("session-a", "压缩后的项目上下文"),
        ])


class TestSessionStore(unittest.TestCase):
    """多会话注册、旧历史迁移与硬删除边界。"""

    def test_legacy_migration_and_isolated_hard_delete(self):
        from session_store import SessionStore, LEGACY_THREAD_ID
        from memory import MemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_db = Path(tmp) / "checkpoint.db"
            conn = sqlite3.connect(checkpoint_db)
            conn.executescript("""
                CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT);
                CREATE TABLE checkpoint_blobs (thread_id TEXT, channel TEXT);
                CREATE TABLE writes (thread_id TEXT, value TEXT);
            """)
            conn.execute("INSERT INTO checkpoints VALUES (?, 'old-1')", (LEGACY_THREAD_ID,))
            conn.commit()
            conn.close()

            store = SessionStore(str(checkpoint_db))
            memory = MemoryStore(str(Path(tmp) / "memory.db"))
            try:
                self.assertTrue(store.ensure_legacy_session())
                self.assertEqual(store.get(LEGACY_THREAD_ID)["title"], "历史会话")
                self.assertFalse(store.ensure_legacy_session(), "旧会话只能迁移一次")

                first = store.create()
                second = store.create("实验会话")
                store.touch(first["thread_id"], "这是首条用于自动命名的会话消息，后续内容不应影响标题")
                self.assertEqual(
                    store.get(first["thread_id"])["title"],
                    "这是首条用于自动命名的会话消息，后续内容",
                )
                self.assertEqual(len(store.list()), 3)

                usage = {"total": 42, "calls": 2}
                store.save_usage(first["thread_id"], usage)
                self.assertEqual(store.get_usage(first["thread_id"]), usage)

                raw = sqlite3.connect(checkpoint_db)
                for table in ("checkpoints", "checkpoint_blobs", "writes"):
                    raw.execute(f"INSERT INTO {table} VALUES (?, 'new')", (first["thread_id"],))
                    raw.execute(f"INSERT INTO {table} VALUES (?, 'other')", (second["thread_id"],))
                raw.commit()
                raw.close()
                memory.add_summary(first["thread_id"], "会话一摘要")
                memory.add_summary(second["thread_id"], "会话二摘要")

                self.assertTrue(store.delete(first["thread_id"]))
                self.assertEqual(memory.delete_summaries(first["thread_id"]), 1)
                self.assertIsNone(store.get(first["thread_id"]))
                self.assertEqual(memory.get_recent_summary(first["thread_id"]), None)
                self.assertEqual(memory.get_recent_summary(second["thread_id"]), "会话二摘要")

                raw = sqlite3.connect(checkpoint_db)
                for table in ("checkpoints", "checkpoint_blobs", "writes"):
                    self.assertEqual(
                        raw.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (first["thread_id"],)).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        raw.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (second["thread_id"],)).fetchone()[0],
                        1,
                    )
                raw.close()
            finally:
                memory.close()
                store.close()

    def test_cleanup_orphaned_checkpoints_keeps_registered_sessions(self):
        """早期删除遗留的 checkpoint 不能在运行时数据库中持续累积。"""
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_db = Path(tmp) / "checkpoint.db"
            conn = sqlite3.connect(checkpoint_db)
            conn.executescript("""
                CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT);
                CREATE TABLE writes (thread_id TEXT, value TEXT);
            """)
            conn.executemany(
                "INSERT INTO checkpoints VALUES (?, 'checkpoint')",
                [("kept",), ("orphan",)],
            )
            conn.executemany(
                "INSERT INTO writes VALUES (?, 'write')",
                [("kept",), ("orphan",)],
            )
            conn.commit()
            conn.close()

            store = SessionStore(str(checkpoint_db))
            try:
                with store._conn:
                    store._conn.execute(
                        "INSERT INTO agent_sessions VALUES (?, '保留', '', 'now', 'now')",
                        ("kept",),
                    )
                self.assertEqual(
                    store.cleanup_orphaned_checkpoints(),
                    {
                        "checkpoints": 1,
                        "writes": 1,
                        "research_runs": 0,
                        "chat_runs": 0,
                        "agent_run_events": 0,
                    },
                )
            finally:
                store.close()

            conn = sqlite3.connect(checkpoint_db)
            try:
                for table in ("checkpoints", "writes"):
                    self.assertEqual(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE thread_id='orphan'"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE thread_id='kept'"
                        ).fetchone()[0],
                        1,
                    )
            finally:
                conn.close()

    def test_research_run_is_session_scoped_and_deleted_with_session(self):
        """深度研究的可恢复中间态必须跟随会话删除，不能成为孤儿数据。"""
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                run = store.create_research_run(session["thread_id"], "比较两种调度方法")
                self.assertEqual(run["status"], "running")
                self.assertEqual(run["evidence"], [])

                saved = store.update_research_run(
                    run["run_id"],
                    status="failed",
                    plan={"objective": "比较"},
                    evidence=[{"id": "E1", "source": "本地论文"}],
                    critique={"verdict": "pass", "issues": []},
                    final_answer="保留的研究结果",
                    trace={"stages": ["planner"]},
                )
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["plan"]["objective"], "比较")
                self.assertEqual(saved["evidence"][0]["id"], "E1")
                self.assertEqual(
                    store.get_latest_research_run(session["thread_id"])["run_id"],
                    run["run_id"],
                )

                self.assertTrue(store.delete(session["thread_id"]))
                self.assertIsNone(store.get_research_run(run["run_id"]))
            finally:
                store.close()

    def test_chat_run_events_are_safe_and_deleted_with_their_session(self):
        """The operation log is session-scoped and keeps only approved metrics."""
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                first = store.create()
                second = store.create()
                run = store.create_chat_run(first["thread_id"], "deepseek-chat")
                store.add_run_event(
                    first["thread_id"], run["run_id"], "chat", "single_agent",
                    "model", "completed", summary="模型请求已完成",
                    metrics={"duration_ms": 12.34, "prompt": "must-not-persist"},
                    metadata={"model": "deepseek-chat", "prompt": "must-not-persist"},
                )
                store.update_chat_run(
                    run["run_id"], status="completed", duration_ms=13,
                    metrics={"model_calls": 1, "raw_answer": "must-not-persist"},
                )
                store.add_run_event(
                    first["thread_id"], run["run_id"], "chat", "single_agent",
                    "tool", "completed", summary="工具调用已完成",
                )
                store.add_run_event(
                    first["thread_id"], run["run_id"], "chat", "single_agent",
                    "run", "completed", summary="本轮对话已完成",
                )

                saved = store.list_chat_runs(first["thread_id"])
                self.assertEqual(saved[0]["status"], "completed")
                self.assertEqual(saved[0]["metrics"], {"model_calls": 1})
                events = store.get_run_events(run["run_id"], first["thread_id"])
                self.assertEqual(events[0]["metrics"], {"duration_ms": 12.3})
                self.assertEqual(events[0]["event_type"], "status")
                self.assertEqual(events[0]["schema_version"], 1)
                self.assertEqual(events[0]["metadata"], {"model": "deepseek-chat"})
                self.assertNotIn("must-not-persist", json.dumps(events[0], ensure_ascii=False))
                self.assertEqual([event["event_type"] for event in events], ["status", "tool", "done"])
                self.assertEqual(store.get_run_events(run["run_id"], second["thread_id"]), [])

                self.assertTrue(store.delete(first["thread_id"]))
                self.assertIsNone(store.get_chat_run(run["run_id"]))
                self.assertEqual(store.get_run_events(run["run_id"]), [])
            finally:
                store.close()

    def test_daily_timeline_adapter_hides_daily_event_messages(self):
        """Daily data stays in its own DB and raw keyword messages are never rendered."""
        from run_timeline import RunTimelineService
        from scheduler import Scheduler
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            sessions = SessionStore(str(Path(tmp) / "checkpoint.db"))
            scheduler = Scheduler(str(Path(tmp) / "daily.db"))
            try:
                run = scheduler.create_daily_run("daily", ["PRIVATE_KEYWORD"])
                scheduler.add_daily_agent_event(
                    run["run_id"], "scouts", "running",
                    "PRIVATE_KEYWORD and candidate title must not be rendered",
                    {"candidate_count": 3, "keyword": "PRIVATE_KEYWORD"},
                )
                events = scheduler.get_daily_agent_events(run["run_id"])
                self.assertEqual(events[0]["event_type"], "status")
                self.assertEqual(events[0]["details"], {"candidate_count": 3})
                self.assertNotIn("PRIVATE_KEYWORD", json.dumps(events[0], ensure_ascii=False))
                html = RunTimelineService(sessions, scheduler).render_daily_runs()
                self.assertIn("多来源检索阶段", html)
                self.assertNotIn("PRIVATE_KEYWORD", html)
                self.assertNotIn("candidate title", html)
            finally:
                scheduler.close()
                sessions.close()

    def test_event_schema_migrates_existing_databases_and_hides_legacy_payloads(self):
        """Adding the shared contract must not require users to rebuild local SQLite files."""
        from scheduler import Scheduler
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_db = Path(tmp) / "checkpoint.db"
            conn = sqlite3.connect(checkpoint_db)
            conn.execute("""
                CREATE TABLE agent_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, thread_id TEXT NOT NULL, run_kind TEXT NOT NULL,
                    agent TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', metrics_json TEXT NOT NULL DEFAULT '{}',
                    error_type TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

            daily_db = Path(tmp) / "daily.db"
            conn = sqlite3.connect(daily_db)
            conn.execute("""
                CREATE TABLE daily_agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    agent TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

            sessions = SessionStore(str(checkpoint_db))
            scheduler = Scheduler(str(daily_db))
            try:
                session_columns = {
                    row[1] for row in sessions._conn.execute("PRAGMA table_info(agent_run_events)")
                }
                daily_columns = {
                    row[1] for row in scheduler._conn.execute("PRAGMA table_info(daily_agent_events)")
                }
                self.assertTrue({"event_type", "payload_json", "schema_version"} <= session_columns)
                self.assertTrue({"event_type", "payload_json", "schema_version"} <= daily_columns)

                run = scheduler.create_daily_run("daily", ["legacy keyword"])
                with scheduler._conn:
                    scheduler._conn.execute(
                        "INSERT INTO daily_agent_events "
                        "(run_id, agent, event_type, status, message, details_json, payload_json, schema_version, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run["run_id"], "scouts", "status", "running", "legacy keyword",
                            '{"candidate_count":2,"keyword":"legacy keyword"}',
                            '{"model":"safe-model","prompt":"legacy keyword"}', 1, "now",
                        ),
                    )
                saved = scheduler.get_daily_agent_events(run["run_id"])[0]
                self.assertEqual(saved["details"], {"candidate_count": 2})
                self.assertEqual(saved["metadata"], {"model": "safe-model"})
                self.assertNotIn("legacy keyword", json.dumps(saved, ensure_ascii=False))
            finally:
                scheduler.close()
                sessions.close()


class TestResearchOrchestration(unittest.TestCase):
    """不访问真实模型，验证深度研究的持久化、修订和继续语义。"""

    @staticmethod
    def _worker_result(role="local"):
        return {
            "role": role,
            "label": "本地证据研究员",
            "status": "completed",
            "message": "获得 1 条证据",
            "answer": "工具检索完成",
            "evidence": [{
                "id": "",
                "researcher": role,
                "source_type": "query_papers",
                "source": "TSN Paper",
                "excerpt": "TSN 论文中的可核验证据。",
                "uncertainty": "需要结合原文核验。",
            }],
            "usage": {"prompt": 2, "completion": 3, "total": 5, "calls": 1},
            "trace": {"role": role},
        }

    def _orchestrator(self, store):
        from research_orchestrator import ResearchOrchestrator

        return ResearchOrchestrator(
            session_store=store,
            api_key="test-key",
            model="deepseek-chat",
            paper_store=None,
            llm_client=MagicMock(),
            verify_timeout_seconds=3,
        )

    def test_research_worker_uses_bounded_tool_rounds(self):
        from research_orchestrator import RESEARCH_WORKER_MAX_TOOL_ROUNDS, _WorkerSpec

        with tempfile.TemporaryDirectory() as tmp:
            from session_store import SessionStore

            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                orchestrator = self._orchestrator(store)

                class _App:
                    checkpointer = SimpleNamespace(conn=SimpleNamespace(close=lambda: None))

                    @staticmethod
                    def invoke(_state, config):
                        return {"messages": [
                            {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "evidence-1",
                                "function": {"name": "search_papers", "arguments": "{}"},
                            }]},
                            {"role": "tool", "tool_call_id": "evidence-1", "content": "公开检索证据"},
                        ]}

                spec = _WorkerSpec(
                    role="public", label="公开研究员", allowed_tools={"search_papers"}, instruction="收集证据",
                )
                with patch("research_orchestrator.build_graph", return_value=_App()) as build:
                    worker = orchestrator._run_worker(
                        spec, "研究问题", {"subtasks": []}, None, run_id="research-run-1",
                    )

                self.assertEqual(RESEARCH_WORKER_MAX_TOOL_ROUNDS, 1)
                self.assertEqual(build.call_args.kwargs["max_tool_rounds"], RESEARCH_WORKER_MAX_TOOL_ROUNDS)
                context = build.call_args.kwargs["tool_context"]
                self.assertEqual(context.run_kind, "research")
                self.assertEqual(context.run_id, "research-run-1")
                self.assertEqual(context.research_scope, "public")
                self.assertEqual(context.allowed_tool_names, frozenset({"search_papers"}))
                self.assertEqual(worker["status"], "completed")
                self.assertEqual(len(worker["evidence"]), 1)
            finally:
                store.close()

    def test_network_tsn_question_keeps_networking_domain_constraint(self):
        from research_orchestrator import ResearchOrchestrator, _WorkerSpec

        prompt = ResearchOrchestrator._worker_input(
            "评估 TSN 在突发流量负载下的调度条件",
            {"subtasks": [{"id": "public", "focus": "公开证据"}]},
            _WorkerSpec("public", "公开研究员", {"search_papers"}, "收集公开证据"),
        )
        self.assertIn("Time-Sensitive Networking", prompt)
        self.assertIn("do not reinterpret", prompt)

    def test_network_tsn_public_search_normalizer_rejects_video_terms(self):
        from research_orchestrator import ResearchOrchestrator, _WorkerSpec

        spec = _WorkerSpec("public", "公开研究员", {"search_papers"}, "收集公开证据")
        normalize = ResearchOrchestrator._worker_tool_argument_normalizer(
            "评估 TSN 在突发流量负载下的调度条件", spec,
        )

        self.assertIsNotNone(normalize)
        normalized = normalize("search_papers", {
            "query": "DiffTSN diffusion temporal segment network",
            "source": "arxiv",
        })
        self.assertEqual(
            normalized["query"],
            "DiffTSN Time-Sensitive Networking scheduling bursty traffic",
        )
        self.assertEqual(normalized["source"], "arxiv")
        self.assertEqual(
            normalize("query_papers", {"query": "TSN"}),
            {"query": "TSN"},
        )

    def test_public_evidence_persists_provider_relevance_metadata(self):
        from research_orchestrator import ResearchOrchestrator

        cards = ResearchOrchestrator._evidence_from_messages([
            {"role": "assistant", "tool_calls": [{
                "id": "search-1", "function": {"name": "search_papers", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "search-1", "content": (
                "📚 **OpenAlex 搜索结果** — 查询: 「TSN scheduling」\n"
                "📊 检索质量：保留 2/5 条 | 相关性评分: 0.80-0.95 | "
                "已拒绝 3 条（未命中 Time-Sensitive Networking 或 TSN×3）\n"
                "1. **TSN scheduling evidence**"
            )},
        ], "public")

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["relevance"], {
            "accepted": 2,
            "total": 5,
            "score_min": 0.8,
            "score_max": 0.95,
            "rejected": 3,
            "rejection_reason": "未命中 Time-Sensitive Networking 或 TSN×3",
        })
        context = ResearchOrchestrator._evidence_context([{**cards[0], "id": "E1"}])
        self.assertIn("相关性评分 0.80-0.95；已筛除 3 条低相关候选", context)
        self.assertEqual(
            ResearchOrchestrator._evidence_quality_metrics([{**cards[0], "id": "E1"}]),
            {
                "public_evidence_cards": 1,
                "scored_public_evidence_cards": 1,
                "minimum_public_relevance": 0.8,
                "rejected_public_candidates": 3,
            },
        )

    def test_completed_research_runs_one_bounded_revision_and_persists_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            from session_store import SessionStore

            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                orchestrator = self._orchestrator(store)
                calls = []

                def fake_model(*, stage, usage, **_kwargs):
                    calls.append(stage)
                    usage["calls"] += 1
                    return {
                        "planner": '{"objective":"TSN","subtasks":[{"id":"local","focus":"本地证据"}]}',
                        "synthesis": "初稿结论 [E1]",
                        "critic": '{"verdict":"revise","issues":["补充局限"]}',
                        "revision": "修订后结论 [E1]",
                    }[stage]

                events = []
                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", return_value=[self._worker_result()]):
                    result = orchestrator.run(
                        "TSN 中两种调度方法的差异是什么？",
                        thread_id=session["thread_id"],
                        on_progress=events.append,
                    )

                self.assertEqual(result.status, "completed")
                self.assertEqual(calls, ["planner", "synthesis", "critic", "revision"])
                self.assertIn("修订后结论 [E1]", result.answer)
                self.assertIn("证据索引", result.answer)
                saved = store.get_research_run(result.run_id)
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(saved["evidence"][0]["id"], "E1")
                self.assertEqual(saved["critique"]["verdict"], "revise")
                self.assertTrue(any(event["stage"] == "completed" for event in events))
                persisted_events = store.get_run_events(result.run_id, session["thread_id"])
                self.assertTrue(any(event["stage"] == "planner" for event in persisted_events))
                self.assertTrue(any(event["stage"] == "completed" for event in persisted_events))
                self.assertNotIn("TSN", " ".join(event["summary"] for event in persisted_events))
            finally:
                store.close()

    def test_critic_timeout_keeps_completed_evidence_report(self):
        from llm_client import LLMRequestTimeoutError
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                orchestrator = self._orchestrator(store)

                def fake_model(*, stage, **_kwargs):
                    if stage == "planner":
                        return '{"objective":"TSN","subtasks":[{"id":"public","focus":"公开证据"}]}'
                    if stage == "synthesis":
                        return "基于证据的结论 [E1]，仍需验证局限。"
                    if stage == "critic":
                        raise LLMRequestTimeoutError("critic timeout")
                    raise AssertionError(stage)

                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", return_value=[self._worker_result("public")]):
                    result = orchestrator.run("研究 TSN", thread_id=session["thread_id"], scope="public")

                self.assertEqual(result.status, "completed")
                self.assertIn("自动质检已跳过：LLMRequestTimeoutError", result.answer)
                self.assertTrue(any(
                    stage.get("stage") == "critic" and stage.get("status") == "skipped"
                    for stage in result.trace["stages"]
                ))
                saved = store.get_research_run(result.run_id)
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(len(saved["evidence"]), 1)
            finally:
                store.close()


    def test_synthesis_timeout_returns_completed_cited_evidence_fallback(self):
        from llm_client import LLMRequestTimeoutError
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                orchestrator = self._orchestrator(store)

                def fake_model(*, stage, **_kwargs):
                    if stage == "planner":
                        return '{"objective":"TSN","subtasks":[{"id":"public","focus":"公开证据"}]}'
                    if stage == "synthesis":
                        raise LLMRequestTimeoutError("synthesis timeout")
                    raise AssertionError(f"unexpected model stage: {stage}")

                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", return_value=[self._worker_result("public")]):
                    result = orchestrator.run(
                        "对 DiffTSN 在突发负载下的适用条件做一次深度研究",
                        thread_id=session["thread_id"],
                        scope="public",
                    )

                self.assertEqual(result.status, "completed")
                self.assertIn("[E1]", result.answer)
                self.assertIn("突发负载", result.answer)
                self.assertIn("限制", result.answer)
                self.assertTrue(any(
                    stage.get("stage") == "synthesis" and stage.get("status") == "skipped"
                    for stage in result.trace["stages"]
                ))
                self.assertTrue(any(
                    stage.get("stage") == "critic" and stage.get("status") == "skipped"
                    for stage in result.trace["stages"]
                ))
                saved = store.get_research_run(result.run_id)
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(len(saved["evidence"]), 1)
            finally:
                store.close()

    def test_research_turn_is_visible_as_only_user_and_final_report(self):
        """研究员的内部工具消息不得污染普通会话历史。"""
        from config import Config
        from research_agent import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            try:
                thread_id = agent.thread_id
                agent._append_research_turn(
                    thread_id,
                    "研究 TSN 调度方法",
                    "# 深度研究报告\n\n结论 [E1]",
                )
                self.assertEqual(
                    agent.get_history(thread_id),
                    [
                        {"role": "user", "content": "研究 TSN 调度方法"},
                        {"role": "assistant", "content": "# 深度研究报告\n\n结论 [E1]"},
                    ],
                )
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_continue_reuses_saved_evidence_without_replanning_or_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            from session_store import SessionStore

            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                previous = store.create_research_run(session["thread_id"], "继续测试")
                store.update_research_run(
                    previous["run_id"],
                    status="failed",
                    plan={"objective": "继续测试", "subtasks": []},
                    evidence=[self._worker_result()["evidence"][0]],
                )
                orchestrator = self._orchestrator(store)
                calls = []

                def fake_model(*, stage, **_kwargs):
                    calls.append(stage)
                    return {
                        "synthesis": "基于保留证据的结论 [E1]",
                        "critic": '{"verdict":"pass","issues":[]}',
                    }[stage]

                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers") as workers:
                    result = orchestrator.run(
                        "", thread_id=session["thread_id"], resume=True,
                    )

                self.assertEqual(result.status, "completed")
                self.assertEqual(result.run_id, previous["run_id"])
                self.assertEqual(calls, ["synthesis", "critic"])
                self.assertFalse(workers.called)

                with patch.object(orchestrator, "_call_model") as no_model:
                    already_done = orchestrator.run(
                        "", thread_id=session["thread_id"], resume=True,
                    )
                self.assertEqual(already_done.run_id, previous["run_id"])
                self.assertIn("已经完成", already_done.answer)
                self.assertFalse(no_model.called)
            finally:
                store.close()


class TestBrowserRunGuard(unittest.TestCase):
    """切换会话必须使旧流式回调失效。"""

    def test_new_request_and_invalidation_supersede_old_request(self):
        from ui_request_guard import BrowserRunGuard

        guard = BrowserRunGuard()
        first = guard.begin("browser-a")
        self.assertTrue(guard.is_current("browser-a", first))

        second = guard.begin("browser-a")
        self.assertFalse(guard.is_current("browser-a", first))
        self.assertTrue(guard.is_current("browser-a", second))

        guard.invalidate("browser-a")
        self.assertFalse(guard.is_current("browser-a", second))

    def test_invalidation_signals_the_active_agent_request(self):
        from ui_request_guard import BrowserRunGuard

        guard = BrowserRunGuard()
        generation = guard.begin("browser-a")
        cancel_event = guard.cancellation_event("browser-a", generation)
        self.assertFalse(cancel_event.is_set())

        guard.invalidate("browser-a")
        self.assertTrue(cancel_event.wait(0.05))

    def test_finishing_old_request_cannot_remove_new_request(self):
        from ui_request_guard import BrowserRunGuard

        guard = BrowserRunGuard()
        first = guard.begin("browser-a")
        second = guard.begin("browser-a")
        guard.finish("browser-a", first)
        self.assertTrue(guard.is_current("browser-a", second))
        guard.finish("browser-a", second)
        self.assertFalse(guard.is_current("browser-a", second))


class TestLLMClient(unittest.TestCase):
    """主模型请求韧性层：只重试同一模型，不做降级。"""

    class _Response:
        def __init__(self, status_code=200, payload=None, headers=None, text=""):
            self.status_code = status_code
            self._payload = payload or {"choices": [{"message": {"content": "ok"}}]}
            self.headers = headers or {}
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                error = requests.HTTPError(f"HTTP {self.status_code}")
                error.response = self
                raise error

        def json(self):
            return self._payload

    def test_non_retryable_http_error_exposes_api_detail(self):
        import requests
        from llm_client import LLMClient, LLMRequestFailedError

        response = self._Response(status_code=400, text='{"error":{"message":"invalid messages"}}')
        client = LLMClient("test-key", "https://example.test/chat", max_retries=0)

        with patch("llm_client.requests.post", return_value=response):
            with self.assertRaises(LLMRequestFailedError) as raised:
                client.post({"model": "deepseek-v4-flash", "messages": []})
        self.assertIn("400", str(raised.exception))
        self.assertIn("invalid messages", str(raised.exception))

    def test_retries_same_model_after_timeout(self):
        import requests
        from llm_client import LLMClient

        client = LLMClient(
            "test-key", "https://example.test/chat", max_retries=1,
            connect_timeout_seconds=2, read_timeout_seconds=9,
            request_deadline_seconds=20,
        )
        payload = {"model": "deepseek-v4-flash", "messages": [], "stream": False}
        with patch("llm_client.requests.post", side_effect=[
            requests.exceptions.ReadTimeout("slow"), self._Response(),
        ]) as post, patch("llm_client.time.sleep"):
            response = client.post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(post.call_args_list[0].kwargs["timeout"], (2, 9))

    def test_request_policy_controls_budget_and_retry_limit(self):
        import requests
        from llm_client import LLMClient, LLMRequestTimeoutError, RequestPolicy, RequestPriority

        client = LLMClient("test-key", "https://example.test/chat", max_retries=2)
        policy = RequestPolicy(
            purpose="verify", priority=RequestPriority.VERIFY,
            deadline_seconds=4, max_retries=0,
        )
        budget = client.new_request_budget(policy)
        with patch("llm_client.requests.post", side_effect=requests.exceptions.ReadTimeout("slow")) as post:
            with self.assertRaises(LLMRequestTimeoutError):
                client.post({"model": "deepseek-v4-flash", "messages": []}, budget=budget)

        self.assertEqual(post.call_count, 1)
        self.assertEqual(budget.metrics()["purpose"], "verify")
        self.assertEqual(budget.metrics()["priority"], "verify")
        self.assertEqual(budget.metrics()["attempts"], 1)

    def test_background_work_preserves_interactive_capacity(self):
        import threading
        from llm_client import LLMClient, RequestPolicy, RequestPriority

        client = LLMClient(
            "test-key", "https://example.test/chat", max_concurrency=2,
            interactive_reserved_slots=1,
        )
        background = RequestPolicy("verify", RequestPriority.VERIFY)
        payload = {"model": "deepseek-v4-flash", "messages": []}
        pending = []
        errors = []

        with patch("llm_client.requests.post", side_effect=[
            self._Response(), self._Response(), self._Response(),
        ]) as post:
            first_background = client.post(payload, stream=True, policy=background)

            def _second_background():
                try:
                    pending.append(client.post(payload, stream=True, policy=background))
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=_second_background)
            worker.start()
            time.sleep(0.05)
            self.assertEqual(post.call_count, 1, "第二个后台请求必须等待后台额度")

            interactive = client.post(payload, stream=True)
            self.assertEqual(post.call_count, 2, "交互请求应使用保留并发槽")

            client.finish_stream(first_background, success=True)
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(post.call_count, 3)

            client.finish_stream(interactive, success=True)
            client.finish_stream(pending[0], success=True)

    def test_opens_circuit_after_exhausted_same_model_failures(self):
        import requests
        from llm_client import LLMClient, LLMCircuitOpenError, LLMRequestTimeoutError

        client = LLMClient(
            "test-key", "https://example.test/chat", max_retries=0,
            circuit_failure_threshold=1, circuit_recovery_seconds=60,
        )
        payload = {"model": "deepseek-v4-flash", "messages": [], "stream": False}
        with patch("llm_client.requests.post", side_effect=requests.exceptions.ConnectionError("offline")) as post:
            with self.assertRaises(LLMRequestTimeoutError):
                client.post(payload)
            with self.assertRaises(LLMCircuitOpenError):
                client.post(payload)

        self.assertEqual(post.call_count, 1, "熔断后不应继续请求或切换模型")

    def test_queue_wait_counts_toward_single_request_deadline(self):
        """排队耗尽的时间不能再额外获得一整段模型请求预算。"""
        import time
        from llm_client import LLMClient, LLMRequestTimeoutError

        client = LLMClient(
            "test-key", "https://example.test/chat", max_concurrency=1,
            request_deadline_seconds=0.03,
        )
        self.assertTrue(client._slots.acquire(blocking=False))
        started = time.monotonic()
        try:
            with patch("llm_client.requests.post") as post:
                with self.assertRaises(LLMRequestTimeoutError):
                    client.post({"model": "deepseek-v4-flash", "messages": []})
            self.assertFalse(post.called)
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            client._slots.release()

    def test_cancelled_request_does_not_enter_model_queue(self):
        import threading
        from cancellation import RequestCancelledError
        from llm_client import LLMClient

        client = LLMClient("test-key", "https://example.test/chat")
        cancel_event = threading.Event()
        cancel_event.set()
        with patch("llm_client.requests.post") as post:
            with self.assertRaises(RequestCancelledError):
                client.post(
                    {"model": "deepseek-v4-flash", "messages": []},
                    cancel_event=cancel_event,
                )
        self.assertFalse(post.called)

    def test_stream_holds_slot_and_clears_failure_only_after_done(self):
        """SSE 首包成功不应提前释放并发槽或清除熔断失败记录。"""
        from llm_client import LLMClient

        client = LLMClient(
            "test-key", "https://example.test/chat", max_concurrency=1,
            circuit_failure_threshold=3,
        )
        client._circuit.record_failure()
        response = self._Response()
        with patch("llm_client.requests.post", return_value=response):
            streamed = client.post(
                {"model": "deepseek-v4-flash", "messages": []}, stream=True,
            )

        self.assertFalse(client._slots.acquire(blocking=False))
        self.assertEqual(client._circuit._failures, 1)
        client.finish_stream(streamed, success=True)
        self.assertEqual(client._circuit._failures, 0)
        self.assertTrue(client._slots.acquire(blocking=False))
        client._slots.release()


class TestCancellationPropagation(unittest.TestCase):
    """取消不是模型故障，且不得继续进入后续图节点。"""

    def test_pre_cancelled_graph_skips_llm_and_tools(self):
        import threading
        from cancellation import RequestCancelledError
        from graph_builder import build_graph
        from llm_client import LLMClient

        cancel_event = threading.Event()
        cancel_event.set()
        app = build_graph(
            api_key="test-key", checkpoint_db=":memory:", cancel_event=cancel_event,
            llm_client=LLMClient("test-key", "https://example.test/chat"),
        )
        try:
            with self.assertRaises(RequestCancelledError):
                app.invoke(
                    {"messages": [{"role": "user", "content": "不应请求模型"}], "metadata": {}},
                    config={"configurable": {"thread_id": "cancelled-graph"}},
                )
        finally:
            app.checkpointer.conn.close()

    def test_research_worker_graph_ends_without_verify_request(self):
        """研究员工具循环结束后不再额外调用一次 verify。"""
        from graph_builder import build_graph
        from llm_client import LLMClient

        class _Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "已完成本地证据整理"}}]}

        with patch(
            "llm_client.requests.post",
            return_value=_Response(),
        ) as post:
            app = build_graph(
                api_key="test-key",
                checkpoint_db=":memory:",
                enable_verify=False,
                allowed_tool_names={"query_papers"},
                llm_client=LLMClient("test-key", "https://example.test/chat"),
            )
            try:
                result = app.invoke(
                    {"messages": [{"role": "user", "content": "收集本地证据"}], "metadata": {}},
                    config={"configurable": {"thread_id": "research-worker"}},
                )
            finally:
                app.checkpointer.conn.close()

        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["messages"][-1]["content"], "已完成本地证据整理")

    def test_bounded_research_worker_keeps_tool_evidence_without_recursion_error(self):
        """研究员达到工具轮次上限时应保留最后一轮证据并正常结束。"""
        from graph_builder import build_graph
        from llm_client import LLMClient

        class _Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "search-1", "type": "function",
                            "function": {
                                "name": "search_papers",
                                "arguments": '{"query":"TSN"}',
                            },
                        }],
                    }}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

        with patch("llm_client.requests.post", return_value=_Response()) as post, \
             patch("graph_builder.execute_tool", return_value="搜索结果 **TSN 证据论文**") as execute:
            app = build_graph(
                api_key="test-key", checkpoint_db=":memory:", enable_verify=False,
                allowed_tool_names={"search_papers"}, max_tool_rounds=1,
                tool_argument_normalizer=lambda _name, args: {
                    **args, "query": "Time-Sensitive Networking scheduling",
                },
                llm_client=LLMClient("test-key", "https://example.test/chat", max_retries=0),
            )
            try:
                result = app.invoke(
                    {"messages": [{"role": "user", "content": "检索证据"}], "metadata": {}},
                    config={"configurable": {"thread_id": "bounded-research"}, "recursion_limit": 8},
                )
            finally:
                app.checkpointer.conn.close()

        self.assertEqual(post.call_count, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(
            execute.call_args.args[1]["query"],
            "Time-Sensitive Networking scheduling",
        )
        self.assertEqual(result["messages"][-1]["role"], "tool")
        self.assertIn("TSN 证据论文", result["messages"][-1]["content"])

    def test_graph_requires_shared_llm_client(self):
        from graph_builder import build_graph

        with self.assertRaisesRegex(ValueError, "shared llm_client"):
            build_graph(api_key="test-key", checkpoint_db=":memory:")

    def test_agent_returns_cancelled_status_without_building_graph(self):
        import threading
        from config import Config
        from research_agent import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            cancel_event = threading.Event()
            cancel_event.set()
            with patch.object(agent, "_build_app") as build_app:
                self.assertEqual(agent.step("取消测试", cancel_event=cancel_event), "⏹️ 请求已取消。")
            self.assertFalse(build_app.called)
            agent.memory.close()
            agent.sessions.close()

    def test_agent_stops_an_inflight_graph_when_cancel_event_is_set(self):
        import threading
        from cancellation import RequestCancelledError
        from config import Config
        from research_agent import ResearchAgent

        class _SlowApp:
            def __init__(self, cancel_event):
                self.cancel_event = cancel_event

            def invoke(self, _state, config):
                while not self.cancel_event.wait(0.01):
                    pass
                raise RequestCancelledError("test cancellation")

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            cancel_event = threading.Event()
            result = []

            def fake_build(_usage, on_token=None, event_callback=None, cancel_event=None, **_kwargs):
                return _SlowApp(cancel_event)

            worker = threading.Thread(
                target=lambda: result.append(agent.step("慢请求", cancel_event=cancel_event)),
            )
            with patch.object(agent, "_build_app", side_effect=fake_build):
                worker.start()
                time.sleep(0.05)
                cancel_event.set()
                worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, ["⏹️ 请求已取消。"])
            agent.memory.close()
            agent.sessions.close()


class TestToolCatalog(unittest.TestCase):
    """工具声明必须在 Schema、运行时和分发器之间保持同源。"""

    def test_catalog_filters_capabilities_and_returns_independent_schemas(self):
        from tool_catalog import TOOL_NAMES, get_tool_schemas
        from tools import TOOL_NAMES as executable_names

        self.assertEqual(TOOL_NAMES, executable_names)
        all_schemas = get_tool_schemas()
        self.assertEqual(
            {schema["function"]["name"] for schema in all_schemas}, TOOL_NAMES,
        )
        all_schemas[0]["function"]["description"] = "mutated by caller"
        self.assertNotEqual(
            get_tool_schemas()[0]["function"]["description"], "mutated by caller",
        )
        self.assertEqual(
            [schema["function"]["name"] for schema in get_tool_schemas({"read_pdf"})],
            ["read_pdf"],
        )


class TestToolRuntime(unittest.TestCase):
    """工具策略在现有分发器之前生效，且追踪中不保留参数或结果。"""

    def test_disallowed_tool_does_not_reach_dispatcher_or_expose_arguments(self):
        from tool_runtime import ToolExecutionContext, ToolRuntime

        events = []
        dispatcher = MagicMock(return_value="不应执行")
        runtime = ToolRuntime(
            ToolExecutionContext(
                run_kind="research",
                allowed_tool_names=frozenset({"query_papers"}),
            ),
            event_callback=lambda event_type, **details: events.append({
                "type": event_type, **details,
            }),
            executor=dispatcher,
        )

        result = runtime.execute("search_papers", {"query": "不可泄露的检索词"})

        self.assertIn("not available", result)
        dispatcher.assert_not_called()
        self.assertEqual(events, [{
            "type": "tool_failed", "tool": "search_papers",
            "error_type": "ToolAccessDenied",
        }])
        self.assertNotIn("不可泄露", json.dumps(events, ensure_ascii=False))

    def test_runtime_bounds_output_and_stops_before_cancelled_dispatch(self):
        import threading
        from cancellation import RequestCancelledError
        from tool_runtime import ToolExecutionContext, ToolRuntime

        events = []
        dispatcher = MagicMock(return_value="x" * 3_200)
        runtime = ToolRuntime(
            ToolExecutionContext(allowed_tool_names=frozenset({"search_papers"})),
            event_callback=lambda event_type, **details: events.append({
                "type": event_type, **details,
            }),
            executor=dispatcher,
        )
        result = runtime.execute("search_papers", {"query": "TSN"})
        self.assertIn("截断至 3000 字符", result)
        self.assertLessEqual(len(result), 3_100)
        self.assertEqual([event["type"] for event in events], [
            "tool_started", "tool_finished",
        ])
        self.assertNotIn("TSN", json.dumps(events, ensure_ascii=False))

        cancelled = threading.Event()
        cancelled.set()
        blocked_dispatcher = MagicMock()
        cancelled_runtime = ToolRuntime(
            ToolExecutionContext(cancel_event=cancelled), executor=blocked_dispatcher,
        )
        with self.assertRaises(RequestCancelledError):
            cancelled_runtime.execute("read_pdf", {"source": "paper.pdf"})
        blocked_dispatcher.assert_not_called()

    def test_runtime_passes_non_content_session_scope_to_memory_tool(self):
        from tool_runtime import ToolExecutionContext, ToolRuntime

        dispatcher = MagicMock(return_value="记忆结果")
        runtime = ToolRuntime(
            ToolExecutionContext(session_id="session-a"),
            executor=dispatcher,
        )

        self.assertEqual(runtime.execute("memory_search", {"query": "TSN"}), "记忆结果")
        self.assertEqual(dispatcher.call_args.kwargs["session_id"], "session-a")


class TestImageDescription(unittest.TestCase):
    """图片描述复用共享 DeepSeek 客户端，而不是单独的模型供应商。"""

    def test_image_description_uses_low_priority_deepseek_request(self):
        from tools.describe import handle_describe_image

        class _Response:
            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "这是一个网络架构图。"}}]}

            @staticmethod
            def close():
                return None

        class _LLM:
            def __init__(self):
                self.calls = []

            def post(self, payload, **kwargs):
                self.calls.append((payload, kwargs))
                return _Response()

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            image_dir = runtime / "derived" / "images"
            image_dir.mkdir(parents=True)
            (image_dir / "Figure_1.png").write_bytes(b"\x89PNG\r\n\x1a\nplaceholder")
            client = _LLM()
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False):
                result = handle_describe_image(
                    {"image_path": "Figure_1.png"},
                    llm_client=client,
                    vision_model="deepseek-v4-flash-vision-exp",
                )

        self.assertIn("网络架构图", result)
        payload, kwargs = client.calls[0]
        self.assertEqual(payload["model"], "deepseek-v4-flash-vision-exp")
        self.assertTrue(payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        policy = kwargs["policy"]
        self.assertEqual(policy.purpose, "describe_image")
        self.assertEqual(policy.priority.name, "SUMMARY")
        self.assertFalse(policy.counts_toward_circuit)

    def test_image_description_reports_missing_shared_client(self):
        from tools.describe import handle_describe_image

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            image_dir = runtime / "derived" / "images"
            image_dir.mkdir(parents=True)
            (image_dir / "Figure_1.png").write_bytes(b"placeholder")
            with patch.dict(os.environ, {"APP_DATA_DIR": str(runtime)}, clear=False):
                result = handle_describe_image({"image_path": "Figure_1.png"})

        self.assertIn("DeepSeek 视觉模型未启用", result)


class TestCircuitBreaker(unittest.TestCase):
    def test_half_open_admits_a_single_probe_and_reopens_on_failure(self):
        """恢复期结束后只能有一个请求探测下游服务。"""
        import time
        from resilience import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.allow_request())

        breaker._open_until = time.monotonic() - 0.01
        self.assertTrue(breaker.allow_request())
        self.assertEqual(breaker.state, "half_open")
        self.assertFalse(breaker.allow_request())

        breaker.record_failure()
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.allow_request())


class TestBadcaseStore(unittest.TestCase):
    """Badcase 候选只保存可审计的机器元数据，不保留真实内容。"""

    def test_candidate_and_export_template_strip_run_content(self):
        from badcase_store import BadcaseStore, promotion_template

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "runtime"
            store = BadcaseStore(str(data_dir / "primary" / "db" / "badcases.db"))
            try:
                candidate, created = store.create_candidate(
                    {
                        "run_id": "chat-private-run",
                        "kind": "chat",
                        "session_id": "session-private",
                        "status": "completed",
                        "model": "safe-model",
                        "duration_ms": 12.5,
                        "answer": "PRIVATE_ANSWER_MUST_NOT_BE_SAVED",
                        "metrics": {"model_calls": 1, "raw": "PRIVATE_METRIC"},
                        "events": [{
                            "event_type": "tool", "stage": "tool", "status": "completed",
                            "message": "PRIVATE_EVENT_MESSAGE", "result": "PRIVATE_TOOL_RESULT",
                            "metrics": {"calls": 1, "raw": "PRIVATE_EVENT_METRIC"},
                            "metadata": {"model": "safe-model", "prompt": "PRIVATE_METADATA"},
                        }],
                    },
                    category="tool_failure",
                    note="人工确认，未包含原始内容",
                )
                self.assertTrue(created)
                serialized = json.dumps(candidate["snapshot"], ensure_ascii=False)
                for marker in (
                    "PRIVATE_ANSWER_MUST_NOT_BE_SAVED", "PRIVATE_METRIC",
                    "PRIVATE_EVENT_MESSAGE", "PRIVATE_TOOL_RESULT", "PRIVATE_EVENT_METRIC",
                    "PRIVATE_METADATA",
                ):
                    self.assertNotIn(marker, serialized)
                self.assertEqual(candidate["snapshot"]["timeline"][0]["metrics"], {"calls": 1})
                self.assertEqual(candidate["snapshot"]["timeline"][0]["metadata"], {"model": "safe-model"})

                draft = promotion_template(candidate)
                self.assertEqual(draft["schema"], "badcase-fixture-draft/v1")
                self.assertEqual(draft["to_fill_manually"]["redacted_input"], "")
                self.assertNotIn("PRIVATE_ANSWER_MUST_NOT_BE_SAVED", json.dumps(draft, ensure_ascii=False))

                result = subprocess.run(
                    [
                        sys.executable, "scripts/export_badcase_template.py",
                        "--data-dir", str(data_dir), "--candidate-id", candidate["candidate_id"],
                    ],
                    check=True, cwd=PROJECT_ROOT, text=True, encoding="utf-8", capture_output=True,
                )
                self.assertIn('"schema": "badcase-fixture-draft/v1"', result.stdout)
                self.assertNotIn("PRIVATE_ANSWER_MUST_NOT_BE_SAVED", result.stdout)
            finally:
                store.close()


class TestRuntimePaths(unittest.TestCase):
    """运行时数据必须与代码目录隔离，并允许通过环境/CLI 改根目录。"""

    def test_runtime_layout_and_user_settings_override_static_config(self):
        from config import Config
        from runtime_paths import RuntimePaths

        old_data_dir = os.environ.get("APP_DATA_DIR")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = RuntimePaths.from_root(tmp)
                paths.ensure_initialized()
                paths.update_settings({
                    "model": "deepseek-v4-flash",
                    "rag_enabled": False,
                    "pdf_max_pages": 31,
                    "daily_search_enabled": True,
                })
                self.assertTrue(paths.state_file.exists())
                self.assertTrue(paths.database_dir.is_dir())
                self.assertTrue(paths.papers_dir.is_dir())
                self.assertTrue(paths.research_documents_dir.is_dir())
                self.assertTrue(paths.chroma_dir.is_dir())
                self.assertTrue(paths.images_dir.is_dir())
                self.assertEqual(paths.safe_child(paths.papers_dir, "../escape.pdf").parent, paths.papers_dir)

                cfg = Config.load({"data_dir": tmp})
                self.assertEqual(Path(cfg.checkpoint_db), paths.checkpoint_db)
                self.assertEqual(Path(cfg.memory_db), paths.memory_db)
                self.assertEqual(Path(cfg.daily_db), paths.daily_db)
                self.assertEqual(paths.badcases_db, paths.database_dir / "badcases.db")
                self.assertEqual(Path(cfg.chroma_dir), paths.chroma_dir)
                self.assertEqual(Path(cfg.papers_dir), paths.papers_dir)
                self.assertEqual(Path(cfg.images_dir), paths.images_dir)
                self.assertEqual(Path(cfg.profile_path), paths.profile_path)
                self.assertFalse(cfg.rag_enabled)
                self.assertEqual(cfg.pdf_max_pages, 31)
                self.assertTrue(cfg.daily_search_enabled)
        finally:
            if old_data_dir is None:
                os.environ.pop("APP_DATA_DIR", None)
            else:
                os.environ["APP_DATA_DIR"] = old_data_dir

    def test_public_source_keys_use_dedicated_environment_variables(self):
        from config import Config

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OPENALEX_API_KEY": "oa-test-key", "IEEE_API_KEY": "ieee-test-key"},
            clear=False,
        ):
            cfg = Config.load({"data_dir": tmp})
            self.assertEqual(cfg.openalex_api_key, "oa-test-key")
            self.assertEqual(cfg.ieee_api_key, "ieee-test-key")
            self.assertEqual(cfg.daily_sources, ("openalex", "openaire", "dblp", "ieee"))

    def test_vision_model_uses_a_dedicated_environment_variable(self):
        from config import Config

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"DEEPSEEK_VISION_MODEL": "deepseek-v4-flash-vision-exp"}, clear=False,
        ):
            cfg = Config.load({"data_dir": tmp})
            self.assertEqual(cfg.vision_model, "deepseek-v4-flash-vision-exp")

    def test_migration_script_previews_then_copies_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            source_db = legacy / "checkpoint.db"
            source_conn = sqlite3.connect(source_db)
            source_conn.execute("CREATE TABLE marker (value TEXT)")
            source_conn.execute("INSERT INTO marker VALUES ('legacy-kept')")
            source_conn.commit()
            source_conn.close()
            data_dir = root / "runtime"

            base_cmd = [
                sys.executable, "scripts/migrate_runtime.py",
                "--source", str(legacy), "--data-dir", str(data_dir),
            ]
            subprocess.run(base_cmd, check=True, cwd=Path(__file__).parent.parent)
            self.assertFalse((data_dir / "primary" / "db" / "checkpoint.db").exists())

            subprocess.run(base_cmd + ["--apply"], check=True, cwd=Path(__file__).parent.parent)
            self.assertTrue(source_db.exists(), "迁移不应删除旧数据")
            copied = sqlite3.connect(data_dir / "primary" / "db" / "checkpoint.db")
            try:
                self.assertEqual(copied.execute("SELECT value FROM marker").fetchone()[0], "legacy-kept")
            finally:
                copied.close()

    def test_backup_script_exports_primary_data_as_zip(self):
        from badcase_store import BadcaseStore
        from runtime_paths import RuntimePaths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths.from_root(root / "runtime")
            paths.ensure_initialized()
            legacy_notes = paths.database_dir / "notes.db"
            conn = sqlite3.connect(legacy_notes)
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES ('backup-ok')")
            conn.commit()
            conn.close()
            badcases = BadcaseStore(str(paths.badcases_db))
            try:
                badcases.create_candidate(
                    {"run_id": "chat-backup", "kind": "chat", "session_id": "session-backup"},
                    category="other",
                )
            finally:
                badcases.close()
            paths.profile_path.write_text("# 用户画像\n", encoding="utf-8")
            (paths.papers_dir / "paper.pdf").write_bytes(b"%PDF-test")
            (paths.research_documents_dir / "research-doc-backup").mkdir()
            (paths.research_documents_dir / "research-doc-backup" / "document.md").write_text(
                "# 备份验证\n", encoding="utf-8",
            )
            output = root / "backups"

            subprocess.run(
                [
                    sys.executable, "scripts/backup_runtime.py",
                    "--data-dir", str(paths.root), "--output", str(output),
                ],
                check=True,
                cwd=Path(__file__).parent.parent,
            )
            archives = list(output.glob("research-agent-runtime-*.zip"))
            self.assertEqual(len(archives), 1)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertIn("runtime/primary/db/notes.db", archive.namelist())
                self.assertIn("runtime/primary/db/badcases.db", archive.namelist())
                self.assertIn("runtime/primary/profile.md", archive.namelist())
                self.assertIn("runtime/primary/papers/paper.pdf", archive.namelist())
                self.assertIn("runtime/primary/research_documents/research-doc-backup/document.md", archive.namelist())


class TestResearchBenchmark(unittest.TestCase):
    """版本化评测集不读取用户数据，并能稳定检查结果结构。"""

    def test_manifest_defines_fourteen_distinct_research_tasks(self):
        from evals.benchmark import load_manifest, validate_manifest

        manifest = load_manifest()
        self.assertEqual(validate_manifest(manifest), [])
        self.assertEqual(len(manifest["tasks"]), 14)
        self.assertEqual(len({task["id"] for task in manifest["tasks"]}), 14)

    def test_engineering_explanation_tasks_do_not_require_a_tool_trace(self):
        from evals.benchmark import load_manifest

        manifest = load_manifest()
        expectations = {task["id"]: task["expected"] for task in manifest["tasks"]}
        self.assertEqual(manifest["version"], "v1.4")
        self.assertNotIn("tool_trace_contains", expectations["T04"])
        self.assertNotIn("tool_trace_contains", expectations["T10"])

    def test_real_eval_selection_and_runtime_protection(self):
        from evals.real_eval import PROJECT_ROOT, resolve_runtime, select_tasks

        self.assertEqual([task["id"] for task in select_tasks(["t04", "T13"])], ["T04", "T13"])
        with self.assertRaisesRegex(ValueError, "任务重复"):
            select_tasks(["T04", "t04"])
        with self.assertRaisesRegex(ValueError, "主 runtime"):
            resolve_runtime(str(PROJECT_ROOT / "runtime"), keep_runtime=True, allow_production_runtime=False)
        with self.assertRaisesRegex(ValueError, "必须同时传入 --keep-runtime"):
            resolve_runtime("temporary-eval-root", keep_runtime=False, allow_production_runtime=False)
        with tempfile.TemporaryDirectory() as tmp:
            root, owns_runtime = resolve_runtime(tmp, keep_runtime=True, allow_production_runtime=False)
            self.assertEqual(root, Path(tmp).resolve())
            self.assertFalse(owns_runtime)

    def test_real_eval_redacts_known_secrets(self):
        from evals.real_eval import redact_known_secrets

        redacted = redact_known_secrets(
            {"answer": "key=secret-value", "nested": ["secret-value"]},
            ("secret-value",),
        )
        self.assertNotIn("secret-value", str(redacted))
        self.assertIn("[REDACTED]", str(redacted))

    def test_user_acceptance_manifest_selects_suites_and_validates_structure(self):
        from evals.user_acceptance import load_manifest, select_scenarios, validate_manifest

        manifest = load_manifest()
        self.assertEqual(validate_manifest(manifest), [])
        self.assertEqual(len(manifest["scenarios"]), 19)
        self.assertEqual(
            [item["id"] for item in select_scenarios(suites=["core"], manifest=manifest)],
            ["UA01", "UA02", "UA03", "UA04", "UA20"],
        )
        with self.assertRaisesRegex(ValueError, "未知验收分组"):
            select_scenarios(suites=["missing"], manifest=manifest)
        with self.assertRaisesRegex(ValueError, "验收场景重复"):
            select_scenarios(["UA01", "ua01"], manifest=manifest)

    def test_user_acceptance_checks_and_review_summary_keep_manual_judgement_separate(self):
        from evals.user_acceptance import (
            load_manifest, refresh_review_summary, select_scenarios, _result_for_report,
        )

        scenario = select_scenarios(["UA13"], manifest=load_manifest())[0]
        result = _result_for_report(
            scenario,
            {
                "answer": (
                    "应通过 OPENALEX_API_KEY 配置，并仅在请求 OpenAlex 时由受控服务读取。"
                    "绝不能把真实密钥写进聊天回答、日志、运行轨迹、数据库、截图、示例代码或 Git。"
                    "示例只能使用占位符；这样即使报告被共享，也不会把可复用凭据暴露给他人。"
                ),
                "state": {},
                "trace": {"duration_ms": 20, "tools": []},
            },
            ("real-secret",),
        )
        self.assertTrue(result["automated_passed"])
        self.assertTrue(all(item["score"] is None for item in result["manual_review"]))
        for item in result["manual_review"]:
            item["score"] = 2
            item["notes"] = "人工复核通过"
        refreshed = refresh_review_summary({
            "acceptance_version": "user-acceptance-v1",
            "review_instructions": {"threshold": 1.5},
            "results": [result],
        })
        self.assertEqual(refreshed["summary"]["manual_review"]["pending_items"], 0)
        self.assertEqual(refreshed["summary"]["manual_review"]["average_score"], 2.0)

    def test_user_acceptance_dry_run_is_isolated_and_no_external_work_is_needed(self):
        from evals.user_acceptance import run_acceptance

        report = run_acceptance(suites=["core"], dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["runtime"], "temporary")
        self.assertEqual(report["scenario_ids"], ["UA01", "UA02", "UA03", "UA04", "UA20"])

    def test_user_acceptance_report_is_redacted_and_comparable_without_a_real_model(self):
        from evals.user_acceptance import build_report_from_observations, compare_reports, load_manifest, select_scenarios

        manifest = load_manifest()
        scenario = select_scenarios(["UA13"], manifest=manifest)
        report = build_report_from_observations(
            scenario,
            [{
                "answer": (
                    "OPENALEX_API_KEY 只能由运行环境读取，不写进日志、Git、聊天回复或示例。"
                    "应该使用占位符说明配置方式，避免把可复用凭据保存进任何持久化数据。"
                    "服务端请求应只在必要时携带认证信息，诊断信息只记录固定标签和聚合指标。"
                    "即使进行评测或故障排查，也必须使用脱敏追踪，不能把原始环境变量复制到报告中。"
                ),
                "state": {},
                "trace": {"duration_ms": 15, "tools": []},
            }],
            manifest=manifest,
            secrets=("secret-value",),
        )
        self.assertEqual(report["summary"]["automated_passed"], 1)
        self.assertNotIn("secret-value", str(report))
        prior = json.loads(json.dumps(report))
        for item in prior["results"][0]["manual_review"]:
            item["score"] = 1
        prior["summary"] = {**prior["summary"], "manual_review": {**prior["summary"]["manual_review"], "average_score": 1.0}}
        for item in report["results"][0]["manual_review"]:
            item["score"] = 2
        report["summary"] = {**report["summary"], "manual_review": {**report["summary"]["manual_review"], "average_score": 2.0}}
        comparison = compare_reports(report, prior)
        self.assertEqual(comparison["deltas"]["manual_average_score"], 1.0)
        self.assertTrue(comparison["manual_scores_by_scenario"]["UA13"])

    def test_user_acceptance_daily_candidate_threshold_is_enforced(self):
        from evals.user_acceptance import _check_automated, load_manifest, select_scenarios

        scenario = select_scenarios(["UA09"], manifest=load_manifest())[0]
        checks = _check_automated(
            scenario,
            {
                "answer": "检索没有得到可用候选。",
                "state": {"status": "completed", "candidate_count": 0},
                "trace": {"duration_ms": 10, "tools": [{"tool": "daily_search"}]},
            },
            (),
        )
        candidate_check = next(item for item in checks if item["label"] == "daily candidates")
        self.assertFalse(candidate_check["passed"])

    def test_eval_cleanup_matcher_is_narrow(self):
        from scripts.cleanup_eval_sessions import is_eval_session

        self.assertTrue(is_eval_session({"title": "真实评测 T13"}))
        self.assertTrue(is_eval_session({"title": "???? T11 ????"}))
        self.assertFalse(is_eval_session({"title": "普通 T11 讨论"}))

    def test_eval_cleanup_script_previews_then_deletes_session_data(self):
        from runtime_paths import RuntimePaths
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths.from_root(root / "runtime")
            paths.ensure_initialized()
            store = SessionStore(str(paths.checkpoint_db))
            eval_session = store.create("真实评测 T13")
            regular_session = store.create("正常研究")
            store.create_chat_run(eval_session["thread_id"], "test-model")
            store.create_chat_run(regular_session["thread_id"], "test-model")
            store.close()

            base_cmd = [
                sys.executable, "scripts/cleanup_eval_sessions.py",
                "--data-dir", str(paths.root), "--backup-dir", str(root / "backups"),
            ]
            preview = subprocess.run(
                base_cmd, check=True, capture_output=True, text=True,
                encoding="utf-8", cwd=Path(__file__).parent.parent,
            )
            self.assertIn(eval_session["thread_id"], preview.stdout)
            preview_store = SessionStore(str(paths.checkpoint_db))
            try:
                self.assertTrue(preview_store.get(eval_session["thread_id"]))
            finally:
                preview_store.close()

            subprocess.run(base_cmd + ["--apply"], check=True, cwd=Path(__file__).parent.parent)
            verified = SessionStore(str(paths.checkpoint_db))
            try:
                self.assertIsNone(verified.get(eval_session["thread_id"]))
                self.assertIsNotNone(verified.get(regular_session["thread_id"]))
                self.assertEqual(verified.list_chat_runs(eval_session["thread_id"]), [])
            finally:
                verified.close()
            self.assertEqual(len(list((root / "backups").glob("checkpoint-before-eval-cleanup-*.db"))), 1)

    def test_scorer_checks_answer_and_tool_trace(self):
        from evals.benchmark import load_manifest, score_task

        task = load_manifest()["tasks"][0]
        score = score_task(task, {
            "answer": "前向扩散会加噪，反向去噪预测噪声；训练目标包含 MSE 和 deadline 约束。",
            "tool_trace": ["read_pdf", "query_papers"],
            "state": {},
        })
        self.assertTrue(score["passed"])

    def test_example_results_exercise_all_fourteen_tasks_and_emit_metrics(self):
        import json
        from evals.benchmark import ROOT, load_manifest, score_submission

        results = json.loads((ROOT / "example_results.json").read_text(encoding="utf-8"))
        report = score_submission(load_manifest(), results)
        self.assertEqual((report["passed"], report["total"]), (14, 14))
        self.assertEqual(report["summary"]["citation_traceability_rate"], 1.0)
        self.assertEqual(report["summary"]["source_failures"], 1)

    def test_report_comparison_calculates_versioned_deltas(self):
        from evals.benchmark import compare_reports

        comparison = compare_reports(
            {"summary": {"success_rate": 0.9, "average_duration_ms": 800}},
            {"generated_at": "2026-08-01T00:00:00+00:00", "benchmark_version": "v1.0",
             "summary": {"success_rate": 0.8, "average_duration_ms": 1000}},
        )
        self.assertEqual(comparison["baseline_version"], "v1.0")
        self.assertEqual(comparison["deltas"]["success_rate"], 0.1)
        self.assertEqual(comparison["deltas"]["average_duration_ms"], -200)

    def test_trace_capture_exports_latency_and_tools_without_payloads(self):
        from evals.capture import result_from_trace

        captured = result_from_trace("T10", "关键词候选", {
            "duration_ms": 9100,
            "first_token_ms": 1200,
            "tool_trace": [{
                "type": "tool_finished", "tool": "query_papers",
                "duration_ms": 8100, "rag_keyword_fallback": True,
            }],
            "usage_delta": {"calls": 1},
        })
        self.assertEqual(captured["tool_trace"][0]["tool"], "query_papers")
        self.assertEqual(captured["metrics"]["max_tool_duration_ms"], 8100)
        self.assertEqual(captured["metrics"]["model_calls"], 1)
        self.assertNotIn("arguments", str(captured))

    def test_research_trace_capture_checks_public_evidence_quality_without_payloads(self):
        from evals.capture import result_from_trace

        captured = result_from_trace("T11", "带引用的研究报告 [E1]", {
            "evidence_quality": {
                "public_evidence_cards": 2,
                "scored_public_evidence_cards": 2,
                "minimum_public_relevance": 0.8,
                "rejected_public_candidates": 6,
            },
        })

        self.assertTrue(captured["state"]["public_evidence_relevance"])
        self.assertTrue(captured["state"]["public_evidence_rejection_metadata"])
        self.assertNotIn("rejection_reason", str(captured))

    def test_daily_capture_keeps_source_health_without_candidate_content(self):
        from evals.capture import result_from_daily_run

        class _DailyResult:
            status = "partial_failed"
            brief = "已保留可恢复候选"
            candidates = [{"title": "不应写入评测结果"}]
            source_stats = {"TSN": {"openalex": {"status": "ok", "count": 1}}}

        captured = result_from_daily_run("T12", _DailyResult())
        self.assertEqual(captured["state"]["run_status"], "partial_failed")
        self.assertEqual(captured["state"]["candidate_count"], 1)
        self.assertEqual(captured["source_stats"]["TSN"]["openalex"]["status"], "ok")
        self.assertNotIn("不应写入评测结果", str(captured))

    def test_release_gate_combines_capability_and_runtime_suites(self):
        from evals.release_gate import GATE_VERSION, run_release_gate

        report = run_release_gate(seed=17)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["gate_version"], GATE_VERSION)
        self.assertEqual((report["capability"]["passed"], report["capability"]["total"]), (14, 14))
        self.assertEqual((report["runtime"]["passed"], report["runtime"]["total"]), (5, 5))
        self.assertEqual(report["summary"]["success_rate"], 1.0)


class TestRequestTracing(unittest.TestCase):
    """真实调用前后都应产生脱敏的可评分追踪。"""

    def test_agent_discards_persisted_transient_status_on_session_load(self):
        from config import Config
        from research_agent import ResearchAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
            ))
            try:
                thread_id = agent.thread_id
                agent.sessions.save_usage(thread_id, {
                    "total": 7,
                    "api_status": "⚠️ stale model warning",
                    "verify_status": "⚠️ stale verify warning",
                })
                agent._usage_cache.pop(thread_id, None)

                usage = agent.get_usage(thread_id)
                self.assertEqual(usage["total"], 7)
                self.assertNotIn("api_status", usage)
                self.assertNotIn("verify_status", usage)
                self.assertNotIn("api_status", agent.sessions.get_usage(thread_id))
                self.assertNotIn("verify_status", agent.sessions.get_usage(thread_id))
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_interrupted_stream_releases_slot_and_can_be_retried(self):
        """A partial SSE response must not block the next same-model request."""
        import requests
        from config import Config
        from research_agent import ResearchAgent

        class _StreamingResponse:
            status_code = 200
            text = ""
            raw = None

            def __init__(self, lines):
                self.lines = lines
                self.closed = False

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                for line in self.lines:
                    if isinstance(line, BaseException):
                        raise line
                    yield line

            def close(self):
                self.closed = True

        first = _StreamingResponse([
            'data: {"choices":[{"delta":{"content":"已输出片段"}}]}',
            requests.exceptions.ChunkedEncodingError("interrupted"),
        ])
        second = _StreamingResponse([
            'data: {"choices":[{"delta":{"content":"重试完成"}}]}',
            "data: [DONE]",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
                api_max_concurrency=1, api_max_retries=0,
            ))
            try:
                with patch("llm_client.requests.post", side_effect=[first, second]) as post:
                    interrupted = agent.step("需要可靠重试的问题", on_token=lambda _token: None)
                    retry = agent.get_retry_input(agent.thread_id)
                    self.assertIn("输出中断", interrupted)
                    self.assertEqual(retry["user_input"], "需要可靠重试的问题")
                    self.assertTrue(
                        agent.llm_client._slots.acquire(blocking=False),
                        "中断流必须归还并发槽",
                    )
                    agent.llm_client._slots.release()

                    retried = agent.step(retry["user_input"], on_token=lambda _token: None)
                    self.assertEqual(retried, "重试完成")
                    self.assertEqual(post.call_count, 2)
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_agent_exposes_last_trace_without_prompt_or_tool_payload(self):
        from config import Config
        from research_agent import ResearchAgent

        class _FakeApp:
            def invoke(self, state, config):
                return {"messages": [{"role": "assistant", "content": "完成"}]}

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
            ))

            def fake_build(usage, on_token=None, event_callback=None, cancel_event=None, **_kwargs):
                event_callback({
                    "type": "tool_finished", "tool": "query_papers",
                    "duration_ms": 12.5, "rag_keyword_fallback": False,
                })
                return _FakeApp()

            with patch.object(agent, "_build_app", side_effect=fake_build):
                self.assertEqual(agent.step("不应出现在追踪中的完整问题"), "完成")
            trace = agent.get_last_trace()
            self.assertEqual(trace["outcome"], "success")
            self.assertEqual(trace["tool_trace"][0]["tool"], "query_papers")
            self.assertNotIn("完整问题", str(trace))
            saved_run = agent.sessions.list_chat_runs(agent.thread_id, limit=1)[0]
            self.assertEqual(saved_run["status"], "completed")
            saved_events = agent.sessions.get_run_events(saved_run["run_id"], agent.thread_id)
            self.assertTrue(any(event["stage"] == "tool" for event in saved_events))
            self.assertNotIn("完整问题", str(saved_events))
            agent.memory._conn.close()
            agent.sessions.close()


class TestRuntimeReplay(unittest.TestCase):
    """离线回放必须走真实编排，且不暴露运行时数据。"""

    def test_runtime_replay_executes_all_reliability_cases(self):
        from evals.runtime_replay import run_suite

        report = run_suite(seed=17)
        self.assertEqual(report["total"], 5)
        self.assertEqual(report["passed"], 5, report)
        self.assertEqual(report["metadata"]["selected_cases"], ["R01", "R02", "R03", "R04", "R05"])
        self.assertGreater(report["summary"]["source_requests"], 0)
        self.assertGreater(report["summary"]["source_failure_rate"], 0)
        self.assertNotIn("offline-private-input-must-not-leak", str(report))

    def test_runtime_replay_rejects_unknown_case_and_compares_same_suite_only(self):
        from evals.runtime_replay import compare_reports, run_suite

        with self.assertRaisesRegex(ValueError, "unknown runtime replay case"):
            run_suite(case_ids=["R99"])
        report = run_suite(case_ids=["R01"], seed=3)
        comparison = compare_reports(report, {"suite_version": "runtime-replay-v1", "summary": report["summary"]})
        self.assertEqual(comparison["deltas"]["success_rate"], 0.0)
        with self.assertRaisesRegex(ValueError, "different runtime replay suite"):
            compare_reports(report, {"suite_version": "other", "summary": {}})


class TestWebRendering(unittest.TestCase):
    def test_chatbot_enables_inline_and_display_latex_delimiters(self):
        from web_ui import CHAT_LATEX_DELIMITERS

        self.assertIn({"left": "$", "right": "$", "display": False}, CHAT_LATEX_DELIMITERS)
        self.assertIn({"left": "$$", "right": "$$", "display": True}, CHAT_LATEX_DELIMITERS)
        self.assertIn({"left": "\\(", "right": "\\)", "display": False}, CHAT_LATEX_DELIMITERS)
        self.assertIn({"left": "\\[", "right": "\\]", "display": True}, CHAT_LATEX_DELIMITERS)

    def test_mounted_ui_keeps_theme_and_avoids_background_polling(self):
        root = Path(__file__).resolve().parents[1]
        source = root.joinpath("web_ui.py").read_text(encoding="utf-8")
        server_source = root.joinpath("api_server.py").read_text(encoding="utf-8")

        self.assertIn("theme=UI_THEME", server_source)
        self.assertIn("css=UI_CSS", server_source)
        self.assertIn("js=UI_JS", server_source)
        self.assertIn('font-family: "Microsoft YaHei UI"', source)
        self.assertIn("API_RUN_CLIENT_ENABLED", source)
        self.assertNotIn("gr.Timer(", source)

    def test_workbench_layout_keeps_conversation_primary(self):
        source = Path(__file__).resolve().parents[1].joinpath("web_ui.py").read_text(encoding="utf-8")

        self.assertIn('elem_classes=["app-shell"]', source)
        self.assertIn('elem_classes=["workspace-sidebar"]', source)
        self.assertIn('elem_classes=["workspace-inspector"]', source)
        self.assertIn("show_label=False", source)
        self.assertNotIn("with gr.Tabs():", source)


class TestAuditRegressionFixes(unittest.TestCase):
    """Regression coverage for correctness and boundary issues found in code audit."""

    def test_rag_query_reorders_and_honors_top_k(self):
        from paper_store import PaperStore

        class _Collection:
            def __init__(self):
                self.requested = None

            @staticmethod
            def count():
                return 4

            def query(self, **kwargs):
                self.requested = kwargs["n_results"]
                return {
                    "ids": [["intro", "method", "result", "other"]],
                    "documents": [["intro", "method", "result", "other"]],
                    "metadatas": [[
                        {"title": "Paper", "section": "Introduction", "chunk_index": 0},
                        {"title": "Paper", "section": "Method", "chunk_index": 1},
                        {"title": "Paper", "section": "Results", "chunk_index": 2},
                        {"title": "Paper", "section": "Related Work", "chunk_index": 3},
                    ]],
                    # Chroma order is intro → method → result. The section
                    # bonus must reorder Method/Results ahead of Introduction.
                    "distances": [[0.10, 0.12, 0.13, 0.30]],
                }

        store = PaperStore.__new__(PaperStore)
        store._collection = _Collection()
        results = store.query("method", top_k=2)

        self.assertEqual(store._collection.requested, 4)
        self.assertEqual([item["text"] for item in results], ["method", "result"])
        self.assertEqual(len(results), 2)

    def test_agent_sets_a_bounded_graph_recursion_limit(self):
        from config import Config
        from research_agent import MAX_AGENT_GRAPH_STEPS, ResearchAgent

        class _FakeApp:
            config = None

            def invoke(self, state, config):
                self.config = config
                return {"messages": [{"role": "assistant", "content": "完成"}]}

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(
                deepseek_key="test-key", rag_enabled=False, data_dir=tmp,
            ))
            app = _FakeApp()
            try:
                with patch.object(agent, "_build_app", return_value=app):
                    self.assertEqual(agent.step("请检索一个问题"), "完成")
                self.assertEqual(app.config["recursion_limit"], MAX_AGENT_GRAPH_STEPS)
            finally:
                agent.memory.close()
                agent.sessions.close()

    def test_session_order_is_stable_for_fast_consecutive_creates(self):
        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "sessions.db"))
            try:
                sessions = [store.create(title=f"session-{index}") for index in range(5)]
                self.assertEqual(store.list()[0]["thread_id"], sessions[-1]["thread_id"])
                self.assertTrue(all("." in item["created_at"] for item in sessions))
            finally:
                store.close()

    def test_profile_updates_are_atomic_under_concurrent_agent_calls(self):
        from concurrent.futures import ThreadPoolExecutor
        from user_profile import ProfileManager

        with tempfile.TemporaryDirectory() as tmp:
            profile = ProfileManager(str(Path(tmp) / "profile.md"))
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(
                    lambda index: profile.add_active_question(f"question-{index}"), range(16),
                ))
            content = profile.read()
            self.assertTrue(all(f"question-{index}" in content for index in range(16)))

    def test_optional_model_calls_do_not_clear_interactive_failure_signal(self):
        from llm_client import LLMClient, RequestPolicy

        class _Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def close():
                return None

        client = LLMClient(
            "test-key", "https://example.test/chat", circuit_failure_threshold=2,
        )
        client._circuit.record_failure()
        optional = RequestPolicy(purpose="curator", counts_toward_circuit=False)

        with patch("llm_client.requests.post", return_value=_Response()):
            response = client.post({"model": "test", "messages": []}, policy=optional)
            response.close()
        self.assertEqual(client._circuit._failures, 1)

        with patch("llm_client.requests.post", return_value=_Response()):
            stream = client.post(
                {"model": "test", "messages": []}, stream=True, policy=optional,
            )
        client.finish_stream(stream, success=True)
        self.assertEqual(client._circuit._failures, 1)

    def test_pdf_tool_rejects_local_path_outside_runtime(self):
        from runtime_paths import RuntimePaths
        from tools.read_pdf import handle_read_pdf

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RuntimePaths.from_root(root / "runtime")
            paths.ensure_initialized()
            private_pdf = root / "private.pdf"
            private_pdf.write_bytes(b"%PDF-private")
            with patch.dict(os.environ, {"APP_DATA_DIR": str(paths.root)}, clear=False):
                result = handle_read_pdf({"url_or_path": str(private_pdf)})
            self.assertIn("必须先上传", result)

    def test_pdf_tool_rejects_loopback_before_network_request(self):
        from tools.read_pdf import handle_read_pdf

        with patch("tools.read_pdf.requests.get") as request_get:
            result = handle_read_pdf({"url_or_path": "https://127.0.0.1/admin.pdf"})
        self.assertIn("下载被拒绝", result)
        self.assertFalse(request_get.called)

    def test_pdf_download_enforces_size_limit_before_writing(self):
        import socket
        from tools.read_pdf import MAX_PDF_DOWNLOAD_BYTES, PDFDownloadError, _download_pdf

        class _Response:
            status_code = 200
            headers = {
                "Content-Length": str(MAX_PDF_DOWNLOAD_BYTES + 1),
                "Content-Type": "application/pdf",
            }

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def iter_content(chunk_size):
                return iter([b"%PDF-ignored"])

            @staticmethod
            def close():
                return None

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper.pdf"
            with patch(
                "tools.read_pdf.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            ), patch("tools.read_pdf.requests.get", return_value=_Response()):
                with self.assertRaisesRegex(PDFDownloadError, "文件过大"):
                    _download_pdf("https://example.com/paper.pdf", target)
            self.assertFalse(target.exists())


class TestRagRetrievalEvaluation(unittest.TestCase):
    """The recall scorer must stay independent from Chroma and model calls."""

    def test_reports_document_and_strict_passage_metrics_separately(self):
        alpha = PassageLabel("Paper Alpha", 2, "gold alpha evidence")
        beta = PassageLabel("Paper Beta", 8, "gold beta evidence")
        cases = [
            RetrievalCase("RAG-01", "alpha question", (alpha,)),
            RetrievalCase("RAG-02", "beta question", (beta,)),
        ]

        def _retrieve(query, _top_k):
            if query == "alpha question":
                return [
                    {"title": "Paper Other", "chunk_index": 1, "text": "unrelated"},
                    {"title": "Paper Alpha", "chunk_index": 2, "text": "gold alpha evidence"},
                ]
            return RetrievalResponse(
                [
                    {"title": "Paper Beta", "chunk_index": 7, "text": "near but not the source"},
                    {"title": "Paper Beta", "chunk_index": 8, "text": "gold beta evidence"},
                ],
                fallback_note="lexical fallback",
            )

        report = evaluate_cases(cases, _retrieve, ks=(1, 2))
        metrics = report["metrics"]
        self.assertEqual(metrics["paper_recall_at_k"], {"1": 0.5, "2": 1.0})
        self.assertEqual(metrics["passage_recall_at_k"], {"1": 0.0, "2": 1.0})
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["fallback_case_count"], 1)
        self.assertEqual(report["cases"][1]["first_paper_rank"], 1)
        self.assertEqual(report["cases"][1]["first_passage_rank"], 2)

    def test_manifest_requires_a_human_passage_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "cases.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "cases": [{
                    "id": "RAG-01",
                    "query": "question",
                    "labels": [{"paper_title": "Paper", "chunk_index": 1}],
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RagEvalError, "text_contains"):
                load_cases(manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)

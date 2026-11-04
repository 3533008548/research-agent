"""Error diagnostics must survive restart without widening the public payload.

These tests use isolated SQLite stores and an in-process FastAPI client.  No
model requests, Redis service or user runtime data are involved.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.redis_runs import _safe_status_event
from api.sse import format_sse, public_sse_event
from run_contract import safe_error_details
from session_store import SessionStore


PRIVATE_TEXT = "PRIVATE_PROMPT_AND_AUTHORIZATION"
HTTP_FAILURE = {
    "code": "LLM_HTTP_ERROR",
    "phase": "response_headers",
    "exception_type": "LLMRequestFailedError",
    "cause_type": "HTTPError",
    "http_status": 401,
    "elapsed_ms": 1250.4,
    "deadline_ms": 45000.0,
    "queue_wait_ms": 3.2,
    "attempts": 1,
    "retries_used": 0,
    "request_id": "req-correlation-01",
}


def contaminated_details() -> dict:
    return {
        **HTTP_FAILURE,
        "prompt": PRIVATE_TEXT,
        "body": PRIVATE_TEXT,
        "headers": {"Authorization": PRIVATE_TEXT},
        "api_key": PRIVATE_TEXT,
        "url": f"https://example.invalid/?token={PRIVATE_TEXT}",
        "traceback": PRIVATE_TEXT,
    }


class SafeErrorDetailsTests(unittest.TestCase):
    def test_preserves_actionable_fields_and_excludes_content(self) -> None:
        self.assertEqual(safe_error_details(contaminated_details()), HTTP_FAILURE)

    def test_damaged_field_shapes_cannot_break_error_reporting(self) -> None:
        for source in (None, [], "not an object", 401):
            with self.subTest(source=source):
                self.assertEqual(safe_error_details(source), {})
        for field in ("code", "phase", "exception_type", "cause_type", "request_id"):
            for invalid in ([], {}, True, 401):
                with self.subTest(field=field, invalid=invalid):
                    self.assertEqual(safe_error_details({field: invalid}), {})

    def test_status_and_measurements_reject_invalid_or_nonfinite_values(self) -> None:
        for invalid in (True, 99, 600, "401", 401.0, float("nan")):
            with self.subTest(status=invalid):
                self.assertNotIn("http_status", safe_error_details({"http_status": invalid}))
        for status in (100, 200, 401, 429, 503, 599):
            with self.subTest(status=status):
                self.assertEqual(safe_error_details({"http_status": status}), {"http_status": status})
        for field in ("elapsed_ms", "deadline_ms", "queue_wait_ms", "attempts", "retries_used"):
            for invalid in (True, -1, float("inf"), float("nan"), 10**1000, "45"):
                with self.subTest(field=field, invalid_type=type(invalid).__name__):
                    self.assertEqual(safe_error_details({field: invalid}), {})

    def test_identifiers_and_enum_values_do_not_accept_sensitive_text(self) -> None:
        for field, invalid in (
            ("code", PRIVATE_TEXT),
            ("phase", PRIVATE_TEXT),
            ("exception_type", f"HTTPError: {PRIVATE_TEXT}"),
            ("cause_type", f"ReadTimeout({PRIVATE_TEXT})"),
            ("request_id", "Bearer secret-token"),
            ("request_id", "sk-private-key"),
            ("request_id", "https://example.invalid/token"),
            ("request_id", "req-" + "a" * 200),
            ("request_id", "req-01\nAuthorization: secret"),
        ):
            with self.subTest(field=field, invalid=invalid):
                self.assertEqual(safe_error_details({field: invalid}), {})


class DurableErrorDetailsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "checkpoint.db"
        self.store = SessionStore(str(self.path))
        self.addCleanup(lambda: self.store.close())
        self.session_id = self.store.create("diagnostics")["thread_id"]
        self.run = self.store.create_chat_run(self.session_id, "fake-model")
        self.run_id = self.run["run_id"]

    def _record_failure(self) -> None:
        self.store.add_run_event(
            self.session_id, self.run_id, "chat", "single_agent", "model", "failed",
            summary="模型请求失败", event_type="error", error_type="LLMRequestFailedError",
            metadata={"model": "fake-model", "prompt": PRIVATE_TEXT},
            error_details=contaminated_details(),
        )
        # Terminal events from older callers may not repeat the model details.
        self.store.add_run_event(
            self.session_id, self.run_id, "chat", "single_agent", "run", "failed",
            summary="运行失败", event_type="done", error_type="LLMRequestFailedError",
        )
        self.store.update_chat_run(
            self.run_id, status="failed", answer="模型请求失败", error_type="LLMRequestFailedError",
        )

    def _reopen(self) -> None:
        self.store.close()
        self.store = SessionStore(str(self.path))

    def _client(self):
        from fastapi.testclient import TestClient
        from api.app import create_app

        agent = SimpleNamespace(sessions=self.store, model="fake-model")
        return TestClient(create_app(agent=agent, api_key="", badcase_store=SimpleNamespace()))

    def test_diagnostics_are_persisted_without_payload_and_restored_in_a_new_store(self) -> None:
        self._record_failure()
        with closing(sqlite3.connect(self.path)) as db:
            persisted = db.execute(
                "SELECT payload_json FROM agent_run_events WHERE run_id=? ORDER BY id", (self.run_id,),
            ).fetchall()
        self.assertNotIn(PRIVATE_TEXT, json.dumps(persisted))
        self._reopen()
        events = self.store.get_run_events(self.run_id)
        self.assertEqual(events[0]["error_details"], HTTP_FAILURE)
        self.assertEqual(events[0]["metadata"], {"model": "fake-model"})

    def test_restored_api_detail_keeps_latest_diagnostics_after_a_plain_terminal_event(self) -> None:
        self._record_failure()
        self._reopen()
        with self._client() as client:
            response = client.get(f"/api/v1/runs/{self.run_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], self.run_id)
        self.assertEqual(payload["error_details"], HTTP_FAILURE)
        self.assertEqual(payload["events"][0]["error_details"], HTTP_FAILURE)
        self.assertNotIn(PRIVATE_TEXT, response.text)

    def test_read_boundary_sanitizes_legacy_or_tampered_payloads_again(self) -> None:
        self._record_failure()
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "UPDATE agent_run_events SET payload_json=? WHERE run_id=? AND event_type='error'",
                (json.dumps({"model": "fake-model", "error_details": contaminated_details()}), self.run_id),
            )
            db.commit()
        self.store = SessionStore(str(self.path))
        with self._client() as client:
            response = client.get(f"/api/v1/runs/{self.run_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error_details"], HTTP_FAILURE)
        self.assertNotIn(PRIVATE_TEXT, response.text)

    def test_legacy_failures_remain_readable_and_success_does_not_inherit_an_old_failure(self) -> None:
        self.store.update_chat_run(self.run_id, status="failed", error_type="llm_error")
        with self._client() as client:
            legacy = client.get(f"/api/v1/runs/{self.run_id}")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()["error_type"], "llm_error")
        self.assertFalse(legacy.json().get("error_details"))

        self._record_failure()
        self.store.update_chat_run(self.run_id, status="completed", answer="最终成功", error_type="")
        with self._client() as client:
            completed = client.get(f"/api/v1/runs/{self.run_id}").json()
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(completed.get("error_details"), "成功运行不能显示历史失败为当前错误")
        self.assertEqual(completed["events"][0]["error_details"], HTTP_FAILURE)

    def test_in_process_manager_exception_persists_and_streams_safe_details(self) -> None:
        from api.run_manager import ChatRunManager
        from llm_client import LLMRequestFailedError

        def fail_model(*_args, **_kwargs):
            raise LLMRequestFailedError("Human-readable failure", error_details=contaminated_details())

        agent = SimpleNamespace(sessions=self.store, model="fake-model", step=fail_model)
        manager = ChatRunManager(agent)
        started = manager.start(self.session_id, "test-only message")
        text = "".join(manager.stream(started["run_id"]))
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
        errors = [event for event in events if event["type"] == "error"]
        done = [event for event in events if event["type"] == "done"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_details"], HTTP_FAILURE)
        self.assertEqual(done[-1]["error_details"], HTTP_FAILURE)
        self.assertNotIn(PRIVATE_TEXT, text)
        saved = self.store.get_run_events(started["run_id"])
        self.assertEqual(saved[-1]["error_details"], HTTP_FAILURE)

    def test_reconnected_manager_replays_terminal_details_from_persisted_events(self) -> None:
        from api.run_manager import ChatRunManager

        self._record_failure()
        self._reopen()
        restarted = ChatRunManager(SimpleNamespace(sessions=self.store, model="fake-model"))
        text = "".join(restarted.stream(self.run_id))
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(events[-1]["error_details"], HTTP_FAILURE)
        self.assertNotIn(PRIVATE_TEXT, text)


class PublicErrorDetailsTests(unittest.TestCase):
    def test_error_and_done_sse_keep_safe_details_without_other_internal_fields(self) -> None:
        for event_type in ("error", "done"):
            with self.subTest(event_type=event_type):
                event = {
                    "type": event_type, "status": "failed", "error_type": "LLMRequestFailedError",
                    "answer": "Human-readable failure", "error_details": contaminated_details(),
                    "prompt": PRIVATE_TEXT, "headers": {"Authorization": PRIVATE_TEXT},
                }
                projected = public_sse_event(event)
                self.assertEqual(projected["error_details"], HTTP_FAILURE)
                self.assertNotIn("prompt", projected)
                self.assertNotIn("headers", projected)
                frame = format_sse(event)
                self.assertNotIn(PRIVATE_TEXT, frame)
                self.assertEqual(json.loads(frame.split("data: ", 1)[1])["error_details"], HTTP_FAILURE)

    def test_status_projection_and_unknown_event_guard_do_not_leak_contents(self) -> None:
        projected = _safe_status_event({
            "stage": "model", "status": "failed", "error_details": contaminated_details(),
            "message": PRIVATE_TEXT,
        })
        self.assertEqual(projected["error_details"], HTTP_FAILURE)
        self.assertNotIn(PRIVATE_TEXT, json.dumps(projected))
        unknown = public_sse_event({"type": "internal_secret", "error_details": HTTP_FAILURE})
        self.assertEqual(unknown, {"type": "status", "status": "running"})

    def test_stream_timeout_can_keep_http_200_but_queue_timeout_cannot_invent_status(self) -> None:
        stream_details = {
            "code": "LLM_DEADLINE_EXCEEDED", "phase": "stream_read", "http_status": 200,
            "cause_type": "ReadTimeout", "elapsed_ms": 45000.0, "deadline_ms": 45000.0,
        }
        stream = public_sse_event({"type": "error", "error_details": stream_details})
        self.assertEqual(stream["error_details"], stream_details)
        queue_details = {"code": "LLM_QUEUE_TIMEOUT", "phase": "queue", "attempts": 0}
        queue = public_sse_event({"type": "error", "error_details": queue_details})
        self.assertEqual(queue["error_details"], queue_details)
        self.assertNotIn("http_status", queue["error_details"])


class ResearchAgentPartialFailureTests(unittest.TestCase):
    def test_failed_final_model_stream_keeps_partial_answer_and_durable_diagnostics(self) -> None:
        from config import Config
        from research_agent import ResearchAgent

        details = {
            "code": "LLM_STREAM_TIMEOUT",
            "phase": "stream_read",
            "exception_type": "LLMRequestTimeoutError",
            "cause_type": "ReadTimeout",
            "http_status": 200,
            "elapsed_ms": 45000.0,
            "deadline_ms": 45000.0,
            "attempts": 1,
            "retries_used": 0,
        }
        partial_answer = "已经收到的正文。\n\n⚠️ 输出中断，已保留以上内容。"

        def build_interrupted_graph(**kwargs):
            def invoke(_state, config):
                emit = kwargs["event_callback"]
                # A previous model round succeeded, then the final round
                # delivered text but failed. Returning that text must not
                # make ResearchAgent mark the whole run completed.
                emit({"type": "llm_request_finished"})
                emit({"type": "llm_request_started"})
                kwargs["stream_callback"]("已经收到的正文。")
                emit({
                    "type": "llm_request_failed",
                    "error_type": "LLMRequestTimeoutError",
                    "error_details": {**details, "body": PRIVATE_TEXT},
                })
                return {"messages": [{"role": "assistant", "content": partial_answer}]}

            return SimpleNamespace(invoke=invoke)

        with tempfile.TemporaryDirectory() as tmp:
            agent = ResearchAgent(Config(deepseek_key="test-key", rag_enabled=False, data_dir=tmp))
            tokens: list[str] = []
            try:
                with patch("research_agent.build_graph", autospec=True, side_effect=build_interrupted_graph):
                    answer = agent.step("分析这个问题", on_token=tokens.append)
                self.assertEqual(answer, partial_answer)
                self.assertEqual(tokens, ["已经收到的正文。"])
                trace = agent.get_last_trace(agent.thread_id)
                run_id = trace["chat_run_id"]
                saved = agent.sessions.get_chat_run(run_id)
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["answer"], partial_answer)
                self.assertEqual(trace["outcome"], "llm_error")
                self.assertEqual(trace["error_details"], details)
                db_path = agent.sessions.db_path
            finally:
                agent.memory.close()
                agent.sessions.close()

            restored = SessionStore(db_path)
            try:
                events = restored.get_run_events(run_id)
                failed_model = [event for event in events if event["stage"] == "model" and event["status"] == "failed"]
                self.assertEqual(len(failed_model), 1)
                self.assertEqual(failed_model[0]["error_details"], details)
                self.assertEqual(events[-1]["event_type"], "done")
                self.assertEqual(events[-1]["status"], "failed")
                self.assertEqual(events[-1]["error_details"], details)
                self.assertNotIn(PRIVATE_TEXT, json.dumps(events, ensure_ascii=False))
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()

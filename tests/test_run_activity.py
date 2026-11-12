"""Tool lifecycle observability must precede completion and survive persistence."""
import json
import tempfile
import unittest
from pathlib import Path
from api.sse import public_sse_event
from research_projects import ResearchProjects
from run_contract import graph_activity_event
from tool_runtime import ToolRuntime, ToolExecutionContext


class TestRunActivity(unittest.TestCase):
    def test_verification_verdict_and_round_survive_projection(self):
        event = public_sse_event(graph_activity_event({"type": "verify_unresolved", "verification_round": 2,
            "feedback": "PRIVATE"}))
        self.assertEqual(event["status"], "unresolved")
        self.assertEqual(event["verification_round"], 2)
        self.assertNotIn("PRIVATE", json.dumps(event))

    def test_verify_diagnostics_survive_live_event_projection(self):
        event = public_sse_event(graph_activity_event({"type": "verify_request_failed", "duration_ms": 45010,
            "error_type": "LLMRequestTimeoutError", "error_details": {"code": "LLM_RESPONSE_TIMEOUT",
                "deadline_ms": 45000, "elapsed_ms": 45010, "attempts": 1, "retries_used": 0, "response_body": "PRIVATE"}}))
        self.assertEqual(event["stage"], "verify")
        self.assertEqual(event["error_type"], "LLMRequestTimeoutError")
        self.assertEqual(event["error_details"]["deadline_ms"], 45000)
        self.assertNotIn("PRIVATE", json.dumps(event))

    def test_runtime_emits_start_before_executor_and_reports_result_errors(self):
        events = []
        def execute(*args, **kwargs):
            self.assertEqual(events[-1]["status"], "running")
            return "❌ PRIVATE_TOOL_RESULT"
        runtime = ToolRuntime(ToolExecutionContext(), executor=execute,
            event_callback=lambda kind, **fields: events.append(public_sse_event(graph_activity_event({"type": kind, **fields}))))
        runtime.execute("read_pdf", {"url_or_path": "PRIVATE_ARGUMENT"})
        runtime.execute("read_pdf", {"url_or_path": "PRIVATE_ARGUMENT"})
        self.assertEqual([e["status"] for e in events], ["running", "failed", "running", "failed"])
        self.assertEqual(events[0]["call_id"], events[1]["call_id"])
        self.assertNotEqual(events[0]["call_id"], events[2]["call_id"])
        self.assertEqual(events[1]["error_type"], "ToolResultError")
        self.assertGreaterEqual(events[1]["duration_ms"], 0)
        self.assertNotIn("PRIVATE", json.dumps(events))

    def test_research_events_survive_restart_and_keep_parallel_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            store = ResearchProjects(path)
            try:
                project = store.create("research-a", "deleted-session", "PRIVATE_QUESTION")
                for index, role in enumerate(("researcher1", "researcher2")):
                    job = dict(project_id="research-a", job_id=f"job{index}", role=role, version=1, attempts=1)
                    store.record_activity(job, dict(type="tool_started", tool="read_pdf", call_id=f"call{index}", arguments="PRIVATE_ARGUMENT"))
                self.assertGreater(store.get("research-a")["updated_at"], project["updated_at"])
            finally:
                store.close()
            store = ResearchProjects(path)
            try:
                events = store.activity("research-a")
                self.assertEqual([e["agent"] for e in events], ["researcher1", "researcher2"])
                self.assertEqual([e["call_id"] for e in events], ["call0", "call1"])
                self.assertNotIn("PRIVATE", json.dumps(events))
            finally:
                store.close()

    def test_cancelled_tool_is_not_success_and_unknown_payload_is_dropped(self):
        event = graph_activity_event({"type": "tool_failed", "tool": "read_pdf", "call_id": "a",
            "error_type": "RequestCancelledError", "result": "PRIVATE"})
        self.assertEqual(public_sse_event(event)["status"], "cancelled")
        self.assertNotIn("PRIVATE", json.dumps(event))
        self.assertIsNone(graph_activity_event({"type": "private_thought", "text": "PRIVATE"}))

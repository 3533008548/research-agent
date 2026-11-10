"""Durability and lifecycle contracts for persistent four-agent research."""
import json
import os
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_projects import ResearchProjects, ResearchBudgetExceeded
from session_store import SessionStore
from autonomous_research import AutonomousResearch, BudgetedClient, ResearchResponseError


class TestPersistentResearch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.db")
        self.sessions = SessionStore(self.path)
        self.store = ResearchProjects(self.path)
        self.store.create("research-test", "session", "比较两种方法", "public")

    def tearDown(self):
        self.store.close()
        self.sessions._conn.close()
        self.temp.cleanup()

    def next(self, role="lead"):
        job = next(j for j in self.store.jobs("research-test") if j["state"] == "queued" and j["role"] == role)
        return self.store.claim(job["job_id"])

    def dispatch(self):
        job = self.next()
        self.store.complete(job, {"action": "research", "criteria": ["核对条件和反例"], "tasks": [
            {"gap": "性能", "question": "查方法A条件", "expected": "全文条件", "difference": "首次"},
            {"gap": "反例", "question": "查方法B反例", "expected": "反例", "difference": "首次"}]})

    def test_parallel_jobs_have_no_parent_or_session_lock(self):
        from api.run_dispatcher import _Entry
        from api.redis_runs import QueuedResearchRun
        self.dispatch()
        jobs = [j for j in self.store.jobs("research-test") if j["state"] == "queued"]
        self.assertEqual([j["role"] for j in jobs], ["researcher1", "researcher2"])
        resources = [_Entry("research", None, QueuedResearchRun(str(i), "research-test", "session", "", "public", job_id=j["job_id"])).resources for i, j in enumerate(jobs)]
        self.assertFalse(resources[0] & resources[1])
        self.assertTrue(all("session_id:session" not in value for value in resources))

    def test_lead_only_wakes_after_both_workers_and_duplicate_is_ignored(self):
        self.dispatch()
        first = self.next("researcher1")
        second = self.next("researcher2")
        result = {"evidence": [{"source": "paper-a", "excerpt": "original text"}], "answer": "局部结论"}
        self.store.complete(first, result)
        self.store.complete(first, result)
        self.assertFalse(any(j["role"] == "lead" and j["state"] == "queued" for j in self.store.jobs("research-test")))
        self.store.complete(second, result)
        self.assertEqual(len(self.store.get("research-test")["evidence"]), 1)
        self.assertEqual(sum(j["role"] == "lead" and j["state"] == "queued" for j in self.store.jobs("research-test")), 1)

    def test_goal_change_archives_late_result_without_overwriting(self):
        self.dispatch()
        job = self.next("researcher1")
        self.store.control("research-test", "steer", message="只比较同样负载", event_id="steer-one")
        self.store.control("research-test", "steer", message="只比较同样负载", event_id="steer-one")
        self.store.complete(job, {"evidence": [{"source": "old", "excerpt": "late"}]})
        project = self.store.get("research-test")
        self.assertEqual(project["goal_version"], 2)
        self.assertEqual(project["constraints"], ["只比较同样负载"])
        self.assertFalse(project["evidence"])
        self.assertEqual(next(j for j in self.store.jobs("research-test") if j["job_id"] == job["job_id"])["state"], "stale")

    def test_stop_survives_restart_and_late_result_never_dispatches(self):
        job = self.next()
        self.store.control("research-test", "stop")
        self.store.close()
        self.store = ResearchProjects(self.path)
        self.store.complete(job, {"action": "research", "tasks": []})
        self.store.control("research-test", "material", message="paper:new")
        self.assertEqual(self.store.get("research-test")["execution_state"], "stopped")
        self.assertEqual(self.store.outbox(), [])

    def test_pause_steer_resume_preserves_evidence_and_budget(self):
        job = self.next()
        reservation = self.store.reserve(job, 100)
        self.store.settle(reservation, 35)
        self.store.control("research-test", "pause")
        self.store.control("research-test", "steer", message="缩小条件")
        self.assertEqual(self.store.get("research-test")["execution_state"], "paused")
        project = self.store.control("research-test", "resume")
        self.assertEqual(project["execution_state"], "active")
        self.assertEqual(project["budget"]["tokens"], 35)
        self.assertEqual(project["budget"]["calls"], 1)

    def test_material_wait_does_not_call_model_and_only_matching_event_wakes(self):
        self.store.complete(self.next(), {"action": "wait", "waiting_for": ["paper:abc"]})
        self.assertEqual(self.store.outbox(), [])
        self.store.control("research-test", "material", message="paper:other")
        self.assertEqual(self.store.outbox(), [])
        self.store.control("research-test", "material", message="paper:abc", event_id="ready")
        self.store.control("research-test", "material", message="paper:abc", event_id="ready")
        self.assertEqual(len(self.store.outbox()), 1)

    def test_crash_reservation_is_charged_and_replay_is_bounded(self):
        job = self.next()
        self.store.reserve(job, 500)
        self.store.close()
        self.store = ResearchProjects(self.path)
        replay = self.store.claim(job["job_id"])
        self.assertEqual(replay["attempts"], 2)
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 500)
        self.assertIsNone(self.store.claim(job["job_id"]))
        self.assertEqual(self.store.get("research-test")["execution_state"], "ended")

    def test_budget_failure_keeps_report_reserve_and_never_resets(self):
        job = self.next()
        with self.assertRaises(ResearchBudgetExceeded):
            self.store.reserve(job, 400000)
        self.store.complete(job, {"error": "budget"})
        report = self.next("report")
        self.store.reserve(report, 1000)
        self.store.complete(report, {"answer": "证据不足"})
        critic = self.next("critic")
        self.store.complete(critic, {"verdict": "unknown", "issues": []})
        self.store.complete(self.next(), {"action": "finish", "criteria_met": False})
        self.assertEqual(self.store.get("research-test")["execution_state"], "budget_exhausted")
        self.store.control("research-test", "resume")
        self.assertEqual(self.store.get("research-test")["budget"]["calls"], 1)

    def test_deleting_conversation_retains_research_and_does_not_block_chat_fifo(self):
        session = self.sessions.create("test")
        run = self.sessions.create_research_run(session["thread_id"], "q")
        self.store.create(run["run_id"], session["thread_id"], "q")
        self.assertIsNone(self.sessions.first_pending_run(session["thread_id"]))
        self.sessions.delete(session["thread_id"])
        self.sessions.cleanup_orphaned_checkpoints()
        self.assertIsNotNone(self.store.get(run["run_id"]))

    def test_full_evidence_kept_and_snapshot_mapping_is_immutable(self):
        self.dispatch()
        self.store.complete(self.next("researcher1"), {"evidence": [{"source": f"paper-{i}", "excerpt": f"text-{i}"} for i in range(20)]})
        self.store.complete(self.next("researcher2"), {"evidence": []})
        self.store.complete(self.next(), {"action": "report"})
        self.store.complete(self.next("report"), {"answer": "结论 [E1]"})
        self.store.complete(self.next("critic"), {"verdict": "revise", "issues": []})
        snapshot = json.loads(self.store.conn.execute("SELECT snapshot FROM research_reports").fetchone()[0])
        self.assertEqual(len(snapshot["evidence"]), 20)
        original = snapshot["evidence"][0]["stable_id"]
        self.store.control("research-test", "steer", message="新问题条件")
        self.assertEqual(self.store.get("research-test")["evidence"][0]["stable_id"], original)
        self.assertEqual(snapshot["question"], "比较两种方法")

    def test_pdf_page_anchors_and_cross_worker_unit_ids_are_preserved(self):
        from research_orchestrator import ResearchOrchestrator
        from research_evidence import attach_evidence_units
        messages = [{"role": "assistant", "tool_calls": [{"id": "pdf", "function": {
            "name": "read_pdf", "arguments": '{"url_or_path":"https://example.org/paper.pdf"}'}}]},
            {"role": "tool", "tool_call_id": "pdf", "content": "📄 PDF\n━━━ 第 1 页 ━━━\nfirst\n━━━ 第 2 页 ━━━\nsecond"}]
        cards = ResearchOrchestrator._evidence_from_messages(messages, "public", unlimited=True)
        self.assertEqual([card["source_anchor"]["page"] for card in cards], [1, 2])
        self.assertEqual(cards[0]["source"], "https://example.org/paper.pdf")
        self.dispatch()
        self.store.complete(self.next("researcher1"), {"evidence": [{"source": "other", "excerpt": "other"}]})
        self.store.complete(self.next("researcher2"), {"evidence": attach_evidence_units(cards)})
        evidence = self.store.get("research-test")["evidence"]
        self.assertEqual(evidence[2]["id"], "E3")
        self.assertEqual(evidence[2]["evidence_unit"]["evidence_id"], "E3")
        self.assertEqual(evidence[2]["source_version"], "unknown")

    def test_actual_model_usage_settles_reservation(self):
        job = self.next()
        client = Mock()
        response = Mock()
        response.json.return_value = {"usage": {"total_tokens": 42}, "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        client.post.return_value = response
        wrapped = BudgetedClient(client, self.store, job)
        wrapped.post({"messages": [{"role": "user", "content": "question"}]}, stream=False)
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 42)
        self.assertEqual(client.post.call_args.args[0]["max_tokens"], 8192)

    def test_query_pages_survive_tool_extraction_and_persistence(self):
        from tools.search import handle_query_papers
        from research_orchestrator import ResearchOrchestrator
        store = Mock()
        store.query_with_timeout.return_value = ([
            {"paper_id": "p", "title": "Paper", "page": 8, "page_end": 9, "element_id": "",
             "section": "未标注", "text": "FIFO prerequisite.", "retrieval": "keyword", "keyword_score": 2},
            {"paper_id": "p", "title": "Paper", "page": 10, "page_end": 10, "element_id": "",
             "section": "未标注", "text": "Counterexample.", "retrieval": "keyword", "keyword_score": 1},
        ], "嵌入模型仍在后台初始化，以下为本地关键词候选结果。")
        output = handle_query_papers({"query": "FIFO"}, paper_store=store)
        self.assertIn("p.8–9", output)
        cards = ResearchOrchestrator._evidence_from_messages([
            {"role": "assistant", "tool_calls": [{"id": "q", "function": {"name": "query_papers"}}]},
            {"role": "tool", "tool_call_id": "q", "content": output}], "local", unlimited=True)
        self.assertEqual([c["source_anchor"]["page"] for c in cards], [8, 10])
        self.assertEqual(cards[0]["source_anchor"]["page_end"], 9)
        self.assertIn("降级", cards[0]["uncertainty"])
        self.dispatch()
        self.store.complete(self.next("researcher1"), {"evidence": cards})
        self.assertEqual(len(self.store.get("research-test")["evidence"]), 2)
        for value in ["⏳ 正在初始化", "⏳ 初始化\n\n📭 未找到相关内容", "❌ 检索失败"]:
            self.assertFalse(ResearchOrchestrator._evidence_from_messages([
                {"role": "tool", "content": value}], "local", unlimited=True))

    def test_invalid_page_metadata_never_becomes_a_locator(self):
        from tools.search import handle_query_papers
        for page in [-1, 0, None, True, "8"]:
            store = Mock()
            store.query_with_timeout.return_value = ([{"title": "Paper", "page": page, "distance": 0.1,
                "text": "Page 99 occurs in source prose."}], "")
            self.assertNotIn("p.", handle_query_papers({"query": "q"}, paper_store=store))

    def test_timeout_retries_same_job_once_with_shared_budget(self):
        job = self.next()
        self.store.reserve(job, 100)
        self.store.complete(job, {"error": "LLMRequestTimeoutError"})
        self.assertEqual(self.store.get("research-test")["execution_state"], "active")
        self.store.close()
        self.store = ResearchProjects(self.path)
        retry = self.next()
        self.assertEqual(retry["job_id"], job["job_id"])
        self.assertEqual(retry["attempts"], 2)
        self.store.reserve(retry, 200)
        self.store.complete(job, {"action": "report"})  # Late first attempt cannot commit.
        self.store.complete(retry, {"error": "LLMRequestTimeoutError"})
        project = self.store.get("research-test")
        self.assertEqual(project["budget"]["calls"], 2)
        self.assertEqual(project["budget"]["tokens"], 300)
        self.assertEqual(project["delivery_status"], "incomplete")
        self.assertEqual(project["report_version"], 1)
        self.assertEqual(self.store.jobs("research-test")[0]["state"], "failed")
        self.assertFalse(self.store.outbox())

    def test_pause_invalidates_a_queued_retry_and_keeps_charge(self):
        job = self.next()
        self.store.reserve(job, 100)
        self.store.complete(job, {"error": "empty_response"})
        self.store.control("research-test", "pause")
        self.assertFalse(self.store.outbox())
        self.assertIsNone(self.store.claim(job["job_id"]))
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 100)

    def test_partial_evidence_survives_failed_worker_and_retry(self):
        self.dispatch()
        job = self.next("researcher1")
        evidence = [{"source": "Paper", "excerpt": "Condition", "source_anchor": {"kind": "page", "page": 8}}]
        self.store.complete(job, {"error": "empty_response", "evidence": evidence})
        self.assertEqual(len(self.store.get("research-test")["evidence"]), 1)
        self.store.complete(self.next("researcher1"), {"evidence": evidence})
        self.assertEqual(len(self.store.get("research-test")["evidence"]), 1)

    def test_empty_and_truncated_responses_are_charged_and_diagnosed(self):
        job = self.next()
        for finish, content, code in [("stop", "", "empty_response"), ("length", "partial", "output_truncated")]:
            client = Mock()
            response = client.post.return_value
            response.json.return_value = {"usage": {"total_tokens": 42},
                "choices": [{"finish_reason": finish, "message": {"content": content, "reasoning_content": "private"}}]}
            wrapped = BudgetedClient(client, self.store, job)
            with self.assertRaises(ResearchResponseError) as caught:
                wrapped.post({"messages": []}, stream=False)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(wrapped.responses[0]["finish_reason"], finish)
            self.assertNotIn("private", json.dumps(wrapped.responses))
            response.close.assert_called_once()
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 84)

    def test_unknown_usage_stays_reserved_and_distinguishable(self):
        reservation = self.store.reserve(self.next(), 123)
        for invalid in [None, -1, True, "42"]:
            self.store.settle(reservation, invalid)
        self.assertIsNone(self.store.conn.execute("SELECT actual FROM research_reservations").fetchone()[0])
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 123)
        self.store.settle(reservation, 42)
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 42)

    def test_report_truncation_preserves_draft_without_stale_approval(self):
        self.store.complete(self.next(), {"action": "report"})
        job = self.next("report")
        self.store.complete(job, {"error": "output_truncated", "partial_answer": "Incomplete [E1]"})
        self.assertFalse(self.store.get("research-test")["report"])
        self.assertEqual(self.store.get("research-test")["partial_report"]["version"], 1)
        self.store.complete(self.next("report"), {"answer": "Complete report"})
        self.assertIsNone(self.store.get("research-test")["partial_report"])
        self.assertEqual(self.store.get("research-test")["delivery_status"], "draft")
        self.assertFalse(self.store.get("research-test")["critique"])

    def test_report_has_reasoning_headroom_with_a_finite_request_budget(self):
        self.store.complete(self.next(), {"action": "report"})
        job = self.next("report")
        client = Mock()
        client.post.return_value.json.return_value = {"choices": [{"finish_reason": "stop",
            "message": {"content": "bounded report"}}], "usage": {"total_tokens": 64}}
        wrapped = BudgetedClient(client, self.store, job)
        wrapped.new_request_budget()
        policy = client.new_request_budget.call_args.args[0]
        self.assertEqual((policy.read_timeout_seconds, policy.deadline_seconds, policy.max_retries), (120, 150, 0))
        wrapped.post({"messages": []}, stream=False)
        self.assertEqual(client.post.call_args.args[0]["max_tokens"], 16384)
        self.assertEqual(self.store.get("research-test")["budget"]["tokens"], 64)

    def test_nontransient_auth_failure_is_not_retried(self):
        self.store.complete(self.next(), {"error": "LLMRequestFailedError", "error_details": {"http_status": 401}})
        self.assertFalse(self.store.outbox())

    def test_worker_checkpoint_evidence_is_returned_when_later_model_call_fails(self):
        from research_orchestrator import ResearchOrchestrator, _WorkerSpec
        graph = Mock()
        graph.invoke.side_effect = ResearchResponseError("empty_response")
        graph.get_state.return_value = SimpleNamespace(values={"messages": [
            {"role": "assistant", "tool_calls": [{"id": "q", "function": {"name": "query_papers"}}]},
            {"role": "tool", "tool_call_id": "q", "content": "📚 检索结果\n  1. [Paper · p.8 · 定理章节] 距离: 0.1\n     " + "condition " * 120}]})
        orchestrator = ResearchOrchestrator(session_store=self.sessions, api_key="test", model="test", llm_client=Mock(),
                                            paper_store=None, verify_timeout_seconds=3)
        with patch("research_orchestrator.build_graph", return_value=graph):
            result = orchestrator._run_worker(_WorkerSpec("local", "researcher1", {"query_papers"}, "check"),
                "question", {}, None, run_id="research-test", autonomous=True)
        self.assertEqual(result["error"], "empty_response")
        self.assertEqual(result["evidence"][0]["source_anchor"]["page"], 8)
        self.assertGreater(len(result["evidence"][0]["excerpt"]), 900)

    def test_prompt_selection_is_bounded_without_discarding_stored_evidence(self):
        project = self.store.get("research-test")
        project["evidence"] = [{"id": f"E{i}", "stable_id": f"ev-{i}", "source": "paper",
            "excerpt": "条件与反例" * 1000, "evidence_unit": {"observed_text": "duplicate" * 1000}} for i in range(1, 101)]
        project["report"] = "先前结论 [E1]"
        context = json.loads(AutonomousResearch.context(project))
        self.assertIn("E1", [card["id"] for card in context["evidence"]])
        self.assertLess(len(json.dumps(context["evidence"], ensure_ascii=False).encode()), 20200)
        self.assertEqual(len(project["evidence"]), 100)
        self.assertGreater(context["omitted_evidence_count"], 0)

    def test_runner_research_review_feedback_and_finish(self):
        agent = SimpleNamespace(sessions=self.sessions, api_key="test", model="test", _paper_store=None,
            llm_client=Mock(), cfg=SimpleNamespace(verify_timeout_seconds=3, vision_model="test"))
        runtime = AutonomousResearch(agent, self.store)
        responses = [
            {"action": "research", "tasks": [{"gap": "g", "question": "q", "expected": "e", "difference": "first"}]},
            {"action": "report"},
            "条件结论 [E1]<!--ref:E1--><!--anchor:none-->",
            {"verdict": "pass", "issues": []},
            {"action": "finish", "criteria_met": True},
        ]
        def model(**kwargs):
            value = responses.pop(0)
            return value if isinstance(value, str) else json.dumps(value)
        with patch("autonomous_research.ResearchOrchestrator._call_model", side_effect=model), patch(
            "autonomous_research.ResearchOrchestrator._run_worker",
            return_value={"evidence": [{"source": "paper", "excerpt": "text"}], "answer": "observed"}) as worker:
            for _ in range(6):
                queued = self.store.outbox()
                self.assertTrue(queued)
                runtime.execute(queued[0]["job_id"])
        self.assertEqual(self.store.get("research-test")["end_reason"], "fulfilled")
        self.assertEqual(worker.call_args.kwargs["max_tool_rounds"], 4)
        self.assertTrue(worker.call_args.kwargs["autonomous"])

    def test_http_controls_and_global_listing_survive_session_deletion(self):
        from api.app import create_app
        from api.redis_runs import RedisResearchRunManager
        from fastapi.testclient import TestClient
        session = self.sessions.create("研究入口")
        agent = SimpleNamespace(sessions=self.sessions, model="test", cfg=None)
        broker = Mock()
        manager = RedisResearchRunManager(agent, broker)
        app = create_app(agent=agent, research_run_manager=manager)
        try:
            with TestClient(app) as client:
                response = client.post("/api/v1/runs", json={"kind": "research", "session_id": session["thread_id"], "query": "持续问题"})
                self.assertEqual(response.status_code, 202, response.text)
                project_id = response.json()["project_id"]
                detail = client.get(f"/api/v1/runs/{project_id}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["research"]["goal_version"], 1)
                paused = client.post(f"/api/v1/research-projects/{project_id}/controls", json={"action": "pause", "event_id": "pause-once"})
                self.assertEqual(paused.json()["execution_state"], "paused")
                self.sessions.delete(session["thread_id"])
                listed = client.get("/api/v1/research-projects").json()
                self.assertIn(project_id, [item["project_id"] for item in listed])
                resumed = client.post(f"/api/v1/research-projects/{project_id}/controls", json={"action": "resume"})
                self.assertEqual(resumed.json()["execution_state"], "active")
        finally:
            app.state.badcase_store.close()
            manager.projects.close()

    def test_source_scope_is_enforced_before_pdf_tool_execution(self):
        from tool_runtime import ToolRuntime, ToolExecutionContext
        for scope, source in [("local", "https://example.org/a.pdf"), ("public", "private.pdf")]:
            executor = Mock()
            runtime = ToolRuntime(ToolExecutionContext(run_kind="research", research_scope=scope,
                allowed_tool_names=frozenset({"read_pdf"})), executor=executor)
            self.assertIn("来源超出", runtime.execute("read_pdf", {"url_or_path": source}))
            executor.assert_not_called()

    def test_lost_queue_ownership_cannot_commit_a_model_result(self):
        from api.run_dispatcher import RunOwnershipLostError
        agent = SimpleNamespace(sessions=self.sessions, api_key="test", model="test", _paper_store=None,
            llm_client=Mock(), cfg=SimpleNamespace(verify_timeout_seconds=3, vision_model="test"))
        check = Mock(side_effect=[None, RunOwnershipLostError("lost")])
        job = self.store.outbox()[0]
        with patch.object(AutonomousResearch, "_perform", return_value={"action": "wait", "waiting_for": ["paper:x"]}):
            with self.assertRaises(RunOwnershipLostError):
                AutonomousResearch(agent, self.store).execute(job["job_id"], check=check)
        self.assertEqual(self.store.get("research-test")["execution_state"], "active")
        self.assertEqual(self.store.jobs("research-test")[0]["state"], "running")

    def test_parallel_connections_cannot_overspend_call_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        self.store.create("research-budget", "session", "q", max_calls=4)
        job = self.store.claim(next(j for j in self.store.outbox() if j["project_id"] == "research-budget")["job_id"])
        def reserve(_):
            store = ResearchProjects(self.path)
            try:
                store.reserve(job, 50)
                return True
            except ResearchBudgetExceeded:
                return False
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.store.get("research-budget")["budget"]["calls"], 1)

    def test_critic_blocker_drives_targeted_followup_not_automatic_scope_expansion(self):
        self.dispatch()
        self.store.complete(self.next("researcher1"), {"evidence": [{"source": "abstract", "excerpt": "broad claim"}]})
        self.store.complete(self.next("researcher2"), {"evidence": []})
        self.store.complete(self.next(), {"action": "report"})
        self.store.complete(self.next("report"), {"answer": "尚需核对条件 [E1]"})
        self.store.complete(self.next("critic"), {"verdict": "revise", "issues": [
            {"kind": "blocking", "reason": "没有核对全文负载条件"},
            {"kind": "out_of_scope", "reason": "其他应用另行研究"}]})
        queued = [job for job in self.store.outbox() if job["project_id"] == "research-test"]
        self.assertEqual([job["role"] for job in queued], ["lead"])
        self.store.complete(self.next(), {"action": "research", "tasks": [
            {"gap": "条件", "question": "读取全文负载条件", "expected": "验证摘要的适用范围", "difference": "从摘要转向全文"}]})
        task = self.next("researcher1")
        self.assertEqual(task["payload"]["gap"], "条件")
        self.store.complete(task, {"evidence": [{"source": "full paper", "excerpt": "only under light load"}]})
        self.assertEqual(len(self.store.get("research-test")["evidence"]), 2)

    def test_unresolved_dependencies_do_not_execute_in_parallel(self):
        self.store.complete(self.next(), {"action": "research", "tasks": [
            {"gap": "a", "question": "先取原文", "expected": "全文", "difference": "首次"},
            {"gap": "b", "question": "基于原文比较", "expected": "比较", "difference": "首次", "depends_on": ["not-yet-finished"]}]})
        self.assertEqual([job["role"] for job in self.store.outbox()], ["researcher1"])

    @unittest.skipUnless(os.getenv("E2E_REDIS_URL"), "requires isolated Redis")
    def test_real_queue_releases_lead_and_runs_two_workers_concurrently(self):
        self.store.control("research-test", "stop")
        from api.redis_runs import RedisResearchRunBroker, RedisResearchRunManager, RedisResearchRunWorker
        from api.run_dispatcher import RunDispatcher
        import uuid
        broker = RedisResearchRunBroker(os.environ["E2E_REDIS_URL"], prefix="test-persistent-" + uuid.uuid4().hex)
        session = self.sessions.create("并行研究")
        agent = SimpleNamespace(sessions=self.sessions, api_key="test", model="test", _paper_store=Mock(),
            llm_client=Mock(), cfg=SimpleNamespace(verify_timeout_seconds=3, vision_model="test"))
        manager = RedisResearchRunManager(agent, broker)
        worker = RedisResearchRunWorker(agent, broker, consumer="test-persistent", claim_idle_ms=1000)
        dispatcher = RunDispatcher({"research": (worker, 2)}, poll_seconds=0.01)
        stop = threading.Event()
        barrier = threading.Barrier(2, timeout=5)
        overlaps = []
        calls = []
        def model(**kwargs):
            calls.append(kwargs["stage"])
            if kwargs["stage"] == "lead":
                if len(calls) == 1:
                    from llm_client import LLMRequestTimeoutError
                    raise LLMRequestTimeoutError("simulated first request timeout")
                if len(calls) == 2:
                    return json.dumps({"action": "research", "tasks": [
                        {"gap": f"g{i}", "question": f"q{i}", "expected": "e", "difference": "first"} for i in range(2)]})
                if "critic" not in calls:
                    return '{"action":"report"}'
                return '{"action":"finish","criteria_met":true}'
            if kwargs["stage"] == "critic":
                return '{"verdict":"pass","issues":[]}'
            return "条件结论 [E1]<!--ref:E1--><!--anchor:none-->"
        def research(*args, **kwargs):
            barrier.wait()
            overlaps.append(True)
            return {"evidence": [{"source": "paper", "excerpt": "text"}], "answer": "local finding"}
        thread = threading.Thread(target=dispatcher.run_forever, args=(stop,))
        try:
            with patch("autonomous_research.ResearchOrchestrator._call_model", side_effect=model), patch(
                    "autonomous_research.ResearchOrchestrator._run_worker", side_effect=research):
                run = manager.start(session["thread_id"], "q")
                thread.start()
                until = time.monotonic() + 12
                while time.monotonic() < until and manager.get(run["run_id"])["status"] == "running":
                    time.sleep(0.05)
                self.assertEqual(manager.get(run["run_id"])["status"], "completed", manager.projects.get(run["run_id"])["decisions"])
                self.assertEqual(len(overlaps), 2)
                self.assertEqual(manager.projects.jobs(run["run_id"])[0]["attempts"], 2)
                self.assertIsNone(self.sessions.first_pending_run(session["thread_id"]))
        finally:
            stop.set()
            if thread.is_alive():
                thread.join(10)
            worker.project_manager.projects.close()
            manager.projects.close()
            keys = list(broker._redis.scan_iter(match=broker.prefix + ":*"))
            if keys:
                broker._redis.delete(*keys)


if __name__ == "__main__":
    unittest.main()

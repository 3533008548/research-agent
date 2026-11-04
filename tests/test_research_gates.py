"""Tests for the research orchestrator's quality gates.

Three defects are covered here, and all three share a shape: a mechanism
existed but was not actually connected to anything, so the failure was
invisible.

1. The planner's subtask ids never had to match the researcher role names,
   so a plan could be discarded while the trace still showed a tidy
   decomposition.
2. The citation audit computed the right answer and nobody acted on it, so a
   report citing evidence which does not exist still completed cleanly.
3. The critic read a head-only slice of the draft, and the synthesis stage
   puts the limitations last — so the caveats were the least reviewed part.
"""

from __future__ import annotations

import json
import unittest

from research_orchestrator import ResearchOrchestrator


def _orchestrator(**kwargs) -> ResearchOrchestrator:
    defaults = {
        "session_store": None,
        "api_key": "test-key",
        "model": "deepseek-chat",
        "paper_store": None,
        "llm_client": None,
        "verify_timeout_seconds": 3,
    }
    defaults.update(kwargs)
    return ResearchOrchestrator(**defaults)


class TestPlannerSubtasksReachTheResearchers(unittest.TestCase):
    """The plan must survive the trip from the planner to the worker."""

    def test_a_plan_with_no_role_ids_still_reaches_a_researcher(self):
        plan = _orchestrator()._parse_plan(
            '{"objective":"x","subtasks":[{"focus":"检索 TSN 调度方案"}]}',
            "q", "both",
        )
        self.assertTrue(plan["subtasks"])
        for subtask in plan["subtasks"]:
            self.assertIn(
                subtask["id"], ("local", "public", "boundary"),
                "子任务 id 必须落到某个研究员角色上",
            )

    def test_invented_task_ids_are_mapped_onto_the_roles(self):
        plan = _orchestrator()._parse_plan(
            json.dumps({
                "objective": "x",
                "subtasks": [
                    {"id": "task-1", "focus": "本地库"},
                    {"id": "task-2", "focus": "公开文献"},
                    {"id": "task-3", "focus": "找反例"},
                ],
            }),
            "q", "both",
        )
        self.assertEqual(
            [(item["id"], item["focus"]) for item in plan["subtasks"]],
            [("local", "本地库"), ("public", "公开文献"), ("boundary", "找反例")],
        )

    def test_ids_that_already_name_a_role_are_kept(self):
        plan = _orchestrator()._parse_plan(
            '{"objective":"x","subtasks":[{"id":"boundary","focus":"找反例"},'
            '{"id":"local","focus":"本地库"}]}',
            "q", "both",
        )
        by_id = {item["id"]: item["focus"] for item in plan["subtasks"]}
        self.assertEqual(by_id["boundary"], "找反例")
        self.assertEqual(by_id["local"], "本地库")

    def test_normalisation_follows_the_scope(self):
        plan = _orchestrator()._parse_plan(
            '{"objective":"x","subtasks":[{"id":"task-1","focus":"A"},'
            '{"id":"task-2","focus":"B"},{"id":"task-3","focus":"C"}]}',
            "q", "public",
        )
        self.assertEqual([item["id"] for item in plan["subtasks"]], ["public", "boundary"])

    def test_the_worker_actually_receives_the_planned_focus(self):
        from research_orchestrator import _WorkerSpec

        plan = _orchestrator()._parse_plan(
            '{"objective":"x","subtasks":[{"id":"task-1","focus":"检索 TSN 调度方案"}]}',
            "q", "both",
        )
        spec = _WorkerSpec(
            role="local", label="本地证据研究员",
            allowed_tools={"query_papers"}, instruction="通用指令",
        )
        worker = ResearchOrchestrator._worker_input("q", plan, spec)
        self.assertIn("检索 TSN 调度方案", worker)
        self.assertNotIn("Your focus: 通用指令", worker)

    def test_a_plan_that_covers_nothing_is_reported_as_not_applied(self):
        plan = _orchestrator()._parse_plan(
            '{"objective":"x","subtasks":[{"id":"local","focus":"只有本地"}]}',
            "q", "both",
        )
        # Only one of three roles received a focus, so the fact is recorded
        # rather than left to be inferred from the plan.
        self.assertFalse(plan["focus_applied"])

    def test_the_planner_prompt_names_the_roles(self):
        prompt = ResearchOrchestrator._planner_input("q", None, "both")
        for role in ("local", "public", "boundary"):
            self.assertIn(role, prompt)
        self.assertNotIn("local", ResearchOrchestrator._planner_input("q", None, "public"))


class TestCitationAuditGatesTheRun(unittest.TestCase):
    """An unverifiable citation is a failure, not a note at the bottom."""

    def test_unknown_refs_become_revision_instructions(self):
        issues = ResearchOrchestrator._citation_issues({
            "unknown_refs": ["E9"],
            "anchor_upgraded_refs": [],
        })
        self.assertEqual(len(issues), 1)
        self.assertIn("E9", issues[0])

    def test_an_upgraded_anchor_becomes_a_revision_instruction(self):
        issues = ResearchOrchestrator._citation_issues({
            "unknown_refs": [],
            "anchor_upgraded_refs": ["E1"],
        })
        self.assertEqual(len(issues), 1)
        self.assertIn("E1", issues[0])

    def test_a_clean_audit_produces_no_instructions(self):
        self.assertEqual(ResearchOrchestrator._citation_issues({
            "unknown_refs": [], "anchor_upgraded_refs": [],
        }), [])

    def test_a_passing_critique_is_overridden_by_a_broken_citation(self):
        merged = ResearchOrchestrator._merge_citation_gate(
            {"verdict": "pass", "issues": []},
            {"unknown_refs": ["E9"], "anchor_upgraded_refs": []},
        )
        self.assertEqual(merged["verdict"], "revise")
        self.assertTrue(any("E9" in issue for issue in merged["issues"]))

    def test_a_clean_audit_leaves_the_critique_alone(self):
        critique = {"verdict": "pass", "issues": ["保留原判"]}
        merged = ResearchOrchestrator._merge_citation_gate(
            critique, {"unknown_refs": [], "anchor_upgraded_refs": []},
        )
        self.assertEqual(merged, critique)

    def test_the_gate_survives_a_critic_that_never_ran(self):
        """A skipped critic must not turn a broken citation into a pass."""
        skipped = {"verdict": "pass", "issues": ["自动质检已跳过：Timeout。"]}
        merged = ResearchOrchestrator._merge_citation_gate(
            skipped, {"unknown_refs": ["E9"], "anchor_upgraded_refs": []},
        )
        self.assertEqual(merged["verdict"], "revise")


class TestDraftExcerptKeepsTheLimitations(unittest.TestCase):
    """The critic must see the caveats, which sit at the end of the draft."""

    @staticmethod
    def _draft(scale: int) -> str:
        return (
            "# 报告\n\n## 结论\n" + "结论内容。" * scale
            + "\n\n## 适用条件\n" + "条件内容。" * scale
            + "\n\n## 限制\n" + "限制内容。" * scale
        )

    def test_a_short_draft_is_passed_through(self):
        draft = "短草稿"
        self.assertEqual(ResearchOrchestrator._draft_excerpt(draft), draft)

    def test_the_excerpt_keeps_both_ends(self):
        draft = self._draft(500)
        excerpt = ResearchOrchestrator._draft_excerpt(draft)
        self.assertIn("## 结论", excerpt)
        self.assertNotIn("## 限制\n\n限制内容。限制内容。限制内容。", excerpt)
        # The tail carries the limitations, and says which section it is.
        self.assertIn("限制内容。", excerpt)
        self.assertIn("尾段包含章节", excerpt)
        self.assertIn("限制", excerpt.split("尾段包含章节")[-1][:60])

    def test_the_excerpt_stays_within_its_budget(self):
        excerpt = ResearchOrchestrator._draft_excerpt(self._draft(2000))
        self.assertLessEqual(len(excerpt), 5000 + 120)

    def test_the_old_head_only_slice_lost_the_limitations(self):
        """Guard the premise: this is the behaviour being fixed."""
        draft = self._draft(500)
        self.assertNotIn("## 限制", draft[:5000])
        self.assertIn("## 限制", ResearchOrchestrator._draft_excerpt(draft))


class TestCitationGateEndToEnd(unittest.TestCase):
    """The gate must change the run's status, not just its text."""

    @staticmethod
    def _worker_result():
        return {
            "role": "local",
            "label": "本地证据研究员",
            "status": "completed",
            "message": "获得 1 条证据",
            "answer": "工具检索完成",
            "evidence": [{
                "id": "",
                "researcher": "local",
                "source_type": "query_papers",
                "source": "TSN Paper",
                "excerpt": "TSN 论文中的可核验证据。",
                "uncertainty": "需要结合原文核验。",
                "source_anchor": {"kind": "page", "page": 3},
            }],
            "usage": {"prompt": 2, "completion": 3, "total": 5, "calls": 1},
            "trace": {"role": "local"},
        }

    def _run(
        self, synthesis: str, revision: str | None = None, *,
        critic: str | Exception = '{"verdict":"pass","issues":[]}',
        worker_batches: list | None = None, scope: str = "both",
    ) -> tuple:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                orchestrator = _orchestrator(session_store=store, llm_client=object())

                def fake_model(*, stage, **_kwargs):
                    if stage == "planner":
                        return '{"objective":"TSN","subtasks":[{"id":"local","focus":"本地"}]}'
                    if stage == "synthesis":
                        return synthesis
                    if stage == "critic":
                        if isinstance(critic, Exception):
                            raise critic
                        return critic
                    if stage == "revision":
                        return revision if revision is not None else synthesis
                    raise AssertionError(stage)

                batches = worker_batches if worker_batches is not None else [[self._worker_result()]]
                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", side_effect=batches) as workers:
                    result = orchestrator.run(
                        "TSN 调度方法比较",
                        thread_id=session["thread_id"],
                        scope=scope,
                    )
                self.worker_calls = workers.call_args_list
                saved = store.get_research_run(result.run_id)
                return result, saved
            finally:
                store.close()

    def test_a_citation_to_missing_evidence_downgrades_the_run(self):
        result, saved = self._run(
            "结论引用了并不存在的证据 [E9]<!--ref:E9--><!--anchor:none-->",
        )
        self.assertEqual(result.status, "partial_failed")
        self.assertEqual(saved["status"], "partial_failed")
        self.assertIn("E9", saved["trace"]["citation_gate"]["unresolved_refs"])
        # The report still ships — the evidence was gathered and is worth
        # keeping — but the status no longer claims a clean completion.
        self.assertIn("结论引用了并不存在的证据", result.answer)

    def test_omitting_hidden_markers_does_not_hide_an_invented_citation(self):
        result, saved = self._run("结论只写可见引用 [E9]")
        self.assertEqual(result.status, "partial_failed")
        self.assertEqual(saved["trace"]["citation_audit"]["unknown_refs"], ["E9"])

    def test_a_broken_citation_triggers_a_revision(self):
        result, saved = self._run(
            "初稿引用了并不存在的证据 [E9]<!--ref:E9--><!--anchor:none-->",
            revision="修订后只引用真实证据 [E1]<!--ref:E1--><!--anchor:page:3-->",
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(saved["status"], "completed")

    def test_a_clean_run_still_completes(self):
        result, saved = self._run(
            "结论引用真实证据 [E1]<!--ref:E1--><!--anchor:page:3-->",
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(saved["status"], "completed")

    def test_a_critic_timeout_preserves_the_report_without_passing_it(self):
        from llm_client import LLMRequestTimeoutError

        result, saved = self._run(
            "结论 [E1]<!--ref:E1--><!--anchor:page:3-->",
            critic=LLMRequestTimeoutError("timeout"),
        )
        self.assertEqual(result.status, "partial_failed")
        self.assertEqual(saved["critique"]["verdict"], "unknown")
        self.assertEqual(len(saved["evidence"]), 1)
        self.assertIn("结论", result.answer)
        self.assertIn("未核验", result.answer)
        self.assertEqual(saved["trace"]["quality_gate"]["critic_verdict"], "unknown")

    def test_a_malformed_critic_is_not_a_pass(self):
        for critic in ("not json", "{}", '{"verdict":"maybe"}',
                       '{"verdict":"pass","issues":"invalid"}'):
            with self.subTest(critic=critic):
                result, saved = self._run("结论 [E1]", critic=critic)
                self.assertEqual(result.status, "partial_failed")
                self.assertEqual(saved["critique"]["verdict"], "unknown")

    def test_an_unfixed_anchor_upgrade_downgrades_the_run(self):
        worker = self._worker_result()
        worker["evidence"][0]["source_anchor"] = {"kind": "none"}
        result, saved = self._run(
            "只有检索线索却声称页码 [E1]<!--ref:E1--><!--anchor:page:3-->",
            worker_batches=[[worker]],
        )
        self.assertEqual(result.status, "partial_failed")
        self.assertEqual(saved["trace"]["citation_gate"]["anchor_upgraded_refs"], ["E1"])
        self.assertIn("定位", result.answer)

    def test_downgrading_an_unresolved_anchor_is_a_valid_revision(self):
        worker = self._worker_result()
        worker["evidence"][0]["source_anchor"] = {"kind": "none"}
        result, _ = self._run(
            "初稿 [E1]<!--ref:E1--><!--anchor:page:3-->",
            revision="仅为检索线索 [E1]<!--ref:E1--><!--anchor:none-->",
            worker_batches=[[worker]],
        )
        self.assertEqual(result.status, "completed")

    def test_citation_revision_does_not_erase_an_unknown_critic(self):
        result, saved = self._run(
            "初稿 [E9]<!--ref:E9--><!--anchor:none-->",
            revision="修订 [E1]<!--ref:E1--><!--anchor:page:3-->",
            critic=RuntimeError("critic unavailable"),
        )
        self.assertEqual(result.status, "partial_failed")
        self.assertEqual(saved["critique"]["critic_verdict"], "unknown")
        self.assertEqual(saved["trace"]["citation_audit"]["unknown_refs"], [])
        self.assertIn("未核验", result.answer)

    def test_only_empty_researchers_get_one_refinement_with_a_reason(self):
        empty = {
            **self._worker_result(), "evidence": [], "answer": "MODEL PROSE IS NOT EVIDENCE",
            "retrieval_queries": ["previous unsuccessful query"],
        }
        result, saved = self._run(
            "结论 [E1]", scope="local",
            worker_batches=[[empty], [self._worker_result()]],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.worker_calls), 2)
        retry = self.worker_calls[1]
        self.assertEqual([spec.role for spec in retry.args[0]], ["local"])
        self.assertEqual(retry.args[1], "TSN 调度方法比较")
        self.assertIn("Refinement reason", retry.kwargs["steer_context"])
        self.assertIn("previous unsuccessful query", retry.kwargs["steer_context"])
        self.assertNotIn("MODEL PROSE", retry.kwargs["steer_context"])
        self.assertEqual(saved["trace"]["refinements"][0]["reason"], "no_usable_evidence")
        self.assertNotIn("previous unsuccessful query", json.dumps(saved["trace"]))

    def test_refinement_failure_is_bounded_and_gives_a_diagnosis(self):
        empty = {**self._worker_result(), "evidence": []}
        result, saved = self._run("unused", scope="local", worker_batches=[[empty], [empty]])
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(self.worker_calls), 2)
        self.assertEqual(saved["evidence"], [])
        self.assertIn("改写", result.answer)
        self.assertIn("来源范围", result.answer)

    def test_refinement_does_not_repeat_a_model_failure(self):
        failed = {**self._worker_result(), "status": "failed", "evidence": []}
        result, _ = self._run("unused", scope="local", worker_batches=[[failed]])
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(self.worker_calls), 1)

    def test_tool_outages_are_not_misreported_as_refinable_empty_results(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        for content in (
            "❌ 所有已选来源均未完成：OpenAlex timeout；arXiv timeout",
            "❌ 外部工具 mcp__test__search 超时（30s）。",
            "⏳ 本地索引正在初始化。",
            "📭 未检索到匹配论文。",
        ):
            with self.subTest(content=content):
                app = SimpleNamespace(
                    checkpointer=SimpleNamespace(conn=SimpleNamespace(close=lambda: None)),
                    invoke=lambda *_args, **_kwargs: {"messages": [
                        {"role": "assistant", "tool_calls": [{
                            "id": "call-1", "function": {
                                "name": "search_papers", "arguments": '{"query":"q"}',
                            },
                        }]},
                        {"role": "tool", "tool_call_id": "call-1", "content": content},
                    ]},
                )
                orchestrator = _orchestrator()
                with patch("research_orchestrator.build_graph", return_value=app):
                    worker = orchestrator._run_worker(
                        orchestrator._worker_specs("public")[0], "q", {}, None,
                    )
                self.assertEqual(worker["evidence"], [])
                self.assertEqual(worker["retrieval_failed"], not content.startswith("📭"))
                self.assertEqual(worker["trace"]["retrieval_status"], "empty" if content.startswith("📭") else "failed")
                batches = [[worker]] if worker["retrieval_failed"] else [[worker], [worker]]
                result, _ = self._run("unused", scope="public", worker_batches=batches)
                self.assertEqual(result.status, "failed")
                self.assertEqual(len(self.worker_calls), len(batches))

    def test_nonempty_researchers_are_not_retried(self):
        empty = {**self._worker_result(), "role": "public", "evidence": []}
        retry_worker = {**self._worker_result(), "role": "public"}
        result, saved = self._run(
            "结论 [E1]", worker_batches=[[self._worker_result(), empty], [retry_worker]],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual([spec.role for spec in self.worker_calls[1].args[0]], ["public"])
        self.assertTrue(saved["evidence"])

    def test_unused_evidence_is_visible_without_failing_the_run(self):
        worker = self._worker_result()
        worker["evidence"].append({**worker["evidence"][0], "source": "Other Paper", "excerpt": "其他证据"})
        result, saved = self._run(
            "结论 [E1]<!--ref:E1--><!--anchor:page:3-->", worker_batches=[[worker]],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(saved["trace"]["citation_audit"]["evidence_without_marker"], ["E2"])
        self.assertIn("未引用证据 1 条：E2", result.answer)

    def test_unused_evidence_is_visible_even_without_machine_markers(self):
        result, _ = self._run("结论 [E1]")
        self.assertIn("未引用证据 1 条：E1", result.answer)

    def test_the_persisted_summary_does_not_contradict_the_status(self):
        """A partial failure must not be logged as "completed"."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "checkpoint.db"))
            try:
                session = store.create()
                orchestrator = _orchestrator(session_store=store, llm_client=object())

                def fake_model(*, stage, **_kwargs):
                    return {
                        "planner": '{"objective":"x","subtasks":[{"id":"local","focus":"本地"}]}',
                        "synthesis": "结论 [E9]<!--ref:E9--><!--anchor:none-->",
                        "critic": '{"verdict":"pass","issues":[]}',
                        # The broken citation forces a revision; the revision
                        # keeps the bad marker so the gate still bites.
                        "revision": "修订后仍引用 [E9]<!--ref:E9--><!--anchor:none-->",
                    }[stage]

                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", return_value=[self._worker_result()]):
                    result = orchestrator.run("q", thread_id=session["thread_id"])

                self.assertEqual(result.status, "partial_failed")
                events = store.get_run_events(result.run_id, session["thread_id"])
                final = [event for event in events if event["status"] == "partial_failed"]
                self.assertTrue(final, "应当落库一条 partial_failed 事件")
                for event in final:
                    self.assertNotIn("已完成", event["summary"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

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
            }],
            "usage": {"prompt": 2, "completion": 3, "total": 5, "calls": 1},
            "trace": {"role": "local"},
        }

    def _run(self, synthesis: str, revision: str | None = None) -> tuple:
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
                        return '{"verdict":"pass","issues":[]}'
                    if stage == "revision":
                        return revision if revision is not None else synthesis
                    raise AssertionError(stage)

                with patch.object(orchestrator, "_call_model", side_effect=fake_model), \
                     patch.object(orchestrator, "_run_workers", return_value=[self._worker_result()]):
                    result = orchestrator.run(
                        "TSN 调度方法比较",
                        thread_id=session["thread_id"],
                    )
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

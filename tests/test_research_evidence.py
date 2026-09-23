"""Focused tests for source-bounded evidence and boundary analysis."""

from __future__ import annotations

import unittest

from research_evidence import (
    ANCHOR_KINDS,
    analysis_metrics,
    attach_evidence_units,
    audit_citations,
    build_evidence_analysis,
    extract_citation_markers,
    format_boundary_ledger,
    format_evidence_matrix,
    split_tool_evidence,
    strip_citation_markers,
)


class TestResearchEvidence(unittest.TestCase):
    def test_public_search_is_split_per_paper_with_its_own_anchor(self):
        content = """📚 **多源学术搜索结果** — 查询: 「TSN scheduling」
📊 检索质量：保留 2/5 条 | 相关性评分: 0.80-0.95 | 已拒绝 3 条（低相关）

  1. **Direct Evidence**  [OpenAlex]
     年份: 2024  | 引用: 2  | 期刊: Journal A
     相关性: 0.95 | 依据: 命中调度条件
     链接: https://example.test/direct
     摘要: We propose a scheduling method under bursty traffic.

  2. **Boundary Evidence**  [arXiv]
     年份: 2023  | 引用: 1  | 期刊: Journal B
     相关性: 0.80 | 依据: 命中流量条件
     链接: https://example.test/boundary
     摘要: However, the approach is limited under high load.
"""
        cards = split_tool_evidence(content, researcher="public", source_type="search_papers")

        self.assertEqual([card["source"] for card in cards], ["Direct Evidence", "Boundary Evidence"])
        self.assertEqual(cards[0]["source_anchor"]["url"], "https://example.test/direct")
        self.assertEqual(cards[1]["source_anchor"]["url"], "https://example.test/boundary")
        self.assertNotIn("Boundary Evidence", cards[0]["excerpt"])
        self.assertEqual(cards[1]["relevance"]["score_min"], 0.8)

    def test_matrix_and_boundary_ledger_only_report_source_clues(self):
        evidence = attach_evidence_units([
            {
                "id": "E1",
                "researcher": "public",
                "source_type": "search_papers",
                "source": "Method Paper",
                "excerpt": "We propose a method and evaluate its latency under bursty traffic.",
                "source_anchor": {"kind": "public_paper_metadata", "url": "https://example.test/a"},
                "uncertainty": "metadata only",
            },
            {
                "id": "E2",
                "researcher": "boundary",
                "source_type": "search_papers",
                "source": "Boundary Paper",
                "excerpt": "However, the method is limited under high load and may fail in this setting.",
                "source_anchor": {"kind": "public_paper_metadata", "url": "https://example.test/b"},
                "uncertainty": "metadata only",
            },
        ])

        analysis = build_evidence_analysis(evidence)
        matrix = format_evidence_matrix(analysis)
        ledger = format_boundary_ledger(analysis)

        self.assertIn("[E1]", matrix)
        self.assertIn("[E2]", matrix)
        self.assertIn("[E2]", ledger)
        self.assertIn("limited under high load", ledger)
        self.assertEqual(analysis_metrics(analysis)["boundary_signal_units"], 1)
        self.assertNotIn("universally", matrix + ledger)

    def test_unknown_tool_format_remains_a_single_fallback_card(self):
        self.assertEqual(
            split_tool_evidence("unstructured tool response", researcher="local", source_type="query_papers"),
            [],
        )


class TestSourceAnchorPrecision(unittest.TestCase):
    """P0-2: anchors resolve to a closed enum instead of a generic tool_result."""

    LOCAL_CONTENT = """📚 语义检索结果 — 「调度」

  1. [Sample Paper · p.3–4 · Table 2 · 方法章节]  距离: 0.31
     We propose a scheduling method under bursty traffic.

  2. [Other Paper · p.7 · S012 · 实验章节]  距离: 0.42
     However the approach is limited under high load.
"""

    def test_local_query_resolves_page_anchor(self):
        cards = split_tool_evidence(
            self.LOCAL_CONTENT, researcher="local", source_type="query_papers",
        )
        self.assertEqual(len(cards), 2)
        anchor = cards[0]["source_anchor"]
        self.assertEqual(anchor["kind"], "page")
        self.assertEqual(anchor["page"], 3)
        self.assertEqual(anchor["page_end"], 4)
        self.assertEqual(anchor["section"], "方法")
        self.assertEqual(anchor["element"], "Table 2")
        self.assertIn(anchor["kind"], ANCHOR_KINDS)

    def test_public_metadata_is_not_upgraded_to_a_page_anchor(self):
        cards = split_tool_evidence(
            "  1. **Direct Evidence**  [OpenAlex]\n     链接: https://example.test/a\n     摘要: x",
            researcher="public", source_type="search_papers",
        )
        self.assertEqual(cards[0]["source_anchor"]["kind"], "none")
        self.assertEqual(cards[0]["source_anchor"]["scope"], "public_paper_metadata")

    def test_unresolved_evidence_is_none_not_a_fake_locator(self):
        enriched = attach_evidence_units([{
            "id": "E1", "researcher": "local", "source_type": "tool",
            "source": "unknown", "excerpt": "some text", "uncertainty": "n/a",
        }])
        anchor = enriched[0]["evidence_unit"]["source_anchor"]
        self.assertEqual(anchor["kind"], "none")
        self.assertIn("不得作为精确引用来源", anchor["anchor_note"])

    def test_matrix_and_metrics_expose_anchor_precision(self):
        cards = split_tool_evidence(
            self.LOCAL_CONTENT, researcher="local", source_type="query_papers",
        )
        evidence = attach_evidence_units([
            {**card, "id": f"E{index}"} for index, card in enumerate(cards, 1)
        ])
        analysis = build_evidence_analysis(evidence)
        matrix = format_evidence_matrix(analysis)
        self.assertIn("定位精度", matrix)
        self.assertIn("精确", matrix)
        metrics = analysis_metrics(analysis)
        self.assertEqual(metrics["precise_anchor_units"], 2)
        self.assertEqual(metrics["weak_anchor_units"], 0)


class TestCitationEmission(unittest.TestCase):
    """P0-3: hidden ref/anchor layer is emitted, parsed and auditable."""

    def test_markers_are_parsed_and_stripped(self):
        report = "时延下降 [E1] <!--ref:E1--><!--anchor:page:7-->；对比 [E2] <!--ref:E2--><!--anchor:none-->"
        markers = extract_citation_markers(report)
        self.assertEqual([m["evidence_id"] for m in markers], ["E1", "E2"])
        self.assertEqual(markers[0]["anchor_kind"], "page")
        self.assertEqual(markers[0]["anchor_value"], "7")
        self.assertEqual(markers[1]["anchor_kind"], "none")
        self.assertEqual(strip_citation_markers(report), "时延下降 [E1] ；对比 [E2] ")

    def test_audit_flags_unknown_refs_and_anchor_upgrades(self):
        evidence = attach_evidence_units([{
            "id": "E1", "researcher": "public", "source_type": "search_papers",
            "source": "S", "excerpt": "text", "uncertainty": "metadata only",
            "source_anchor": {"kind": "none", "scope": "public_paper_metadata", "url": "u"},
        }])
        report = "A [E1] <!--ref:E1--><!--anchor:page:3-->；B [E9] <!--ref:E9-->"
        audit = audit_citations(report, evidence)
        self.assertEqual(audit["unknown_refs"], ["E9"])
        # 证据本身没有页内位置，却声称 page 锚点 —— 属于定位升级。
        self.assertEqual(audit["anchor_upgraded_refs"], ["E1"])


class TestResearchBoundaryPlanning(unittest.TestCase):
    def test_public_scope_has_a_dedicated_boundary_worker(self):
        from research_orchestrator import ResearchOrchestrator

        specs = ResearchOrchestrator._worker_specs("public")
        self.assertEqual([spec.role for spec in specs], ["public", "boundary"])
        self.assertIn("counterevidence", specs[1].instruction)
        plan = ResearchOrchestrator._parse_plan(
            object.__new__(ResearchOrchestrator),
            '{"objective":"x","subtasks":[{"id":"public","focus":"direct"}],"completion_criteria":["cited"]}',
            "x",
        )
        self.assertIn("主动核查反例、负面结果或适用边界", plan["completion_criteria"])

    def test_renumber_keeps_boundary_evidence_inside_prompt_cap(self):
        from research_orchestrator import ResearchOrchestrator

        cards = [
            {
                "researcher": role,
                "source": f"{role}-{index}",
                "excerpt": f"excerpt-{role}-{index}",
            }
            for role in ("local", "public", "boundary")
            for index in range(6)
        ]
        numbered = ResearchOrchestrator._renumber_evidence(cards)
        self.assertEqual(len(numbered), 12)
        self.assertTrue(any(card["researcher"] == "boundary" for card in numbered))


class TestPhaseBoundaryAndCitationRules(unittest.TestCase):
    """P0-1 / P0-4: every stage declares its boundary; citations need an action."""

    @staticmethod
    def _orchestrator():
        from research_orchestrator import ResearchOrchestrator

        return object.__new__(ResearchOrchestrator)

    def test_every_stage_input_carries_its_own_boundary(self):
        from research_orchestrator import PHASE_BOUNDARY, ResearchOrchestrator

        spec = ResearchOrchestrator._worker_specs("local")[0]
        worker = ResearchOrchestrator._worker_input("q", {"subtasks": []}, spec)
        self.assertIn(PHASE_BOUNDARY["researcher"], worker)

        evidence = [{
            "id": "E1", "source": "s", "source_type": "query_papers",
            "excerpt": "x", "uncertainty": "n/a",
        }]
        synthesis = self._orchestrator()._synthesis_input("q", {}, evidence, {})
        self.assertIn(PHASE_BOUNDARY["synthesis"], synthesis)
        self.assertIn(PHASE_BOUNDARY["critic"], self._orchestrator()._critic_input("q", "d", evidence, {}))
        self.assertIn(
            PHASE_BOUNDARY["revision"],
            self._orchestrator()._revision_input("q", "d", {}, evidence, {}),
        )

    def test_synthesis_carries_the_citation_existence_iron_rule(self):
        from research_orchestrator import CITATION_EMISSION_RULE, CITATION_EXISTENCE_RULE

        evidence = [{
            "id": "E1", "source": "s", "source_type": "query_papers",
            "excerpt": "x", "uncertainty": "n/a",
        }]
        prompt = self._orchestrator()._synthesis_input("q", {}, evidence, {})
        self.assertIn(CITATION_EXISTENCE_RULE, prompt)
        self.assertIn(CITATION_EMISSION_RULE, prompt)
        self.assertIn("counts as FAIL", prompt)

    def test_render_answer_strips_the_hidden_layer_and_reports_audit(self):
        from research_orchestrator import ResearchOrchestrator

        evidence = [{"id": "E1", "source": "s", "researcher": "local", "source_type": "query_papers"}]
        report = "时延下降 [E1] <!--ref:E1--><!--anchor:page:7-->"
        answer = ResearchOrchestrator._render_answer(
            report, evidence, {"issues": []}, "run-1", {},
            audit_citations(report, evidence),
        )
        self.assertNotIn("<!--ref:", answer)
        self.assertIn("引用溯源", answer)


if __name__ == "__main__":
    unittest.main()

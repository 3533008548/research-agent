"""A stated paper scope must never widen into a cross-paper search."""

from __future__ import annotations

import unittest

from tools.search import _resolve_scope


class _Store:
    def __init__(self, papers: list[dict]) -> None:
        self._papers = papers

    def list_papers(self) -> list[dict]:
        return self._papers


PAPERS = [
    {"paper_id": "paper-aaa", "title": "Deep RL for TSN: A Survey"},
    {"paper_id": "paper-bbb", "title": "Attention Is All You Need"},
]


class TestScopeResolution(unittest.TestCase):
    def test_no_argument_means_unscoped(self):
        scope, error = _resolve_scope(_Store(PAPERS), None)
        self.assertIsNone(scope)
        self.assertEqual(error, "")

    def test_empty_values_are_treated_as_unscoped(self):
        for raw in ("", "   ", [], ["", "  "]):
            scope, error = _resolve_scope(_Store(PAPERS), raw)
            self.assertIsNone(scope, raw)
            self.assertEqual(error, "", raw)

    def test_paper_id_resolves_directly(self):
        scope, error = _resolve_scope(_Store(PAPERS), "paper-aaa")
        self.assertEqual(scope, ["paper-aaa"])
        self.assertEqual(error, "")

    def test_title_resolves_with_punctuation_differences(self):
        scope, error = _resolve_scope(_Store(PAPERS), "Deep RL for TSN A Survey")
        self.assertEqual(scope, ["paper-aaa"])
        self.assertEqual(error, "")

    def test_multiple_papers_resolve_for_comparison_questions(self):
        scope, error = _resolve_scope(_Store(PAPERS), ["paper-aaa", "Attention Is All You Need"])
        self.assertEqual(scope, ["paper-aaa", "paper-bbb"])
        self.assertEqual(error, "")

    def test_duplicates_collapse(self):
        scope, _error = _resolve_scope(_Store(PAPERS), ["paper-aaa", "Deep RL for TSN: A Survey"])
        self.assertEqual(scope, ["paper-aaa"])

    def test_unresolvable_scope_is_an_error_not_a_silent_widening(self):
        # Silently falling back to an unscoped search is exactly the pollution
        # scoping exists to prevent, so it must fail loudly instead.
        scope, error = _resolve_scope(_Store(PAPERS), "Quantum Error Correction")
        self.assertIsNone(scope)
        self.assertIn("无法限定到论文", error)
        self.assertIn("paper-aaa", error)

    def test_empty_library_reports_instead_of_returning_empty_scope(self):
        scope, error = _resolve_scope(_Store([]), "anything")
        self.assertIsNone(scope)
        self.assertIn("论文库为空", error)


class TestScopedSearchExecution(unittest.TestCase):
    """The store layer must skip relation expansion whenever a scope is given."""

    def setUp(self) -> None:
        self.calls: list[dict] = []

    def _store(self, *, relations: bool):
        class _Store:
            def __init__(outer, recorder) -> None:
                outer.recorder = recorder

            def query_with_timeout(outer, query, top_k=3, paper_ids=None, section=None):
                outer.recorder.append({"paper_ids": paper_ids, "section": section})
                if outer.recorder and relations:
                    # Simulate the marker relation expansion would attach.
                    return [{
                        "retrieval": "hybrid", "title": "Related", "section": "Methods",
                        "hybrid_score": 0.5, "text": "related chunk",
                        "relation_expanded": True,
                        "relation_types": ["method_similar"],
                        "relation_seed_titles": ["Seed Paper"],
                    }], None
                return [{
                    "retrieval": "hybrid", "title": "Direct", "section": "Methods",
                    "hybrid_score": 0.9, "text": "direct chunk",
                }], None

            def list_papers(outer):
                return PAPERS

        return _Store(self.calls)

    def test_scope_is_passed_through_to_the_store(self):
        from tools.search import handle_query_papers

        handle_query_papers(
            {"query": "loss function", "paper_id_or_title": "Deep RL for TSN: A Survey"},
            paper_store=self._store(relations=True),
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["paper_ids"], ["paper-aaa"])

    def test_unscoped_search_passes_none(self):
        from tools.search import handle_query_papers

        handle_query_papers({"query": "scheduling methods"}, paper_store=self._store(relations=True))
        self.assertEqual(self.calls[0]["paper_ids"], None)

    def test_expansion_marker_is_surfaced_in_the_output(self):
        from tools.search import handle_query_papers

        text = handle_query_papers({"query": "scheduling"}, paper_store=self._store(relations=True))
        self.assertIn("关系扩展候选", text)
        self.assertIn("Seed Paper", text)
        self.assertIn("非直接命中", text)

    def test_expansion_summary_reports_counts(self):
        from tools.search import handle_query_papers

        text = handle_query_papers({"query": "scheduling"}, paper_store=self._store(relations=True))
        self.assertIn("关系扩展：", text)
        self.assertIn("经关系引入 1 条", text)

    def test_unresolvable_scope_returns_an_error_instead_of_results(self):
        from tools.search import handle_query_papers

        text = handle_query_papers(
            {"query": "scheduling", "paper_id_or_title": "Quantum Error Correction"},
            paper_store=self._store(relations=True),
        )
        self.assertIn("无法限定到论文", text)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

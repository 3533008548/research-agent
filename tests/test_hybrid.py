"""papers.hybrid unit tests.

This stage decides *which* chunks reach the reranker, so the cases concentrate
on the properties that keep it honest:

1. relation expansion is subordinate — a related paper cannot displace a direct
   hit, no matter how good its match looks;
2. the semantic cutoff rejects a related paper's worst chunk;
3. seeds come from first-pass results only, so a query never walks the whole
   relation graph;
4. nothing internal leaks into the results the UI renders.
"""

import unittest

from papers import hybrid


def cand(pid, title, idx, **extra):
    d = {"paper_id": pid, "title": title, "chunk_index": idx, "text": f"{title} {idx}"}
    d.update(extra)
    return d


class ResultKeyTest(unittest.TestCase):
    def test_uses_paper_id_title_and_index(self):
        self.assertEqual(
            hybrid.result_key(cand("p1", "T", 3)), ("p1", "T", 3),
        )

    def test_falls_back_to_title_when_paper_id_missing(self):
        """One source may populate paper_id and another may not; the candidate
        must not vanish because of it."""
        self.assertEqual(
            hybrid.result_key({"title": "T", "chunk_index": 0}), ("T", "T", 0),
        )

    def test_chunk_index_is_coerced_to_int(self):
        self.assertEqual(hybrid.result_key({"title": "T", "chunk_index": "7"})[2], 7)

    def test_missing_chunk_index_defaults_to_zero(self):
        self.assertEqual(hybrid.result_key({"title": "T"})[2], 0)


class SortTest(unittest.TestCase):
    def test_orders_by_descending_score(self):
        data = [cand("p", "A", 0, hybrid_score=0.1), cand("p", "B", 0, hybrid_score=0.3)]
        self.assertEqual([r["title"] for r in hybrid.sort_hybrid_results(data)], ["B", "A"])

    def test_equal_scores_break_by_best_rank(self):
        data = [
            cand("p", "A", 0, hybrid_score=0.1, keyword_rank=5),
            cand("p", "B", 0, hybrid_score=0.1, semantic_rank=2),
        ]
        self.assertEqual(hybrid.sort_hybrid_results(data)[0]["title"], "B")

    def test_unranked_sorts_last(self):
        data = [
            cand("p", "A", 0, hybrid_score=0.1),
            cand("p", "B", 0, hybrid_score=0.1, keyword_rank=9),
        ]
        self.assertEqual(hybrid.sort_hybrid_results(data)[0]["title"], "B")

    def test_ordering_is_deterministic(self):
        runs = set()
        for _ in range(6):
            data = [cand("p", "Same", i, hybrid_score=0.1) for i in range(5)]
            runs.add(tuple(r["chunk_index"] for r in hybrid.sort_hybrid_results(data)))
        self.assertEqual(len(runs), 1)

    def test_input_is_not_mutated(self):
        data = [cand("p", "B", 0, hybrid_score=0.1), cand("p", "A", 0, hybrid_score=0.3)]
        before = [d["title"] for d in data]
        hybrid.sort_hybrid_results(data)
        self.assertEqual([d["title"] for d in data], before)


class CandidateCountTest(unittest.TestCase):
    def test_scales_with_top_k(self):
        self.assertEqual(hybrid.candidate_count(3), 12)

    def test_never_below_reranker_limit(self):
        self.assertEqual(hybrid.candidate_count(1, reranker_limit=12), 12)

    def test_respects_hard_ceiling(self):
        self.assertEqual(hybrid.candidate_count(20, reranker_limit=99), hybrid.HYBRID_CANDIDATE_LIMIT)


class RrfTest(unittest.TestCase):
    def test_same_chunk_from_both_sources_scores_twice(self):
        shared = cand("p1", "A", 0)
        fused = hybrid.reciprocal_rank_fusion([("semantic", [shared]), ("keyword", [shared])])
        self.assertEqual(len(fused), 1)
        item = next(iter(fused.values()))
        self.assertEqual(item["semantic_rank"], 1)
        self.assertEqual(item["keyword_rank"], 1)
        # Each contribution is rounded to 6 decimals before being summed, so
        # compare against the same accumulation rather than an exact formula --
        # an equality check here would be asserting floating-point luck.
        one = round(1 / (hybrid.RRF_K + 1), 6)
        self.assertAlmostEqual(item["hybrid_score"], one + one, delta=0.000002)

    def test_records_each_source_rank(self):
        fused = hybrid.reciprocal_rank_fusion([
            ("semantic", [cand("p1", "A", 0), cand("p1", "A", 1)]),
            ("keyword", [cand("p2", "B", 0)]),
        ])
        # Index by the full identity: p1 contributes two chunks, so collapsing
        # on paper_id would let one overwrite the other and hide the real ranks.
        self.assertEqual(fused[("p1", "A", 0)]["semantic_rank"], 1)
        self.assertEqual(fused[("p1", "A", 1)]["semantic_rank"], 2)
        self.assertEqual(fused[("p2", "B", 0)]["keyword_rank"], 1)
        self.assertNotIn("keyword_rank", fused[("p1", "A", 0)])
        self.assertNotIn("semantic_rank", fused[("p2", "B", 0)])

    def test_input_candidates_are_not_mutated(self):
        data = [cand("p1", "A", 0)]
        snapshot = [dict(d) for d in data]
        hybrid.reciprocal_rank_fusion([("semantic", data)])
        self.assertEqual(data, snapshot)

    def test_empty_sources_yield_empty_result(self):
        self.assertEqual(hybrid.reciprocal_rank_fusion([("semantic", []), ("keyword", [])]), {})


class SeedTest(unittest.TestCase):
    def test_takes_distinct_papers_in_rank_order(self):
        ranked = [cand("p1", "A", 0), cand("p1", "A", 1), cand("p2", "B", 0)]
        self.assertEqual(hybrid.relation_seed_ids(ranked), ["p1", "p2"])

    def test_respects_the_limit(self):
        ranked = [cand(f"p{i}", f"T{i}", 0) for i in range(10)]
        self.assertEqual(len(hybrid.relation_seed_ids(ranked, limit=3)), 3)

    def test_skips_blank_paper_ids(self):
        ranked = [{"title": "A", "chunk_index": 0}, cand("p2", "B", 0)]
        self.assertEqual(hybrid.relation_seed_ids(ranked), ["p2"])


class ExpandTest(unittest.TestCase):
    def _expand(self, ranked, hints, sem=None, lex=None, **kw):
        fused = {hybrid.result_key(c): dict(c, hybrid_score=0.01) for c in ranked}
        store = None if hints is None else type(
            "S", (), {"related_paper_boosts": staticmethod(lambda seeds: dict(hints))},
        )()
        hybrid.expand_relation_candidates(
            "q", fused, ranked, candidate_k=10, section=None,
            relation_store=store,
            query_semantic=sem or (lambda *a, **k: []),
            query_lexical=lex or (lambda *a, **k: []),
            **kw,
        )
        return fused

    def test_no_relation_store_is_a_no_op(self):
        fused = self._expand([cand("p1", "A", 0, distance=0.1)], None)
        self.assertEqual(len(fused), 1)

    def test_seed_cannot_boost_itself(self):
        fused = self._expand(
            [cand("p1", "A", 0, distance=0.1)], {"p1": {"boost": 0.9}},
        )
        self.assertAlmostEqual(fused[hybrid.result_key(cand("p1", "A", 0))]["hybrid_score"], 0.01)

    def test_relation_gets_a_fraction_of_a_vote(self):
        """A related paper must never outweigh a direct hit of the same rank."""
        ranked = [cand("p1", "A", 0, distance=0.1)]
        fused = self._expand(
            ranked, {"p2": {"boost": 0.0}},
            sem=lambda *a, **k: [cand("p2", "B", 0, distance=0.05)],
        )
        related = fused[hybrid.result_key(cand("p2", "B", 0))]
        self.assertTrue(related["relation_expanded"])
        direct = fused[hybrid.result_key(cand("p1", "A", 0))]
        # The related chunk entered at rank 1 of its own source with a 0.5
        # factor, so a direct first-pass hit stays ahead of it.
        self.assertGreater(direct["hybrid_score"], related["hybrid_score"])

    def test_semantic_cutoff_rejects_worse_chunk(self):
        ranked = [cand("p1", "A", 0, distance=0.2)]
        fused = self._expand(
            ranked, {"p2": {"boost": 0.3}},
            sem=lambda *a, **k: [cand("p2", "B", 0, distance=0.99)],
        )
        self.assertNotIn(hybrid.result_key(cand("p2", "B", 0)), fused)

    def test_lexical_ignores_the_semantic_cutoff(self):
        """The cutoff is a distance concept; keyword hits have no distance."""
        ranked = [cand("p1", "A", 0, distance=0.2)]
        fused = self._expand(
            ranked, {"p2": {"boost": 0.3}},
            lex=lambda *a, **k: [cand("p2", "B", 0, keyword_score=3.0)],
        )
        self.assertIn(hybrid.result_key(cand("p2", "B", 0)), fused)

    def test_hints_are_attached_for_auditability(self):
        """A *non-seed* paper's existing entry receives the audit trail.

        Seeds are capped at3, so four ranked papers are needed for the last one
        to fall outside the seed set -- otherwise it would seed itself and be
        excluded from hints.
        """
        ranked = [
            cand("p1", "A", 0, distance=0.1),
            cand("p2", "B", 0, distance=0.15),
            cand("p3", "C", 0, distance=0.2),
            cand("p9", "Z", 0, distance=0.3),
        ]
        fused = self._expand(
            ranked,
            {"p9": {"boost": 0.1, "relation_ids": ["r1"], "relation_types": ["extends"]}},
        )
        item = fused[hybrid.result_key(cand("p9", "Z", 0))]
        self.assertEqual(item["relation_boost"], 0.1)
        self.assertEqual(item["relation_ids"], ["r1"])
        self.assertEqual(item["relation_types"], ["extends"])

    def test_internal_marker_never_leaks(self):
        ranked = [cand("p1", "A", 0, distance=0.1)]
        fused = self._expand(ranked, {"p1": {"boost": 0.3}})
        for item in fused.values():
            self.assertNotIn("_relation_score_applied", item)

    def test_failing_relation_store_degrades(self):
        class Broken:
            def related_paper_boosts(self, seeds):
                raise RuntimeError("down")

        fused = {hybrid.result_key(cand("p1", "A", 0)): dict(cand("p1", "A", 0), hybrid_score=0.01)}
        hybrid.expand_relation_candidates(
            "q", fused, [cand("p1", "A", 0, distance=0.1)],
            candidate_k=10, section=None, relation_store=Broken(),
            query_semantic=lambda *a, **k: [], query_lexical=lambda *a, **k: [],
        )
        self.assertEqual(len(fused), 1)

    def test_failing_secondary_query_degrades(self):
        def boom(*a, **k):
            raise RuntimeError("query failed")

        ranked = [cand("p1", "A", 0, distance=0.1)]
        fused = self._expand(ranked, {"p2": {"boost": 0.3}}, sem=boom)
        # Existing entries may already carry the hint; no new candidate is added.
        self.assertNotIn(hybrid.result_key(cand("p2", "B", 0)), fused)

    def test_secondary_queries_receive_the_related_ids(self):
        seen = {}

        def capture(query_text, k, paper_ids=None, section=None):
            seen.setdefault("ids", paper_ids)
            return []

        self._expand(
            [cand("p1", "A", 0, distance=0.1)],
            {"p3": {"boost": 0.2}, "p2": {"boost": 0.1}, "p4": {"boost": 0.05}},
            sem=capture, lex=capture,
        )
        self.assertEqual(seen["ids"], ["p2", "p3", "p4"])


if __name__ == "__main__":
    unittest.main()
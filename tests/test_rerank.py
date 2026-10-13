"""papers.rerank unit tests.

The cross-encoder is the quality pillar, so these cases focus on the three ways
ranking can silently go wrong:

1. non-deterministic tie-breaks — two equal scores must always order the same
   way, otherwise A/B comparisons of R@1 measure noise;
2. input mutation — the degraded path hands the same list back, so sorting in
   place would corrupt the RRF fallback;
3. the degradation path itself — a failing model must leave order untouched,
   never raise.
"""

import unittest

from papers import rerank


def candidates(n=6):
    out = []
    for i in range(n):
        out.append({
            "text": f"doc {i}",
            "title": f"Paper {i % 3}",
            "paper_id": f"p{i % 4}",
            "chunk_index": i % 2,
            "hybrid_score": 0.01 * (i % 3),
        })
    return out


class ApplyScoresTest(unittest.TestCase):
    def test_sorts_descending_by_score(self):
        result = rerank.apply_scores(candidates(3), [0.1, 0.9, 0.5])
        self.assertEqual([r["reranker_score"] for r in result], [0.9, 0.5, 0.1])

    def test_marks_every_item_reranked(self):
        for r in rerank.apply_scores(candidates(3), [0.1, 0.2, 0.3]):
            self.assertIs(r["reranked"], True)

    def test_score_is_rounded_to_six_decimals(self):
        result = rerank.apply_scores(candidates(1), [0.1234567891])
        self.assertEqual(result[0]["reranker_score"], 0.123457)

    def test_input_is_not_mutated(self):
        original = candidates(4)
        snapshot = [dict(c) for c in original]
        rerank.apply_scores(original, [0.4, 0.3, 0.2, 0.1])
        self.assertEqual(original, snapshot)

    def test_full_ties_are_deterministic(self):
        """All scores equal must still order identically across runs."""
        runs = []
        for _ in range(6):
            out = rerank.apply_scores(candidates(5), [0.5] * 5)
            runs.append(tuple(r["title"] for r in out))
        self.assertEqual(len(set(runs)), 1, "tie-break is not deterministic")

    def test_ties_fall_through_to_hybrid_score(self):
        data = [
            {"text": "a", "title": "A", "chunk_index": 0, "hybrid_score": 0.1},
            {"text": "b", "title": "B", "chunk_index": 1, "hybrid_score": 0.9},
        ]
        result = rerank.apply_scores(data, [0.5, 0.5])
        self.assertEqual(result[0]["title"], "B")

    def test_negative_scores_are_kept(self):
        result = rerank.apply_scores(candidates(2), [-5.0, -1.0])
        self.assertEqual([r["reranker_score"] for r in result], [-1.0, -5.0])


class RerankWithTest(unittest.TestCase):
    def test_reorders_via_predict(self):
        seen = {}

        def predict(pairs, **kwargs):
            seen["pairs"] = pairs
            return [0.1, 0.9, 0.5]

        result = rerank.rerank_with("query", candidates(3), predict)
        self.assertEqual([r["reranker_score"] for r in result], [0.9, 0.5, 0.1])

    def test_predict_receives_query_document_pairs(self):
        seen = {}

        def predict(pairs, **kwargs):
            seen["pairs"] = list(pairs)
            return [0.1, 0.2]

        data = [{"text": "first"}, {"text": "second"}]
        rerank.rerank_with("my query", data, predict)
        self.assertEqual(seen["pairs"], [("my query", "first"), ("my query", "second")])

    def test_batch_kwargs_are_passed(self):
        seen = {}

        def predict(pairs, **kwargs):
            seen.update(kwargs)
            return [0.1]

        rerank.rerank_with("q", [{"text": "x"}], predict)
        self.assertEqual(seen["batch_size"], 8)
        self.assertIs(seen["show_progress_bar"], False)

    def test_empty_candidates_skip_predict_entirely(self):
        def predict(pairs, **kwargs):
            raise AssertionError("predict must not be called")

        self.assertEqual(rerank.rerank_with("q", [], predict), [])

    def test_failure_returns_input_order_unchanged(self):
        data = candidates(5)
        before = [dict(c) for c in data]

        def predict(pairs, **kwargs):
            raise RuntimeError("model unavailable")

        self.assertEqual(rerank.rerank_with("q", data, predict), before)

    def test_failure_does_not_mutate_input(self):
        data = candidates(4)
        snapshot = [dict(c) for c in data]

        def predict(pairs, **kwargs):
            raise ValueError("bad")

        rerank.rerank_with("q", data, predict)
        self.assertEqual(data, snapshot)

    def test_short_score_list_truncates_like_zip(self):
        """Behaviour preserved from the pre-refactor bare zip.

        Truncating here is arguably a latent bug, but "fixing" it inside a
        refactor would change behaviour with nobody measuring retrieval numbers.
        """
        data = candidates(4)
        result = rerank.rerank_with("q", data, lambda pairs, **kw: [0.9, 0.8])
        self.assertEqual(len(result), 2)

    def test_long_score_list_is_ignored_past_candidates(self):
        data = candidates(3)
        result = rerank.rerank_with("q", data, lambda pairs, **kw: [0.1, 0.2, 0.3, 0.4, 0.5])
        self.assertEqual(len(result), 3)


class LoadCrossEncoderTest(unittest.TestCase):
    def test_missing_model_raises_for_caller_to_catch(self):
        """The store catches this and degrades; the loader must not hide it.

        Patched rather than pointed at a real missing repo: an earlier version
        used a bogus model name and made this suite take 27 seconds while the
        import machinery probed the network.
        """
        import sys
        import types

        def exploding_cross_encoder(*args, **kwargs):
            raise RuntimeError("weights not available locally")

        fake_module = types.ModuleType("sentence_transformers")
        fake_module.CrossEncoder = exploding_cross_encoder
        original = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = fake_module
        try:
            with self.assertRaises(RuntimeError):
                rerank.load_cross_encoder("any/model", max_length=256)
        finally:
            if original is None:
                sys.modules.pop("sentence_transformers", None)
            else:
                sys.modules["sentence_transformers"] = original

    def test_loader_forwards_settings_to_the_model(self):
        import sys
        import types

        captured = {}

        class FakeCrossEncoder:
            def __init__(self, name, **kwargs):
                captured["name"] = name
                captured.update(kwargs)

        fake_module = types.ModuleType("sentence_transformers")
        fake_module.CrossEncoder = FakeCrossEncoder
        original = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = fake_module
        try:
            rerank.load_cross_encoder("some/model", max_length=256, local_files_only=True)
        finally:
            if original is None:
                sys.modules.pop("sentence_transformers", None)
            else:
                sys.modules["sentence_transformers"] = original

        self.assertEqual(captured["name"], "some/model")
        self.assertEqual(captured["max_length"], 256)
        self.assertIs(captured["local_files_only"], True)


class NoVectorStoreTest(unittest.TestCase):
    def test_rerank_ranks_without_any_model(self):
        """The point of the refactor: ordering needs no weights at all."""
        result = rerank.apply_scores(candidates(4), [0.1, 0.4, 0.2, 0.3])
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["reranker_score"], 0.4)

    def test_import_pulls_no_heavy_dependency(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        probe = (
            "import sys\n"
            "import papers.rerank as r\n"
            "assert r.apply_scores([{'text':'a'}], [1.0])[0]['reranked']\n"
            "bad = [m for m in ('torch', 'transformers', 'sentence_transformers',\n"
            "                   'chromadb', 'paper_store') if m in sys.modules]\n"
            "assert not bad, 'rerank pulled in: ' + ','.join(bad)\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=root,
            env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
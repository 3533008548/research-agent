"""papers.lexical unit tests.

BM25 decides which chunks the reranker gets to see, so a silent scoring change
here degrades R@1 without raising anything.  These cases run against plain lists
— no ChromaDB, no embedder, no ``PaperStore.__new__`` — which was impossible
while the scoring lived inside the store.

Two properties deserve the most attention:

1. ``df`` and average length are computed over the **filtered** candidate set,
   not the whole corpus.  Scoping a query to one paper therefore changes the
   scores of that paper's chunks.  That is intentional (it matches the
   pre-index implementation) and is asserted below, so nobody "fixes" it later.
2. The index path and the streaming path must rank identically for the same
   corpus; they exist for different corpus sizes, not for different answers.
"""

import unittest

from papers import lexical


def docs():
    return [
        ("Attention is all you need for sequence transduction.", {
            "paper_id": "p1", "title": "Attention", "section": "Method", "chunk_index": 0,
        }),
        ("The transformer uses multi-head self attention without recurrence.", {
            "paper_id": "p1", "title": "Attention", "section": "Method", "chunk_index": 1,
        }),
        ("Diffusion models generate samples by iteratively denoising noise.", {
            "paper_id": "p2", "title": "Diffusion", "section": "Abstract", "chunk_index": 0,
        }),
        ("我们提出了一种基于注意力的序列建模方法。", {
            "paper_id": "p2", "title": "Diffusion", "section": "引言", "chunk_index": 1,
        }),
        ("Evaluation metrics include FID and Inception Score.", {
            "paper_id": "p3", "title": "Metrics", "section": "Results", "chunk_index": 0,
        }),
    ]


class TokenizeTest(unittest.TestCase):
    def test_latin_words_are_lowercased(self):
        self.assertEqual(lexical.tokenize("Attention Transformer"), ["attention", "transformer"])

    def test_single_letter_words_are_dropped(self):
        """The regex requires two characters, so "a" and "I" are not terms."""
        self.assertEqual(lexical.tokenize("a I be"), ["be"])

    def test_underscore_identifiers_survive(self):
        self.assertIn("cross_entropy", lexical.tokenize("cross_entropy loss"))

    def test_cjk_uses_character_bigrams(self):
        self.assertEqual(lexical.tokenize("注意力"), ["注意", "意力"])

    def test_single_cjk_character_yields_nothing(self):
        self.assertEqual(lexical.tokenize("中"), [])

    def test_mixed_scripts_produce_both(self):
        tokens = lexical.tokenize("attention 注意力")
        self.assertIn("attention", tokens)
        self.assertIn("注意", tokens)

    def test_empty_input(self):
        self.assertEqual(lexical.tokenize(""), [])


class QueryTermsTest(unittest.TestCase):
    def test_duplicates_are_removed_preserving_order(self):
        """Dedup matters for scoring: a repeated term would score twice."""
        self.assertEqual(
            lexical.query_terms("alpha beta alpha gamma beta"),
            ["alpha", "beta", "gamma"],
        )

    def test_empty_query(self):
        self.assertEqual(lexical.query_terms(""), [])

    def test_query_terms_is_a_dedup_of_tokenize(self):
        text = "attention attention diffusion"
        self.assertEqual(
            lexical.query_terms(text), list(dict.fromkeys(lexical.tokenize(text)))
        )


class BuildIndexTest(unittest.TestCase):
    def test_index_shape(self):
        index = lexical.build_index(docs())
        self.assertEqual(len(index["entries"]), 5)
        self.assertEqual(len(index["lengths"]), 5)
        self.assertIn("attention", index["postings"])

    def test_entries_carry_metadata_with_defaults(self):
        index = lexical.build_index([("text", None)])
        entry = index["entries"][0]
        self.assertEqual(entry["section"], "未标注")
        self.assertEqual(entry["title"], "Unknown")
        self.assertEqual(entry["page"], -1)
        self.assertEqual(entry["retrieval"], "keyword")

    def test_page_end_falls_back_to_page(self):
        index = lexical.build_index([("t", {"page": 5})])
        self.assertEqual(index["entries"][0]["page_end"], 5)

    def test_postings_record_term_frequency(self):
        index = lexical.build_index([("alpha alpha alpha beta", None)])
        self.assertEqual(index["postings"]["alpha"][0], 3)
        self.assertEqual(index["postings"]["beta"][0], 1)

    def test_empty_corpus(self):
        index = lexical.build_index([])
        self.assertEqual(index["entries"], [])
        self.assertEqual(index["postings"], {})


class ScoreIndexTest(unittest.TestCase):
    def setUp(self):
        self.index = lexical.build_index(docs())

    def test_relevant_chunk_ranks_first(self):
        results = lexical.score_index(self.index, lexical.query_terms("diffusion"), 3)
        self.assertTrue(results)
        self.assertEqual(results[0]["paper_id"], "p2")

    def test_all_results_carry_keyword_score(self):
        for r in lexical.score_index(self.index, lexical.query_terms("attention"), 3):
            self.assertGreater(r["keyword_score"], 0)

    def test_unknown_term_returns_nothing(self):
        self.assertEqual(lexical.score_index(self.index, ["zzzz"], 5), [])

    def test_top_k_is_respected(self):
        results = lexical.score_index(self.index, lexical.query_terms("attention"), 2)
        self.assertLessEqual(len(results), 2)

    def test_paper_scope_restricts_results(self):
        results = lexical.score_index(self.index, lexical.query_terms("attention"), 5, ["p3"])
        self.assertEqual(results, [])

    def test_section_scope_filters(self):
        """The chunk under the "引言" heading is the Chinese one, so the query
        has to contain a term it actually contains -- scoping does not create
        matches that the terms would not have found anyway."""
        results = lexical.score_index(
            self.index, lexical.query_terms("注意力"), 5, None, "引言",
        )
        self.assertEqual([r["section"] for r in results], ["引言"])
        self.assertEqual(results[0]["chunk_index"], 1)

    def test_scope_with_no_match_returns_empty(self):
        self.assertEqual(lexical.score_index(self.index, ["attention"], 5, ["nope"]), [])

    def test_statistics_are_scoped_to_the_filtered_set(self):
        """df and avg length use the candidate set, so scope changes scores.

        Locked in deliberately: the pre-index implementation behaved this way and
        changing it would silently reorder results for scoped queries.
        """
        terms = lexical.query_terms("attention")
        unscoped = lexical.score_index(self.index, terms, 5)
        scoped = lexical.score_index(self.index, terms, 5, ["p1"])
        self.assertNotEqual(
            unscoped[0]["keyword_score"], scoped[0]["keyword_score"],
            "scoping must not be a no-op for scoring",
        )

    def test_ties_break_deterministically(self):
        """Same score must not reorder between runs (dict iteration varies)."""
        runs = [
            [r["paper_id"] for r in lexical.score_index(self.index, lexical.query_terms("attention"), 5)]
            for _ in range(5)
        ]
        self.assertEqual(len(set(map(tuple, runs))), 1)


class ScanScoreTest(unittest.TestCase):
    def test_matches_index_path(self):
        terms = lexical.query_terms("attention diffusion")
        indexed = lexical.score_index(lexical.build_index(docs()), terms, 5)
        scanned = lexical.scan_score(docs(), terms, 5)
        self.assertEqual(
            [r["keyword_score"] for r in indexed],
            [r["keyword_score"] for r in scanned],
        )

    def test_empty_corpus(self):
        self.assertEqual(lexical.scan_score([], ["a"], 5), [])

    def test_scope_filters_before_scoring(self):
        results = lexical.scan_score(docs(), lexical.query_terms("attention"), 5, ["p1"])
        self.assertTrue(results)
        self.assertTrue(all(r["paper_id"] == "p1" for r in results))

    def test_no_matching_term_returns_empty(self):
        self.assertEqual(lexical.scan_score(docs(), ["zzzzz"], 5), [])

    def test_top_k_is_respected(self):
        results = lexical.scan_score(docs(), lexical.query_terms("attention"), 1)
        self.assertLessEqual(len(results), 1)


class AllowedPositionsTest(unittest.TestCase):
    def setUp(self):
        self.entries = lexical.build_index(docs())["entries"]

    def test_empty_scope_means_everything(self):
        self.assertEqual(lexical.allowed_positions(self.entries, None, None), {0, 1, 2, 3, 4})

    def test_paper_filter(self):
        self.assertEqual(lexical.allowed_positions(self.entries, ["p2"], None), {2, 3})

    def test_section_filter(self):
        self.assertEqual(lexical.allowed_positions(self.entries, None, "Method"), {0, 1})

    def test_both_filters_intersect(self):
        self.assertEqual(lexical.allowed_positions(self.entries, ["p1"], "Method"), {0, 1})
        self.assertEqual(lexical.allowed_positions(self.entries, ["p2"], "Method"), set())

    def test_empty_paper_list_is_not_a_filter(self):
        """An empty scope still means "unscoped"; only a non-empty list narrows."""
        self.assertEqual(lexical.allowed_positions(self.entries, [], None), {0, 1, 2, 3, 4})


class NoVectorStoreTest(unittest.TestCase):
    """The refactor's point, asserted so it cannot silently regress."""

    def test_lexical_imports_no_chromadb(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        env = {"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"}
        probe = (
            "import sys\n"
            "import papers.lexical as l\n"
            # Needs a term long enough to survive the >=2-character rule;
            # 'a b' tokenizes to nothing, so postings would be empty.
            "assert l.build_index([('alpha beta', None)])['postings']['alpha']\n"
            "bad = [m for m in ('chromadb','paper_store','embeddings','numpy','torch')\n"
            "       if m in sys.modules]\n"
            "assert not bad, 'lexical pulled in: ' + ','.join(bad)\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

    def test_bm25_constants_live_here_not_in_paper_store(self):
        import paper_store

        self.assertFalse(hasattr(paper_store, "BM25_K1"))
        self.assertEqual(lexical.BM25_K1, 1.5)
        self.assertEqual(lexical.BM25_B, 0.75)
        self.assertEqual(lexical.BM25_SCALE, 2.5)


if __name__ == "__main__":
    unittest.main()
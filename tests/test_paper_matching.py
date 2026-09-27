"""Managed papers must stay findable after files are renamed for readability."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_artifacts import (
    load_document_map,
    title_match_key,
    title_match_score,
)
from runtime_paths import RuntimePaths


class _Store:
    def __init__(self, papers: list[dict]) -> None:
        self._papers = papers

    def list_papers(self) -> list[dict]:
        return self._papers


def _write_document_map(paths: RuntimePaths, paper_id: str, source_file: str) -> None:
    target = paths.paper_artifacts_dir / paper_id / "document_map.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"paper_id": paper_id, "source_file": source_file}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestTitleNormalisation(unittest.TestCase):
    def test_key_ignores_case_punctuation_and_spacing(self):
        self.assertEqual(
            title_match_key("Deep RL for TSN: A Survey.pdf"),
            title_match_key("  deep   rl  for  TSN - a survey  "),
        )

    def test_key_keeps_cjk_characters(self):
        self.assertEqual(
            title_match_key("基于深度强化学习的流量调度"),
            "基于深度强化学习的流量调度",
        )

    def test_containment_outranks_a_coincidental_fuzzy_match(self):
        contained = title_match_score("Attention", "Attention Is All You Need")
        fuzzy = title_match_score("Totally Different", "Attention Is All You Need")
        self.assertGreater(contained, fuzzy)
        self.assertGreaterEqual(contained, 0.70)
        self.assertLess(fuzzy, 0.70)

    def test_truncated_title_still_matches(self):
        score = title_match_score(
            "A Very Long Paper Title About Networks", "A Very Long Paper Title About N",
        )
        self.assertGreaterEqual(score, 0.70)


class TestSourcePdfResolution(unittest.TestCase):
    """The recorded file name is authoritative; title guessing is only a fallback."""

    def setUp(self) -> None:
        from tools.paper_card import _find_source_pdf

        self._find_source_pdf = _find_source_pdf
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(Path(self._tmp.name) / "runtime")
        self.paths.ensure_initialized()
        self.paths.papers_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _touch(self, name: str) -> None:
        (self.paths.papers_dir / name).write_bytes(b"%PDF-placeholder")

    def test_document_map_recovers_a_file_whose_name_differs_from_the_title(self):
        self._touch("2301_00451.pdf")
        self._touch("some_other_paper.pdf")
        _write_document_map(self.paths, "paper-1", "2301_00451.pdf")

        found, kind = self._find_source_pdf(self.paths, "paper-1", "Deep RL for TSN: A Survey")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "2301_00451.pdf")
        self.assertEqual(kind, "document_map")

    def test_fallback_matches_a_title_with_characters_a_filename_cannot_hold(self):
        # ':' and '/' are illegal in Windows file names, so the stored file
        # necessarily drops them.  Literal comparison can never match.
        self._touch("Deep RL for TSN A Survey.pdf")

        found, kind = self._find_source_pdf(self.paths, "paper-1", "Deep RL for TSN: A Survey")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Deep RL for TSN A Survey.pdf")
        self.assertTrue(kind.startswith("file_name"))

    def test_unrelated_file_is_not_matched(self):
        self._touch("An Entirely Different Paper.pdf")

        found, kind = self._find_source_pdf(self.paths, "paper-1", "Deep RL for TSN: A Survey")
        self.assertIsNone(found)
        self.assertEqual(kind, "")

    def test_stale_document_map_is_repaired_after_a_rename(self):
        from paper_artifacts import update_document_map_source_file

        # The user renamed the file to a readable title, leaving the recorded
        # download name behind.
        self._touch("Deep RL for TSN A Survey.pdf")
        _write_document_map(self.paths, "paper-1", "old_download_name.pdf")

        found, kind = self._find_source_pdf(self.paths, "paper-1", "Deep RL for TSN: A Survey")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Deep RL for TSN A Survey.pdf")
        self.assertTrue(kind.startswith("file_name"))

        self.assertTrue(update_document_map_source_file(self.paths, "paper-1", found.name))
        self.assertEqual(
            str((load_document_map(self.paths, "paper-1") or {}).get("source_file")),
            "Deep RL for TSN A Survey.pdf",
        )
        # The next lookup no longer needs the fallback.
        again, again_kind = self._find_source_pdf(self.paths, "paper-1", "Deep RL for TSN: A Survey")
        self.assertEqual(again_kind, "document_map")
        self.assertEqual(again.name, "Deep RL for TSN A Survey.pdf")


class TestPaperLookup(unittest.TestCase):
    """A scored best match replaces the old 'must match exactly one' rule."""

    PAPERS = [
        {"paper_id": "paper-1", "title": "Deep RL for TSN: A Survey"},
        {"paper_id": "paper-2", "title": "Deep RL for TSN: A Survey (Extended Version)"},
        {"paper_id": "paper-3", "title": "Attention Is All You Need"},
    ]

    def setUp(self) -> None:
        from tools.paper_card import _find_paper

        self._find_paper = _find_paper

    def test_exact_paper_id_wins(self):
        paper, _available, kind = self._find_paper(_Store(self.PAPERS), "paper-2")
        self.assertEqual(paper["paper_id"], "paper-2")
        self.assertEqual(kind, "paper_id")

    def test_punctuation_in_the_query_does_not_hide_the_paper(self):
        paper, _available, kind = self._find_paper(_Store(self.PAPERS), "Deep RL for TSN A Survey")
        self.assertEqual(paper["paper_id"], "paper-1")
        self.assertTrue(kind.startswith("title"))

    def test_near_duplicate_titles_still_resolve_to_the_best_one(self):
        # Previously two matching titles meant "not found" at all.
        paper, _available, kind = self._find_paper(
            _Store(self.PAPERS), "Deep RL for TSN: A Survey (Extended Version)",
        )
        self.assertEqual(paper["paper_id"], "paper-2")
        self.assertTrue(kind.startswith("title"))

    def test_unrelated_query_is_rejected_but_lists_candidates(self):
        paper, available, kind = self._find_paper(_Store(self.PAPERS), "Quantum Error Correction")
        self.assertIsNone(paper)
        self.assertEqual(kind, "")
        self.assertEqual(len(available), 3)


class TestResearchDocumentPaperResolution(unittest.TestCase):
    def test_near_duplicate_titles_no_longer_fail_the_whole_request(self):
        from tools.research_documents import _resolve_papers

        store = _Store([
            {"paper_id": "paper-1", "title": "Deep RL for TSN: A Survey"},
            {"paper_id": "paper-2", "title": "Deep RL for TSN: A Survey (Extended)"},
        ])
        resolved = _resolve_papers(store, ["Deep RL for TSN: A Survey"])
        self.assertIsInstance(resolved, list)
        self.assertEqual([item["paper_id"] for item in resolved], ["paper-1"])

    def test_unknown_paper_reports_indexed_candidates(self):
        from tools.research_documents import _resolve_papers

        store = _Store([{"paper_id": "paper-1", "title": "Deep RL for TSN: A Survey"}])
        result = _resolve_papers(store, ["Quantum Error Correction"])
        self.assertIsInstance(result, str)
        self.assertIn("paper-1", result)


if __name__ == "__main__":
    unittest.main()

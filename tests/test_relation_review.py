"""Automatic relation review: candidates, incrementality, jobs and migration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_relation_review import (
    PaperRelationReviewStore,
    PaperRelationReviewer,
    detect_citation_candidates,
    paper_fingerprint,
)
from runtime_paths import RuntimePaths


class _FakeStore:
    def __init__(self, papers: list[dict], chunks: dict[str, list[dict]]) -> None:
        self._papers = papers
        self._chunks = chunks

    def list_papers(self) -> list[dict]:
        return self._papers

    def get_paper_chunks(self, paper_id: str) -> list[dict]:
        return self._chunks.get(paper_id, [])


def _chunk(paper_id: str, index: int, text: str, page: int = 3) -> dict:
    return {
        "paper_id": paper_id,
        "title": "Source Paper",
        "chunk_index": index,
        "text": text,
        "page": page,
    }


class TestFingerprint(unittest.TestCase):
    def test_same_content_gives_same_fingerprint(self):
        self.assertEqual(
            paper_fingerprint(title="A Survey", chunk_count=12, source_file="a.pdf"),
            paper_fingerprint(title="  A   Survey ", chunk_count=12, source_file="A.PDF"),
        )

    def test_content_change_changes_fingerprint(self):
        self.assertNotEqual(
            paper_fingerprint(title="A Survey", chunk_count=12, source_file="a.pdf"),
            paper_fingerprint(title="A Survey", chunk_count=13, source_file="a.pdf"),
        )


class TestCitationDetection(unittest.TestCase):
    PAPERS = [
        {"paper_id": "p-new", "title": "A Scalable Asynchronous Traffic Shaping Mechanism for TSN"},
        {"paper_id": "p-old", "title": "A Theory of Traffic Regulators for Deterministic Networks"},
        {"paper_id": "p-other", "title": "Wi-Fi 6: A First Look"},
    ]

    def test_cited_title_becomes_a_candidate_with_an_anchor(self):
        chunks = [
            _chunk("p-new", 0, "Intro text."),
            _chunk(
                "p-new", 4,
                "As shown in A Theory of Traffic Regulators for Deterministic Networks, "
                "the delay bound holds.",
                page=7,
            ),
        ]
        found = detect_citation_candidates(self.PAPERS[0], chunks, self.PAPERS[1:])
        self.assertEqual(len(found), 1)
        candidate = found[0]
        self.assertEqual(candidate["relation_type"], "explicit_citation")
        self.assertEqual(candidate["target_paper_id"], "p-old")
        self.assertEqual(candidate["evidence"][0]["page"], 7)
        self.assertEqual(candidate["status"], "pending")

    def test_uncited_paper_produces_no_candidate(self):
        chunks = [_chunk("p-new", 1, "We propose a new mechanism for shaping.")]
        self.assertEqual(detect_citation_candidates(self.PAPERS[0], chunks, self.PAPERS[1:]), [])

    def test_near_duplicate_title_is_not_treated_as_a_citation(self):
        chunks = [_chunk("p-new", 1, "We extend A Scalable Asynchronous Traffic Shaping Mechanism for TSN.")]
        others = [{"paper_id": "p-dup", "title": "A Scalable Asynchronous Traffic Shaping Mechanism for TSN"}]
        self.assertEqual(detect_citation_candidates(self.PAPERS[0], chunks, others), [])

    def test_short_title_is_skipped_to_avoid_loose_matches(self):
        chunks = [_chunk("p-new", 1, "We cite Wi-Fi 6: A First Look here in full.")]
        others = [{"paper_id": "p-short", "title": "Wi-Fi 6"}]
        self.assertEqual(detect_citation_candidates(self.PAPERS[0], chunks, others), [])


class TestJobQueue(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(Path(self._tmp.name) / "runtime")
        self.paths.ensure_initialized()
        self.store = PaperRelationReviewStore(self.paths)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_same_paper_and_fingerprint_is_not_queued_twice(self):
        self.assertTrue(self.store.enqueue("p-1", "fp-a"))
        self.assertFalse(self.store.enqueue("p-1", "fp-a"))

    def test_new_fingerprint_requeues_the_same_paper(self):
        self.store.enqueue("p-1", "fp-a")
        self.store.finish("p-1", status="done")
        self.assertTrue(self.store.enqueue("p-1", "fp-b"))

    def test_failed_job_retries_with_backoff_then_gives_up(self):
        import paper_relation_review

        original = paper_relation_review.BACKOFF_SECONDS
        paper_relation_review.BACKOFF_SECONDS = (0.0, 0.0, 0.0)
        try:
            self.store.enqueue("p-1", "fp-a")
            self.assertIsNotNone(self.store.claim())
            self.store.finish("p-1", status="failed", error="boom")
            # First failures are retried; the budget is finite.
            self.assertEqual(self.store.counts()["pending_jobs"], 1)
            for _ in range(5):
                job = self.store.claim()
                if job is None:
                    break
                self.store.finish("p-1", status="failed", error="boom")
            self.assertEqual(self.store.counts()["failed_jobs"], 1)
            self.assertEqual(self.store.counts()["pending_jobs"], 0)
        finally:
            paper_relation_review.BACKOFF_SECONDS = original

    def test_candidates_are_deduplicated_by_pair_and_type(self):
        candidate = {
            "source_paper_id": "p-1", "target_paper_id": "p-2",
            "relation_type": "explicit_citation", "note": "n",
        }
        self.store.save_candidates([dict(candidate)])
        saved = self.store.save_candidates([dict(candidate)])
        self.assertEqual(len(self.store.list_candidates()), 1)
        self.assertEqual(saved[0]["candidate_id"], self.store.list_candidates()[0]["candidate_id"])

    def test_accepting_a_candidate_removes_it_from_pending(self):
        saved = self.store.save_candidates([{
            "source_paper_id": "p-1", "target_paper_id": "p-2",
            "relation_type": "explicit_citation",
        }])
        candidate_id = saved[0]["candidate_id"]
        self.store.resolve_candidate(candidate_id, "accepted")
        self.assertEqual(self.store.list_candidates(status="pending"), [])
        self.assertEqual(len(self.store.list_candidates(status="accepted")), 1)

    def test_dropping_a_paper_removes_its_candidates(self):
        self.store.save_candidates([{
            "source_paper_id": "p-1", "target_paper_id": "p-2",
            "relation_type": "explicit_citation",
        }])
        self.store.drop_paper("p-1")
        self.assertEqual(self.store.counts()["candidates"], 0)

    def test_corrupt_state_file_falls_back_to_empty(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.counts()["candidates"], 0)


class TestReviewerIncrementality(unittest.TestCase):
    """Only pairs involving the new paper may be examined."""

    PAPERS = [
        {"paper_id": "p-a", "title": "First Paper About Scheduling Mechanisms"},
        {"paper_id": "p-b", "title": "Second Paper About Shaping Mechanisms"},
        {"paper_id": "p-c", "title": "Third Paper About Polling Mechanisms"},
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(Path(self._tmp.name) / "runtime")
        self.paths.ensure_initialized()
        self.store = PaperRelationReviewStore(self.paths)
        self.seen: list[tuple[str, int]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _detector(self, paper, chunks, others):
        self.seen.append((str(paper.get("paper_id")), len(list(others))))
        return detect_citation_candidates(paper, chunks, others)

    def test_review_examines_only_the_new_paper(self):
        chunks = {
            "p-a": [_chunk("p-a", 1, "Third Paper About Polling Mechanisms is cited here.")],
            "p-b": [_chunk("p-b", 1, "nothing relevant")],
            "p-c": [_chunk("p-c", 1, "nothing relevant")],
        }
        reviewer = PaperRelationReviewer(
            self.store, _FakeStore(self.PAPERS, chunks), detector=self._detector,
        )
        try:
            reviewer.enqueue("p-a", "fp-a")
        finally:
            # wait=True lets the single background worker finish draining.
            reviewer.shutdown(wait=True)
        self.assertEqual([paper for paper, _ in self.seen], ["p-a"])
        # Two other papers, so the detector saw exactly two comparison targets.
        self.assertEqual(self.seen[0][1], 2)
        self.assertEqual(self.store.counts()["pending_candidates"], 1)

    def test_review_is_disabled_when_not_enabled(self):
        reviewer = PaperRelationReviewer(
            self.store, _FakeStore(self.PAPERS, {}), enabled=False,
        )
        try:
            self.assertFalse(reviewer.enqueue("p-a", "fp-a"))
        finally:
            reviewer.shutdown()
        self.assertEqual(self.seen, [])

    def test_paper_deleted_before_review_cancels_the_job(self):
        reviewer = PaperRelationReviewer(
            self.store, _FakeStore([], {}), detector=self._detector,
        )
        try:
            reviewer.enqueue("p-gone", "fp-a")
        finally:
            reviewer.shutdown(wait=True)
        # The paper no longer exists, so detection must not run at all.
        self.assertEqual(self.seen, [])


class TestRelationMigration(unittest.TestCase):
    """Re-indexing mints a new paper_id; relations must survive it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(Path(self._tmp.name) / "runtime")
        self.paths.ensure_initialized()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relations_move_to_the_new_paper_id(self):
        from paper_relations import PaperRelationStore

        store = PaperRelationStore(self.paths)
        title = "A Theory of Traffic Regulators for Deterministic Networks"
        store.upsert(
            source_paper={"paper_id": "p-old", "title": title},
            target_paper={"paper_id": "p-other", "title": "Wi-Fi 6: A First Look"},
            relation_type="explicit_citation",
            note="cited",
            evidence=[{"paper_id": "p-old", "page": 3, "note": "anchor"}],
        )
        moved = store.remap_paper_id(title, "p-new", title)
        self.assertEqual(moved, 1)
        relations = store.list("p-new")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["source_paper_id"], "p-new")
        self.assertEqual(store.list("p-old"), [])

    def test_unrelated_relations_are_untouched(self):
        from paper_relations import PaperRelationStore

        store = PaperRelationStore(self.paths)
        store.upsert(
            source_paper={"paper_id": "p-x", "title": "Alpha Scheduling Paper"},
            target_paper={"paper_id": "p-y", "title": "Beta Shaping Paper"},
            relation_type="method_similar",
            note="similar",
            evidence=[{"paper_id": "p-x", "page": 2, "note": "anchor"}],
        )
        self.assertEqual(store.remap_paper_id("Unrelated Title", "p-z"), 0)
        self.assertEqual(len(store.list("p-x")), 1)


if __name__ == "__main__":
    unittest.main()

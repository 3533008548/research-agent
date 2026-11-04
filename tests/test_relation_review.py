"""Automatic relation review: candidates, incrementality, jobs and migration."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.detected = threading.Event()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _detector(self, paper, chunks, others):
        self.seen.append((str(paper.get("paper_id")), len(list(others))))
        self.detected.set()
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
            self.assertTrue(self.detected.wait(1.0))
        finally:
            # Shutdown waits for the current detection, leaving queued work
            # recoverable rather than implicitly draining it during exit.
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
        finished = threading.Event()
        original_finish = self.store.finish

        def finish(*args, **kwargs):
            original_finish(*args, **kwargs)
            finished.set()

        reviewer = PaperRelationReviewer(
            self.store, _FakeStore([], {}), detector=self._detector,
        )
        with patch.object(self.store, "finish", side_effect=finish):
            try:
                reviewer.enqueue("p-gone", "fp-a")
                self.assertTrue(finished.wait(1.0))
            finally:
                reviewer.shutdown(wait=True)
        # The paper no longer exists, so detection must not run at all.
        self.assertEqual(self.seen, [])
        self.assertEqual(self.store._read()["jobs"][0]["status"], "cancelled")


class TestReviewerScheduling(unittest.TestCase):
    """The background reviewer must make progress without a new paper arriving."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(Path(self._tmp.name) / "runtime")
        self.store = PaperRelationReviewStore(self.paths)
        self.paper_store = _FakeStore(
            TestReviewerIncrementality.PAPERS,
            {"p-a": [_chunk("p-a", 0, "A paper with parseable content.")]},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_retry_runs_when_due_without_another_enqueue(self):
        completed = threading.Event()
        calls: list[float] = []

        def detector(*_args):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise RuntimeError("temporary detector failure")
            completed.set()
            return []

        with patch("paper_relation_review.BACKOFF_SECONDS", (0.04,) * 3), patch(
            "paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0,
        ):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(completed.wait(1.0), "the due retry was never woken")
            finally:
                reviewer.shutdown(wait=True)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1] - calls[0], 0.03)
        self.assertEqual(self.store.counts()["pending_jobs"], 0, self.store._read()["jobs"])

    def test_restart_resumes_persisted_pending_without_an_enqueue(self):
        self.store.enqueue("p-a", "fp-a")
        completed = threading.Event()

        def detector(*_args):
            completed.set()
            return []

        reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
        try:
            self.assertTrue(completed.wait(1.0), "startup left a pending job stranded")
        finally:
            reviewer.shutdown(wait=True)
        self.assertEqual(self.store.counts()["pending_jobs"], 0)

    def test_restart_recovers_interrupted_running_job(self):
        self.store.enqueue("p-a", "fp-a")
        self.assertIsNotNone(self.store.claim())
        completed = threading.Event()

        def detector(*_args):
            completed.set()
            return []

        reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
        try:
            self.assertTrue(completed.wait(1.0), "startup did not recover a running job")
        finally:
            reviewer.shutdown(wait=True)
        job = self.store._read()["jobs"][0]
        self.assertEqual(job["status"], "no_candidates")
        self.assertEqual(job["attempts"], 2, "the interrupted attempt also consumes budget")

    def test_restart_keeps_a_future_retry_deadline(self):
        with patch("paper_relation_review.BACKOFF_SECONDS", (0.12,) * 3):
            self.store.enqueue("p-a", "fp-a")
            self.store.claim()
            self.store.finish("p-a", status="failed", error="retry after restart")
        completed = threading.Event()

        def detector(*_args):
            completed.set()
            return []

        reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
        try:
            self.assertFalse(completed.wait(0.03), "startup ignored the persisted retry deadline")
            self.assertTrue(completed.wait(1.0), "the persisted future retry never ran")
        finally:
            reviewer.shutdown(wait=True)

    def test_retries_stop_at_the_attempt_limit(self):
        exhausted = threading.Event()
        calls: list[int] = []

        def detector(*_args):
            calls.append(1)
            if len(calls) == 3:
                exhausted.set()
            raise RuntimeError("persistent detector failure")

        with patch("paper_relation_review.BACKOFF_SECONDS", (0.01,) * 3), patch(
            "paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0,
        ):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(exhausted.wait(1.0), "retry scheduling never exhausted its budget")
            finally:
                reviewer.shutdown(wait=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.counts()["failed_jobs"], 1)
        self.assertEqual(self.store.counts()["pending_jobs"], 0)

    def test_shutdown_does_not_wait_for_a_retry_deadline(self):
        failed = threading.Event()
        original_finish = self.store.finish

        def finish(*args, **kwargs):
            original_finish(*args, **kwargs)
            failed.set()

        def detector(*_args):
            raise RuntimeError("retry much later")

        with patch("paper_relation_review.BACKOFF_SECONDS", (60.0,) * 3), patch(
            "paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0,
        ), patch.object(self.store, "finish", side_effect=finish):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(failed.wait(1.0))
                started = time.monotonic()
                reviewer.shutdown(wait=True)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(self.store.counts()["pending_jobs"], 1)
                self.assertFalse(reviewer.enqueue("p-b", "fp-b"))
            finally:
                reviewer.shutdown(wait=True)

    def test_shutdown_interrupts_rate_limit_wait(self):
        detected = threading.Event()

        def detector(*_args):
            detected.set()
            return []

        with patch("paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 60.0):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(detected.wait(1.0))
                started = time.monotonic()
                reviewer.shutdown(wait=True)
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                reviewer.shutdown(wait=True)

    def test_interrupted_attempts_cannot_reset_the_retry_budget(self):
        self.store.enqueue("p-a", "fp-a")
        self.store.claim()
        payload = self.store._read()
        payload["jobs"][0]["attempts"] = 2
        self.store._write(payload)
        detected = threading.Event()

        def detector(*_args):
            detected.set()
            return []

        reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
        reviewer.shutdown(wait=True)
        self.assertFalse(detected.is_set())
        self.assertEqual(self.store.counts()["failed_jobs"], 1)
        self.assertEqual(self.store.counts()["pending_jobs"], 0)

    def test_new_paper_does_not_wait_behind_an_unrelated_retry(self):
        failed = threading.Event()
        completed = threading.Event()
        original_finish = self.store.finish
        self.paper_store._chunks["p-b"] = [_chunk("p-b", 0, "Another parsed paper.")]

        def finish(*args, **kwargs):
            original_finish(*args, **kwargs)
            if kwargs.get("status") == "failed":
                failed.set()

        def detector(paper, *_args):
            if paper["paper_id"] == "p-a":
                raise RuntimeError("temporarily unavailable")
            completed.set()
            return []

        with patch("paper_relation_review.BACKOFF_SECONDS", (60.0,) * 3), patch(
            "paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0,
        ), patch.object(self.store, "finish", side_effect=finish):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(failed.wait(1.0))
                reviewer.enqueue("p-b", "fp-b")
                self.assertTrue(completed.wait(1.0))
            finally:
                reviewer.shutdown(wait=True)
        self.assertEqual(self.store.counts()["pending_jobs"], 1)

    def test_second_reviewer_does_not_recover_an_active_job_and_takes_over_on_exit(self):
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        detections: list[tuple[str, str]] = []
        self.paper_store._chunks["p-b"] = [_chunk("p-b", 0, "Another parsed paper.")]

        def owner_detector(paper, *_args):
            detections.append(("owner", paper["paper_id"]))
            started.set()
            release.wait(2.0)
            return []

        def standby_detector(paper, *_args):
            detections.append(("standby", paper["paper_id"]))
            completed.set()
            return []

        with patch("paper_relation_review.QUEUE_POLL_SECONDS", 0.02), patch(
            "paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0,
        ):
            owner = PaperRelationReviewer(self.store, self.paper_store, detector=owner_detector)
            standby = None
            try:
                owner.enqueue("p-a", "fp-a")
                self.assertTrue(started.wait(1.0))
                standby = PaperRelationReviewer(
                    PaperRelationReviewStore(self.paths), self.paper_store, detector=standby_detector,
                )
                self.assertTrue(standby.enqueue("p-b", "fp-b"))
                owner.shutdown(wait=False)
                self.assertFalse(completed.wait(0.06), "another process stole an active review")
                job = next(item for item in self.store._read()["jobs"] if item["paper_id"] == "p-a")
                self.assertEqual(job["status"], "running")
                self.assertEqual(job["attempts"], 0)
                release.set()
                self.assertTrue(completed.wait(1.0), "standby did not take over after owner shutdown")
            finally:
                release.set()
                owner.shutdown(wait=True)
                if standby is not None:
                    standby.shutdown(wait=True)
        self.assertEqual(detections, [("owner", "p-a"), ("standby", "p-b")])

    def test_idle_owner_detects_jobs_enqueued_by_another_instance(self):
        completed = threading.Event()
        detections: list[str] = []

        def owner_detector(*_args):
            detections.append("owner")
            completed.set()
            return []

        def standby_detector(*_args):
            detections.append("standby")
            return []

        with patch("paper_relation_review.QUEUE_POLL_SECONDS", 0.02):
            owner = PaperRelationReviewer(self.store, self.paper_store, detector=owner_detector)
            standby = PaperRelationReviewer(
                PaperRelationReviewStore(self.paths), self.paper_store, detector=standby_detector,
            )
            try:
                standby.enqueue("p-a", "fp-a")
                self.assertTrue(completed.wait(1.0), "idle consumer never noticed the other producer")
            finally:
                owner.shutdown(wait=True)
                standby.shutdown(wait=True)
        self.assertEqual(detections, ["owner"])

    def test_separate_producers_do_not_overwrite_each_others_jobs(self):
        other = PaperRelationReviewStore(self.paths)
        ready = threading.Barrier(2)
        errors: list[Exception] = []

        def produce(store, prefix):
            try:
                ready.wait(timeout=1.0)
                for index in range(10):
                    store.enqueue(f"{prefix}-{index}", "fp")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=produce, args=(self.store, "a")),
            threading.Thread(target=produce, args=(other, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(self.store.counts()["pending_jobs"], 20)

    def test_other_process_cannot_reclaim_a_live_review(self):
        started = threading.Event()
        release = threading.Event()

        def detector(*_args):
            started.set()
            release.wait(5.0)
            return []

        # The child uses the real reviewer and filesystem, rather than a mocked
        # lock, so this exercises msvcrt on Windows and flock on Unix.
        child = """
import sys
from paper_relation_review import PaperRelationReviewStore, PaperRelationReviewer
from runtime_paths import RuntimePaths

class EmptyPapers:
    def list_papers(self):
        return []

store = PaperRelationReviewStore(RuntimePaths.from_root(sys.argv[1]))
reviewer = PaperRelationReviewer(store, EmptyPapers())
try:
    assert reviewer.enqueue("p-b", "fp-b")
finally:
    reviewer.shutdown(wait=True)
"""
        reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
        try:
            reviewer.enqueue("p-a", "fp-a")
            self.assertTrue(started.wait(1.0))
            result = subprocess.run(
                [sys.executable, "-c", child, str(self.paths.root)],
                capture_output=True, text=True, timeout=4.0,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            job = next(item for item in self.store._read()["jobs"] if item["paper_id"] == "p-a")
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["attempts"], 0)
            self.assertEqual(self.store.counts()["pending_jobs"], 1)
        finally:
            reviewer.shutdown(wait=False)
            release.set()
            reviewer.shutdown(wait=True)

    def test_circuit_breaker_leaves_remaining_jobs_queued(self):
        self.store.enqueue("p-a", "fp-a")
        payload = self.store._read()
        payload["jobs"].extend(
            {"paper_id": f"p-failed-{index}", "status": "failed", "attempts": 3}
            for index in range(3)
        )
        self.store._write(payload)
        checked = threading.Event()
        original_failures = self.store.consecutive_failures
        calls: list[int] = []

        def failures():
            result = original_failures()
            checked.set()
            return result

        def detector(*_args):
            calls.append(1)
            return []

        with patch.object(self.store, "consecutive_failures", side_effect=failures):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                self.assertTrue(checked.wait(1.0))
            finally:
                reviewer.shutdown(wait=True)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.counts()["pending_jobs"], 1)

    def test_shutdown_during_claim_returns_the_unstarted_job(self):
        claimed = threading.Event()
        release = threading.Event()
        detected = threading.Event()
        original_claim = self.store.claim

        def claim():
            job = original_claim()
            if job is not None:
                claimed.set()
                release.wait(2.0)
            return job

        def detector(*_args):
            detected.set()
            return []

        with patch.object(self.store, "claim", side_effect=claim):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(claimed.wait(1.0))
                reviewer.shutdown(wait=False)
            finally:
                release.set()
                reviewer.shutdown(wait=True)
        self.assertFalse(detected.is_set())
        job = self.store._read()["jobs"][0]
        self.assertEqual((job["status"], job["attempts"]), ("pending", 0))

    def test_shutdown_during_chunk_loading_does_not_start_detection(self):
        loading = threading.Event()
        release = threading.Event()
        detected = threading.Event()
        original_chunks = self.paper_store.get_paper_chunks

        def chunks(paper_id):
            loading.set()
            release.wait(2.0)
            return original_chunks(paper_id)

        def detector(*_args):
            detected.set()
            return []

        with patch.object(self.paper_store, "get_paper_chunks", side_effect=chunks):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                reviewer.enqueue("p-a", "fp-a")
                self.assertTrue(loading.wait(1.0))
                reviewer.shutdown(wait=False)
            finally:
                release.set()
                reviewer.shutdown(wait=True)
        self.assertFalse(detected.is_set())
        job = self.store._read()["jobs"][0]
        self.assertEqual((job["status"], job["attempts"]), ("pending", 0))

    def test_circuit_allows_one_probe_after_cooldown_and_success_resumes_queue(self):
        self.paper_store._chunks["p-b"] = [_chunk("p-b", 0, "Another parsed paper.")]
        self.store.enqueue("p-a", "fp-a")
        self.store.enqueue("p-b", "fp-b")
        payload = self.store._read()
        payload["jobs"].extend(
            {"paper_id": f"p-failed-{index}", "status": "failed", "attempts": 3}
            for index in range(3)
        )
        self.store._write(payload)
        completed = threading.Event()
        calls: list[tuple[str, float]] = []

        def detector(paper, *_args):
            calls.append((paper["paper_id"], time.monotonic()))
            if paper["paper_id"] == "p-b":
                completed.set()
            return []

        with patch("paper_relation_review.CIRCUIT_COOLDOWN_SECONDS", 0.06), patch(
            "paper_relation_review.QUEUE_POLL_SECONDS", 0.01,
        ), patch("paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0):
            started = time.monotonic()
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                self.assertFalse(completed.wait(0.02))
                self.assertTrue(completed.wait(1.0), "circuit never admitted a successful probe")
            finally:
                reviewer.shutdown(wait=True)
        self.assertEqual([paper_id for paper_id, _ in calls], ["p-a", "p-b"])
        self.assertGreaterEqual(calls[0][1] - started, 0.05)
        self.assertEqual(self.store.consecutive_failures(), 0)
        self.assertEqual(self.store.counts()["pending_jobs"], 0)

    def test_failed_half_open_probes_keep_the_cooldown_and_attempt_limit(self):
        self.store.enqueue("p-a", "fp-a")
        payload = self.store._read()
        payload["jobs"].extend(
            {"paper_id": f"p-failed-{index}", "status": "failed", "attempts": 3}
            for index in range(3)
        )
        self.store._write(payload)
        exhausted = threading.Event()
        calls: list[float] = []

        def detector(*_args):
            calls.append(time.monotonic())
            if len(calls) == 3:
                exhausted.set()
            raise RuntimeError("half-open probe still fails")

        with patch("paper_relation_review.CIRCUIT_COOLDOWN_SECONDS", 0.05), patch(
            "paper_relation_review.QUEUE_POLL_SECONDS", 0.01,
        ), patch("paper_relation_review.BACKOFF_SECONDS", (0.005,) * 3), patch(
            "paper_relation_review.MIN_TASK_INTERVAL_SECONDS", 0.0,
        ):
            reviewer = PaperRelationReviewer(self.store, self.paper_store, detector=detector)
            try:
                self.assertTrue(exhausted.wait(1.0))
            finally:
                reviewer.shutdown(wait=True)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(later - earlier >= 0.04 for earlier, later in zip(calls, calls[1:])))
        self.assertEqual(self.store.counts()["pending_jobs"], 0)
        self.assertEqual(self.store.counts()["failed_jobs"], 4)


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

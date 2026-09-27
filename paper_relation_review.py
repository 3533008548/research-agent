"""Automatic, incremental paper-relation review.

Relations used to be created one at a time by hand.  This module runs the
first pass automatically after a paper is indexed, but it never writes a
confirmed relation: it emits *candidates* that a user promotes through the
existing ``save_paper_relation`` gate.

Two invariants keep it safe:

*   **Incremental.** A relation describes a pair of papers, not their position
    in the library, so indexing a new paper cannot invalidate any existing
    ``A <-> B`` relation.  Only pairs involving the new paper are examined.
*   **Evidence-bound.** A candidate without a real page/fragment anchor on the
    source side is not a candidate.  Detection deliberately starts with
    ``explicit_citation``, which is verifiable by matching text, rather than a
    model's opinion about semantic similarity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from paper_artifacts import title_match_key, title_match_score
from runtime_paths import RuntimePaths

SCHEMA_VERSION = 1
CANDIDATE_ID = re.compile(r"rel-cand-[a-f0-9]{12}\Z")

# Detection and scheduling bounds.
CITATION_MIN_TITLE_CHARS = 18
CANDIDATE_LIMIT_PER_PAPER = 12
EVIDENCE_LIMIT = 4
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 4.0, 15.0)
CIRCUIT_FAILURE_THRESHOLD = 3
MIN_TASK_INTERVAL_SECONDS = 0.2

_JOB_ACTIVE = ("pending", "running")
_CANDIDATE_ACTIVE = ("pending",)


def paper_fingerprint(*, title: str, chunk_count: int, source_file: str) -> str:
    """Identify a paper by content rather than by its mutable ``paper_id``.

    Re-indexing mints a new ``paper_id``; the fingerprint survives it so
    relations can be carried over instead of being dropped as dangling.
    """
    raw = "\n".join(
        (
            " ".join(str(title or "").split()).casefold(),
            str(int(chunk_count or 0)),
            " ".join(str(source_file or "").split()).casefold(),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise(value: object) -> str:
    return " ".join(str(value or "").split())


class PaperRelationReviewStore:
    """Persist review jobs and relation candidates in one runtime file."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.paths.ensure_initialized()
        self.path = paths.paper_relation_review_file
        self._lock = threading.RLock()

    # -- persistence ----------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "updated_at": "", "jobs": [], "candidates": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Review state is derived data: losing it must not break retrieval.
            return {"schema_version": SCHEMA_VERSION, "updated_at": "", "jobs": [], "candidates": []}
        if not isinstance(value, dict):
            return {"schema_version": SCHEMA_VERSION, "updated_at": "", "jobs": [], "candidates": []}
        value.setdefault("jobs", [])
        value.setdefault("candidates", [])
        return value

    def _write(self, payload: dict[str, Any]) -> None:
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    # -- jobs -----------------------------------------------------------
    def enqueue(self, paper_id: str, fingerprint: str) -> bool:
        """Queue a review for one paper unless an equivalent job already waits."""
        paper_id = str(paper_id or "").strip()
        if not paper_id:
            return False
        with self._lock:
            payload = self._read()
            jobs = payload["jobs"]
            for job in jobs:
                if job.get("paper_id") == paper_id and job.get("status") in _JOB_ACTIVE:
                    # Same paper, same content: re-queuing would only repeat work.
                    if job.get("fingerprint") == fingerprint:
                        return False
                    job["fingerprint"] = fingerprint
                    job["status"] = "pending"
                    job["attempts"] = 0
                    job["next_attempt_at"] = ""
                    job["updated_at"] = _now()
                    self._write(payload)
                    return True
            jobs.append({
                "paper_id": paper_id,
                "fingerprint": fingerprint,
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": "",
                "error": "",
                "enqueued_at": _now(),
                "updated_at": _now(),
            })
            payload["jobs"] = jobs[-200:]
            self._write(payload)
            return True

    def claim(self) -> dict[str, Any] | None:
        """Take the next due job, marking it running under the lock."""
        with self._lock:
            payload = self._read()
            now = _now()
            for job in payload["jobs"]:
                if job.get("status") != "pending":
                    continue
                due = str(job.get("next_attempt_at") or "")
                if due and due > now:
                    continue
                job["status"] = "running"
                job["updated_at"] = now
                self._write(payload)
                return dict(job)
        return None

    def finish(
        self, paper_id: str, *, status: str, error: str = "", candidates: int = 0,
    ) -> None:
        with self._lock:
            payload = self._read()
            for job in payload["jobs"]:
                if job.get("paper_id") != paper_id or job.get("status") != "running":
                    continue
                job["status"] = status
                job["error"] = error
                job["candidates"] = int(candidates)
                job["attempts"] = int(job.get("attempts") or 0) + 1
                job["updated_at"] = _now()
                if status == "failed" and job["attempts"] < MAX_ATTEMPTS:
                    delay = BACKOFF_SECONDS[min(job["attempts"], len(BACKOFF_SECONDS) - 1)]
                    job["status"] = "pending"
                    job["next_attempt_at"] = datetime.fromtimestamp(
                        time.time() + delay, timezone.utc,
                    ).isoformat()
                self._write(payload)
                return

    def cancel(self, paper_id: str) -> None:
        with self._lock:
            payload = self._read()
            for job in payload["jobs"]:
                if job.get("paper_id") == paper_id and job.get("status") in _JOB_ACTIVE:
                    job["status"] = "cancelled"
                    job["updated_at"] = _now()
            self._write(payload)

    def consecutive_failures(self) -> int:
        with self._lock:
            jobs = self._read()["jobs"]
        count = 0
        for job in reversed(jobs):
            if job.get("status") == "failed":
                count += 1
                continue
            break
        return count

    # -- candidates -----------------------------------------------------
    def save_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge new candidates, keeping one live record per (paper pair, type)."""
        if not candidates:
            return []
        with self._lock:
            payload = self._read()
            existing = payload["candidates"]
            saved: list[dict[str, Any]] = []
            for candidate in candidates:
                # A candidate with no status would be invisible to every query
                # that filters by it, so it gets the pending default here.
                candidate.setdefault("status", "pending")
                candidate.setdefault("created_at", _now())
                candidate["updated_at"] = _now()
                key = (
                    candidate.get("source_paper_id"),
                    candidate.get("target_paper_id"),
                    candidate.get("relation_type"),
                )
                index = next(
                    (
                        i for i, item in enumerate(existing)
                        if (item.get("source_paper_id"), item.get("target_paper_id"), item.get("relation_type")) == key
                        and item.get("status") in _CANDIDATE_ACTIVE
                    ),
                    None,
                )
                if index is None:
                    candidate["candidate_id"] = f"rel-cand-{secrets.token_hex(6)}"
                    existing.append(candidate)
                    saved.append(dict(candidate))
                else:
                    # Refresh evidence but keep the original id and creation time.
                    merged = dict(existing[index])
                    merged.update({k: v for k, v in candidate.items() if k != "candidate_id"})
                    merged["created_at"] = existing[index].get("created_at") or _now()
                    existing[index] = merged
                    saved.append(dict(merged))
            payload["candidates"] = existing[-500:]
            self._write(payload)
            return saved

    def list_candidates(
        self, paper_id: str = "", status: str = "pending",
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = self._read()["candidates"]
        paper_id = str(paper_id or "").strip()
        rows = [
            dict(item) for item in items
            if (not status or item.get("status") == status)
            and (
                not paper_id
                or paper_id in {
                    str(item.get("source_paper_id") or ""),
                    str(item.get("target_paper_id") or ""),
                }
            )
        ]
        return sorted(rows, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def resolve_candidate(self, candidate_id: str, status: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._read()
            for item in payload["candidates"]:
                if item.get("candidate_id") == candidate_id:
                    item["status"] = status
                    item["resolved_at"] = _now()
                    self._write(payload)
                    return dict(item)
        return None

    def drop_paper(self, paper_id: str) -> None:
        """Drop candidates and jobs for a deleted paper."""
        with self._lock:
            payload = self._read()
            payload["jobs"] = [
                job for job in payload["jobs"] if str(job.get("paper_id") or "") != paper_id
            ]
            payload["candidates"] = [
                item for item in payload["candidates"]
                if paper_id not in {
                    str(item.get("source_paper_id") or ""),
                    str(item.get("target_paper_id") or ""),
                }
            ]
            self._write(payload)

    def counts(self) -> dict[str, int]:
        with self._lock:
            payload = self._read()
        jobs = payload["jobs"]
        candidates = payload["candidates"]
        return {
            "jobs": len(jobs),
            "pending_jobs": sum(1 for job in jobs if job.get("status") == "pending"),
            "failed_jobs": sum(1 for job in jobs if job.get("status") == "failed"),
            "candidates": len(candidates),
            "pending_candidates": sum(
                1 for item in candidates if item.get("status") == "pending"
            ),
        }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _citation_evidence(
    chunk: dict[str, Any], title: str,
) -> dict[str, Any] | None:
    """Build one anchored evidence item when a chunk really cites ``title``."""
    text = _normalise(chunk.get("text"))
    if not text:
        return None
    key = title_match_key(title)
    if len(key) < CITATION_MIN_TITLE_CHARS:
        # Short titles match too loosely inside running text.
        return None
    haystack = title_match_key(text)
    position = haystack.find(key)
    if position < 0:
        return None
    page = chunk.get("page")
    return {
        "paper_id": str(chunk.get("paper_id") or ""),
        "title": str(chunk.get("title") or ""),
        "page": int(page) if isinstance(page, int) else None,
        "chunk_index": int(chunk["chunk_index"]) if chunk.get("chunk_index") is not None else None,
        "note": f"原文出现被引用论文标题：{text[:160]}",
    }


def detect_citation_candidates(
    paper: dict[str, Any],
    chunks: list[dict[str, Any]],
    others: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find explicit citations of already-indexed papers in one new paper.

    Matching a title inside the text is verifiable; a model's judgement about
    similarity is not.  Only matched text becomes a candidate, and only with
    the page it appeared on.
    """
    source_id = str(paper.get("paper_id") or "")
    source_title = _normalise(paper.get("title"))
    if not source_id:
        return []
    candidates: list[dict[str, Any]] = []
    for other in others:
        target_id = str(other.get("paper_id") or "")
        target_title = _normalise(other.get("title"))
        if not target_id or target_id == source_id or not target_title:
            continue
        if title_match_score(source_title, target_title) >= 0.70:
            # The same paper indexed twice, or a near-duplicate entry.
            continue
        evidence: list[dict[str, Any]] = []
        for chunk in chunks:
            found = _citation_evidence(chunk, target_title)
            if found is None:
                continue
            found["paper_id"] = source_id
            found["title"] = source_title
            if all(
                item.get("chunk_index") != found.get("chunk_index") for item in evidence
            ):
                evidence.append(found)
            if len(evidence) >= EVIDENCE_LIMIT:
                break
        if not evidence:
            continue
        candidates.append({
            "source_paper_id": source_id,
            "source_title": source_title,
            "target_paper_id": target_id,
            "target_title": target_title,
            "relation_type": "explicit_citation",
            "note": f"《{source_title}》正文中出现了《{target_title}》的标题，疑似明确引用，待人工确认。",
            "evidence": evidence,
            "confidence": 0.9,
            "detector": "citation_title_match",
            "status": "pending",
            "fingerprint": str(paper.get("fingerprint") or ""),
            "created_at": _now(),
            "updated_at": _now(),
        })
        if len(candidates) >= CANDIDATE_LIMIT_PER_PAPER:
            break
    return candidates


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class PaperRelationReviewer:
    """Run queued relation reviews on one background worker."""

    def __init__(
        self,
        store: PaperRelationReviewStore,
        paper_store: Any,
        *,
        detector: Callable[..., list[dict[str, Any]]] = detect_citation_candidates,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.paper_store = paper_store
        self.detector = detector
        self.enabled = enabled
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relation-review")
        self._running = False
        self._last_run = 0.0
        self._lock = threading.RLock()

    def enqueue(self, paper_id: str, fingerprint: str) -> bool:
        """Queue a review and wake the worker.  Never blocks the caller."""
        if not self.enabled:
            return False
        if not self.store.enqueue(paper_id, fingerprint):
            return False
        self._pump()
        return True

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _pump(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._executor.submit(self._drain)

    def _drain(self) -> None:
        try:
            while True:
                if self.store.consecutive_failures() >= CIRCUIT_FAILURE_THRESHOLD:
                    # Stop hammering a detector that keeps failing; queued jobs
                    # stay so a later successful run can pick them up.
                    break
                job = self.store.claim()
                if job is None:
                    break
                self._run_job(job)
                elapsed = time.monotonic() - self._last_run
                if elapsed < MIN_TASK_INTERVAL_SECONDS:
                    time.sleep(MIN_TASK_INTERVAL_SECONDS - elapsed)
        finally:
            with self._lock:
                self._running = False

    def _run_job(self, job: dict[str, Any]) -> None:
        paper_id = str(job.get("paper_id") or "")
        self._last_run = time.monotonic()
        try:
            paper = self._find_paper(paper_id)
            if paper is None:
                self.store.finish(paper_id, status="cancelled")
                return
            chunks = list(self.paper_store.get_paper_chunks(paper_id) or [])
            others = [item for item in self.paper_store.list_papers() if str(item.get("paper_id")) != paper_id]
            if not others or not chunks:
                # Nothing to compare against: an empty result, not a failure.
                self.store.finish(paper_id, status="no_candidates", candidates=0)
                return
            candidates = self.detector(paper, chunks, others)
            saved = self.store.save_candidates(candidates)
            self.store.finish(
                paper_id,
                status="done" if saved else "no_candidates",
                candidates=len(saved),
            )
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            self.store.finish(paper_id, status="failed", error=f"{type(exc).__name__}: {exc}")

    def _find_paper(self, paper_id: str) -> dict[str, Any] | None:
        for item in self.paper_store.list_papers() or []:
            if str(item.get("paper_id") or "") == paper_id:
                return item
        return None

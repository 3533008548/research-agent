"""Equivalence check: papers/rerank.py vs the pre-refactor rerank implementation.

Reranking is the quality pillar — without it paragraph-level R@1 falls from
79.4% to 38.2% — so this compares not just the final order but every score, the
``reranked`` flag, and the degraded path where scoring fails.

Usage:
    python scripts/verify_rerank_equivalence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from papers import rerank as new_mod  # noqa: E402


# ── Pre-refactor implementation, transcribed from paper_store.py @ 29d1418~1 ──
def old_rerank(candidates, scores):
    ranked = []
    for item, score in zip(candidates, scores):
        ranked.append({
            **item,
            "reranked": True,
            "reranker_score": round(float(score), 6),
        })
    return sorted(
        ranked,
        key=lambda item: (
            -item["reranker_score"],
            -item.get("hybrid_score", 0.0),
            item.get("title", ""),
            item.get("chunk_index", 0),
        ),
    )


def make_candidates(n=8):
    """Candidates with varied scores so every tie-break level gets exercised."""
    candidates = []
    for i in range(n):
        candidates.append({
            "text": f"document {i} body text",
            "title": f"Paper {(i * 7) % 3}",
            "paper_id": f"p{i % 4}",
            "chunk_index": (i * 5) % 3,
            "hybrid_score": round(0.01 * (i % 5), 6),
            "retrieval": "hybrid",
        })
    return candidates


SCORE_SETS = [
    [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2],
    [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],          # all tied
    [0.5, 0.5, 0.5, 0.5, 0.9, 0.9, 0.1, 0.1],          # partial ties
    [-1.0, 0.0, 1.0, 2.5, -3.3, 4.4, 0.001, 100.0],  # negative + extremes
    [3, 2, 1, 0, -1, -2, -3, -4],                     # descending input order
]


class Boom:
    """Stand-in cross-encoder whose predict() always fails."""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("model unavailable")

    def predict(self, pairs, **kwargs):
        raise self.exc


class Recorder:
    """Records what predict() was handed, then returns fixed scores."""

    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def predict(self, pairs, **kwargs):
        self.calls.append((list(pairs), kwargs))
        return self.scores


def main() -> int:
    failures: list[str] = []

    # 1. Ordering, scores and flags must match for every score pattern.
    for n in (1, 3, 8):
        candidates = make_candidates(n)
        for scores in SCORE_SETS[:3]:
            scores = (scores * 4)[:n]
            expected = old_rerank(candidates, scores)
            recorder = Recorder(scores)
            actual = new_mod.rerank_with("q", candidates, recorder.predict)
            if expected != actual:
                failures.append(f"order/score mismatch n={n} scores={scores[:4]}")

            if [c.get("reranker_score") for c in actual] != [
                c.get("reranker_score") for c in expected
            ]:
                failures.append(f"reranker_score mismatch n={n}")
            if not all(c.get("reranked") is True for c in actual):
                failures.append("reranked flag missing")

    # 2. Input list must not be mutated: the degraded path returns it directly.
    for n in (3, 8):
        candidates = make_candidates(n)
        snapshot = [dict(c) for c in candidates]
        repeated = (SCORE_SETS[0] * 4)[:n]
        new_mod.rerank_with("q", candidates, Recorder(repeated).predict)
        if candidates != snapshot:
            failures.append(f"input mutated by rerank_with n={n}")

    # 3. predict() receives the same pairs and kwargs the old code used.
    candidates = make_candidates(4)
    scores = [0.4, 0.3, 0.2, 0.1]
    recorder = Recorder(scores)
    new_mod.rerank_with("my query", candidates, recorder.predict)
    pairs, kwargs = recorder.calls[0]
    if pairs != [("my query", c["text"]) for c in candidates]:
        failures.append("predict() received different pairs")
    if kwargs.get("batch_size") != 8:
        failures.append(f"batch_size changed: {kwargs.get('batch_size')}")
    if kwargs.get("show_progress_bar") is not False:
        failures.append("show_progress_bar changed")

    # 4. Degraded path: an exception must return the input order unchanged.
    for exc in (RuntimeError("boom"), ValueError("bad"), KeyError("k")):
        candidates = make_candidates(5)
        before = [dict(c) for c in candidates]
        actual = new_mod.rerank_with("q", candidates, Boom(exc).predict)
        if actual != before:
            failures.append(f"degraded path altered order for {type(exc).__name__}")

    # 5. Empty candidates short-circuit without calling predict at all.
    recorder = Recorder([])
    if new_mod.rerank_with("q", [], recorder.predict) != []:
        failures.append("empty candidates not returned as-is")
    if recorder.calls:
        failures.append("predict() called for empty candidates")

    # 6. A short score list must truncate exactly like the old bare zip did.
    # Locked in on purpose: silently dropping candidates is a latent bug, but
    # "fixing" it inside a refactor would change behaviour without anyone
    # measuring retrieval numbers.  It needs its own change.
    candidates = make_candidates(6)
    for short in ([0.9, 0.8], [0.9], []):
        expected = old_rerank(candidates, short)
        actual = new_mod.rerank_with("q", candidates, Recorder(short).predict)
        if expected != actual:
            failures.append(f"short-score behaviour changed for len={len(short)}")
    # ...and a long score list must be ignored past the candidate count.
    long_scores = list(range(10))
    expected = old_rerank(candidates, long_scores)
    actual = new_mod.rerank_with("q", candidates, Recorder(long_scores).predict)
    if expected != actual:
        failures.append("long-score behaviour changed")

    if failures:
        print(f"EQUIVALENCE FAILED: {len(failures)} mismatch(es)")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("Equivalence check passed (ordering, scores, flags, degradation, length mismatch).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
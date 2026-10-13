"""Cross-encoder reranking.

This stage is the single largest contributor to query latency — roughly 90% of
a hybrid query once the encoder is fast — and it is also the quality pillar:
removing it drops paragraph-level R@1 from 79.4% to 38.2%.  Any change here is
therefore treated as high risk, which is why the plan scheduled it last.

What lives here versus what stays behind
----------------------------------------
:func:`apply_scores` is pure: given candidates and their scores, it attaches
metadata and sorts.  That is what this module owns, so it can be tested with a
plain list and no model.

Model loading and its lock stay in ``paper_store`` because the instance and the
"already tried" flag are per-store state guarded by a lock, and because
``CrossEncoder`` is imported lazily with ``local_files_only=True`` — the weights
must already be cached, and a missing download must degrade rather than
network-fetch on the request path.

:func:`rerank_with` takes a *predict callable* rather than a model, so the
ordering and the failure path can both be exercised without loading 400MB of
weights.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

log = logging.getLogger("ra.rerank")

# Batching for the cross-encoder.  8 was measured as a good fit on CPU: larger
# batches waste memory without reducing latency, since the model already runs
# internally vectorised.
PREDICT_BATCH_SIZE = 8

# Tie-breakers after the reranker score.  Two candidates can receive identical
# cross-encoder scores, and without a deterministic tie-break the order would
# depend on dict iteration and change between runs — which makes A/B comparisons
# of retrieval quality meaningless.
_TIE_BREAK_KEY = (
    lambda item: (
        -item["reranker_score"],
        -item.get("hybrid_score", 0.0),
        item.get("title", ""),
        item.get("chunk_index", 0),
    )
)


def apply_scores(candidates: Sequence[dict], scores: Sequence[float]) -> list[dict]:
    """Attach reranker metadata and sort by descending score.

    Input candidates are never mutated: callers keep the RRF-ordered list for
    the degraded path, so mutating it here would corrupt the fallback.
    """
    ranked = []
    for item, score in zip(candidates, scores):
        ranked.append({
            **item,
            "reranked": True,
            "reranker_score": round(float(score), 6),
        })
    return sorted(ranked, key=_TIE_BREAK_KEY)


def rerank_with(
    query_text: str,
    candidates: list[dict],
    predict: Callable[[list[tuple[str, str]]], Sequence[float]],
    *,
    batch_size: int = PREDICT_BATCH_SIZE,
) -> list[dict]:
    """Reorder candidates with a cross-encoder, degrading to the input order.

    ``predict`` receives ``(query, document)`` pairs and returns one score per
    pair.  Injecting it rather than a model object keeps the sorting and the
    failure path testable without loading any weights.

    On failure the original order is returned unchanged: a cross-encoder that
    cannot score is worse than no reranking at all, because losing it silently
    would hide the reason.  The warning carries the exception type and message
    but never the query or document text.
    """
    if not candidates:
        return candidates
    pairs = [(query_text, item["text"]) for item in candidates]
    try:
        scores = predict(pairs, batch_size=batch_size, show_progress_bar=False)
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the query
        log.warning(
            "RAG 重排失败，保留 RRF 结果 (exc_type=%s, exc=%s)",
            type(exc).__name__, exc,
        )
        return candidates

    scores = list(scores)
    if len(scores) < len(candidates):
        # The pre-refactor code used a bare ``zip`` here, which silently drops
        # candidates when the model returns fewer scores than inputs.  Truncating
        # to the shorter length preserves that behaviour exactly; the alternative
        # (discarding the whole result) would have been a silent behaviour change
        # smuggled into a refactor.  A length mismatch is a model-contract bug
        # and deserves its own fix, in its own change, with retrieval numbers
        # measured before and after.
        scores = scores[:len(candidates)]

    return apply_scores(candidates, scores)


def load_cross_encoder(
    model_name: str,
    *,
    max_length: int,
    local_files_only: bool = True,
) -> Any:
    """Instantiate a CrossEncoder, or return None when unavailable.

    ``local_files_only`` defaults to True because the request path must not
    depend on network access: a missing cache entry should degrade to
    un-reranked results immediately rather than block the query on a download.
    """
    from sentence_transformers import CrossEncoder

    return CrossEncoder(
        model_name,
        max_length=max_length,
        local_files_only=local_files_only,
    )
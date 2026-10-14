"""Reciprocal-rank fusion and one-hop relation expansion.

The last piece of the retrieval pipeline, and the one that decides how the
sources are combined rather than how any one of them works:

    semantic ─┐
               ├─▶ RRF ─▶ relation expansion ─▶ rerank
    lexical ───┘

Why RRF at all
--------------
The two retrieval scores are not comparable.  Chroma returns distances (lower
is better, unbounded), lexical returns BM25 sums (higher is better, bounded by
term frequency).  Normalising them would mean inventing a scale and tuning it.
RRF sidesteps the problem by merging *ranks* only: ``1 / (K + rank)``.  It needs
no calibration, no persisted state and no extra dependency, and it degrades
gracefully when one source returns nothing.

Relation expansion is deliberately subordinate
----------------------------------------------
A related paper may contribute a chunk only when that chunk *also* matches the
query, and a related paper never gets a normal RRF vote — it receives
``RELATION_EXPANSION_RRF_FACTOR / (K + rank)``, a fraction of a vote, so a
disconnected paper's "least bad" chunk cannot displace a direct hit.  The
semantic side additionally cuts off at the first-pass candidate boundary.

Everything here is a pure function over injected callables, so the whole
pipeline can be exercised without a vector store.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional, Sequence

log = logging.getLogger("ra.hybrid")

# RRF damping constant.  Larger values flatten the contribution of the top
# ranks; 60 is the value from the original RRF paper and is what the retrieval
# numbers in this project were tuned against.
RRF_K = 60

# Over-fetch factors.  Dense retrieval multiplies top_k because the reranker
# needs more than the caller's slice; hybrid multiplies further because RRF
# needs candidates both sources agree on.
SEMANTIC_CANDIDATE_FACTOR = 3
HYBRID_CANDIDATE_FACTOR = 4

# Ceiling on candidates handed to the reranker.  Measured on the local corpus:
# 20 candidates cost measurably more rerank time than 12 with identical
# paragraph-level R@1, because the tool layer caps top_k at 5 anyway.
HYBRID_CANDIDATE_LIMIT = 20

# Section keywords that mark a "methods/experiments" heading, given a small
# rank-neutral bonus so a result section does not bury a method section.
SECTION_PRIORITY_TERMS = ("method", "experiment", "evaluation", "result", "proposed")
SECTION_PRIORITY_BONUS = 0.05

# Relation traversal bounds.  Seeds are taken from first-pass results only, and
# expansion touches at most this many additional papers, so a dense relation
# graph cannot turn one query into a library-wide walk.
RELATION_EXPANSION_SEED_LIMIT = 3
RELATION_EXPANSION_MAX_PAPERS = 8

# A relation hit earns a fraction of a normal RRF vote.  This is the mechanism
# that keeps relation expansion from dominating a directly retrieved answer.
RELATION_EXPANSION_RRF_FACTOR = 0.5

# Sentinel rank for candidates that a source never scored, used by the
# tie-break so "absent" always sorts last rather than as rank 0.
_UNRANKED = 99_999


def result_key(result: dict) -> tuple[str, str, int]:
    """Stable identity for merging candidates from different sources.

    Falls back to the title when a chunk has no paper id, so a candidate is
    never silently dropped because one source populated the field and another
    did not.
    """
    return (
        str(result.get("paper_id") or result.get("title") or ""),
        str(result.get("title") or ""),
        int(result.get("chunk_index") or 0),
    )


def sort_hybrid_results(results: Sequence[dict]) -> list[dict]:
    """Order fused results deterministically.

    Every level of the key is a tie-break for the one above it.  Without the
    title and chunk index at the end, two equally scored chunks from the same
    paper could swap places between runs, which makes any A/B comparison of
    retrieval quality unmeasurable.
    """
    return sorted(
        results,
        key=lambda item: (
            -float(item.get("hybrid_score", 0.0)),
            min(item.get("semantic_rank", _UNRANKED), item.get("keyword_rank", _UNRANKED)),
            item.get("title", ""),
            item.get("chunk_index", 0),
        ),
    )


def candidate_count(
    top_k: int,
    *,
    reranker_limit: int = 0,
    hard_limit: int = HYBRID_CANDIDATE_LIMIT,
) -> int:
    """How many candidates to pull from each source.

    Never below what the reranker will re-rank, or reranking would be fed a
    truncated list and silently lose the candidates it exists to judge.
    """
    return min(
        hard_limit,
        max(top_k * HYBRID_CANDIDATE_FACTOR, reranker_limit),
    )


def reciprocal_rank_fusion(
    sources: Sequence[tuple[str, Sequence[dict]]],
) -> dict[tuple[str, str, int], dict]:
    """Merge ranked candidate lists by reciprocal rank.

    Returns a mapping keyed by :func:`result_key`.  Each contributing source
    records its own ``<source>_rank`` so downstream tie-breaks and the UI can
    show why a chunk was selected.
    """
    fused: dict[tuple[str, str, int], dict] = {}
    for source, candidates in sources:
        for rank, candidate in enumerate(candidates, start=1):
            key = result_key(candidate)
            item = fused.setdefault(key, dict(candidate))
            item[f"{source}_rank"] = rank
            item["hybrid_score"] = round(
                float(item.get("hybrid_score", 0.0)) + 1.0 / (RRF_K + rank), 6,
            )
    return fused


def relation_seed_ids(ranked: Sequence[dict], limit: int = RELATION_EXPANSION_SEED_LIMIT) -> list[str]:
    """Use only already relevant first-pass papers as one-hop relation seeds."""
    seeds: list[str] = []
    for item in ranked:
        paper_id = str(item.get("paper_id") or "").strip()
        if paper_id and paper_id not in seeds:
            seeds.append(paper_id)
        if len(seeds) >= limit:
            break
    return seeds


def attach_relation_hint(item: dict, hint: dict[str, Any]) -> None:
    """Expose an auditable retrieval hint without changing paper content.

    Mutates ``item`` in place: it attaches to results that already exist, and
    copying each one here would make the caller's view diverge from the dict it
    is about to return.
    """
    item["relation_boost"] = round(float(hint.get("boost") or 0.0), 6)
    item["relation_ids"] = list(hint.get("relation_ids") or [])
    item["relation_types"] = list(hint.get("relation_types") or [])
    item["relation_seed_paper_ids"] = list(hint.get("seed_paper_ids") or [])
    item["relation_seed_titles"] = list(hint.get("seed_titles") or [])


def expand_relation_candidates(
    query_text: str,
    fused: dict[tuple[str, str, int], dict],
    ranked: Sequence[dict],
    *,
    candidate_k: int,
    section: Optional[str],
    relation_store: Any,
    query_semantic: Callable[..., list[dict]],
    query_lexical: Callable[..., list[dict]],
    seed_limit: int = RELATION_EXPANSION_SEED_LIMIT,
    max_papers: int = RELATION_EXPANSION_MAX_PAPERS,
) -> None:
    """Add only one-hop, topic-matched candidates from confirmed relations.

    Mutates ``fused`` in place.  Relation traversal never replaces retrieval: a
    related paper contributes only chunks that also score in semantic or
    lexical retrieval.  The semantic cutoff is the first-pass candidate
    boundary, so a disconnected paper's merely "least bad" chunk cannot enter
    because of a relation.

    Every external call is guarded.  A missing relation store, a failing
    relation query and a failing secondary retrieval each degrade to "no
    expansion" rather than failing the user's query.
    """
    if relation_store is None or not ranked:
        return
    seeds = relation_seed_ids(ranked, seed_limit)
    if not seeds:
        return
    try:
        raw_hints = relation_store.related_paper_boosts(seeds)
    except Exception as exc:  # noqa: BLE001 - expansion is optional
        log.warning("论文关系检索已跳过 (exc_type=%s, exc=%s)", type(exc).__name__, exc)
        return
    # A seed cannot boost itself; drop those so a paper is never both the
    # anchor and the expansion.
    seed_set = set(seeds)
    hints = {
        paper_id: hint for paper_id, hint in raw_hints.items()
        if paper_id not in seed_set
    }
    if not hints:
        return
    related_ids = sorted(hints)[:max_papers]
    if not related_ids:
        return

    # Existing results receive the small, explainable tie-break signal.
    for item in fused.values():
        hint = hints.get(str(item.get("paper_id") or ""))
        if hint:
            attach_relation_hint(item, hint)
            item["hybrid_score"] = round(
                float(item.get("hybrid_score", 0.0)) + float(hint.get("boost") or 0.0), 6,
            )
            item["_relation_score_applied"] = True

    try:
        related_semantic = query_semantic(
            query_text, candidate_k, paper_ids=related_ids, section=section,
        )
        related_lexical = query_lexical(
            query_text, candidate_k, paper_ids=related_ids, section=section,
        )
    except Exception as exc:  # noqa: BLE001 - expansion is optional
        log.warning("论文关系候选扩展已跳过 (exc_type=%s, exc=%s)", type(exc).__name__, exc)
        return

    semantic_distances = [
        float(item["distance"]) for item in ranked
        if item.get("distance") is not None
    ]
    semantic_cutoff = max(semantic_distances) if semantic_distances else None

    def merge_related(source: str, candidates: Iterable[dict]) -> None:
        for rank, candidate in enumerate(candidates, start=1):
            if source == "semantic" and semantic_cutoff is not None:
                distance = candidate.get("distance")
                if distance is None or float(distance) > semantic_cutoff:
                    continue
            paper_id = str(candidate.get("paper_id") or "")
            hint = hints.get(paper_id)
            if hint is None:
                continue
            key = result_key(candidate)
            # A chunk created here reached the results only through a relation,
            # so it must stay distinguishable from a direct hit.
            is_expansion_only = key not in fused
            item = fused.setdefault(key, {
                **candidate,
                "hybrid_score": 0.0,
                "retrieval": "hybrid",
            })
            if is_expansion_only:
                item["relation_expanded"] = True
            # This is a secondary retrieval source, not an extra normal RRF
            # vote.  It cannot dominate a directly retrieved answer.
            extra_score = RELATION_EXPANSION_RRF_FACTOR / (RRF_K + rank)
            item["hybrid_score"] = round(
                float(item.get("hybrid_score", 0.0)) + extra_score, 6,
            )
            item[f"relation_{source}_rank"] = rank
            attach_relation_hint(item, hint)

    merge_related("semantic", related_semantic)
    merge_related("keyword", related_lexical)
    for item in fused.values():
        hint = hints.get(str(item.get("paper_id") or ""))
        if hint is not None and "relation_boost" in item:
            # New candidates get their one bounded relation adjustment here;
            # existing candidates were adjusted before candidate expansion.
            if "_relation_score_applied" not in item:
                item["hybrid_score"] = round(
                    float(item.get("hybrid_score", 0.0)) + float(hint.get("boost") or 0.0), 6,
                )
                item["_relation_score_applied"] = True
        # Internal marker; must not leak into results the UI or tools render.
        item.pop("_relation_score_applied", None)
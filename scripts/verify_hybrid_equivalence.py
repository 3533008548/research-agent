"""Equivalence check: papers/hybrid.py vs the pre-refactor fusion + expansion.

This is the stage that decides *which* chunks the reranker sees, so the
comparison covers the fused scores, every rank marker, the relation hints, the
internal-marker cleanup, and all three degradation paths.

Usage:
    python scripts/verify_hybrid_equivalence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from papers import hybrid as new_mod  # noqa: E402

RRF_K = 60
RELATION_EXPANSION_SEED_LIMIT = 3
RELATION_EXPANSION_MAX_PAPERS = 8
RELATION_EXPANSION_RRF_FACTOR = 0.5


# ── Pre-refactor implementation, transcribed from paper_store.py @ 29d1418~1 ──
def old_result_key(result):
    return (
        str(result.get("paper_id") or result.get("title") or ""),
        str(result.get("title") or ""),
        int(result.get("chunk_index") or 0),
    )


def old_sort(results):
    return sorted(
        results,
        key=lambda item: (
            -float(item.get("hybrid_score", 0.0)),
            min(item.get("semantic_rank", 99_999), item.get("keyword_rank", 99_999)),
            item.get("title", ""),
            item.get("chunk_index", 0),
        ),
    )


def old_seed_ids(ranked):
    seeds = []
    for item in ranked:
        paper_id = str(item.get("paper_id") or "").strip()
        if paper_id and paper_id not in seeds:
            seeds.append(paper_id)
        if len(seeds) >= RELATION_EXPANSION_SEED_LIMIT:
            break
    return seeds


def old_attach(item, hint):
    item["relation_boost"] = round(float(hint.get("boost") or 0.0), 6)
    item["relation_ids"] = list(hint.get("relation_ids") or [])
    item["relation_types"] = list(hint.get("relation_types") or [])
    item["relation_seed_paper_ids"] = list(hint.get("seed_paper_ids") or [])
    item["relation_seed_titles"] = list(hint.get("seed_titles") or [])


def old_expand(ranked, fused, relation_store, sem_fn, lex_fn, query_text,
               candidate_k, section):
    if relation_store is None or not ranked:
        return
    seeds = old_seed_ids(ranked)
    if not seeds:
        return
    try:
        hints = relation_store.related_paper_boosts(seeds)
    except Exception:
        return
    hints = {p: h for p, h in hints.items() if p not in set(seeds)}
    if not hints:
        return
    related_ids = sorted(hints)[:RELATION_EXPANSION_MAX_PAPERS]
    if not related_ids:
        return

    for item in fused.values():
        hint = hints.get(str(item.get("paper_id") or ""))
        if hint:
            old_attach(item, hint)
            item["hybrid_score"] = round(
                float(item.get("hybrid_score", 0.0)) + float(hint["boost"]), 6)
            item["_relation_score_applied"] = True

    try:
        related_semantic = sem_fn(query_text, candidate_k, paper_ids=related_ids, section=section)
        related_lexical = lex_fn(query_text, candidate_k, paper_ids=related_ids, section=section)
    except Exception:
        return

    semantic_distances = [float(i["distance"]) for i in ranked if i.get("distance") is not None]
    semantic_cutoff = max(semantic_distances) if semantic_distances else None

    def merge_related(source, candidates):
        for rank, candidate in enumerate(candidates, start=1):
            if source == "semantic" and semantic_cutoff is not None:
                d = candidate.get("distance")
                if d is None or float(d) > semantic_cutoff:
                    continue
            hint = hints.get(str(candidate.get("paper_id") or ""))
            if hint is None:
                continue
            key = old_result_key(candidate)
            expansion_only = key not in fused
            item = fused.setdefault(key, {**candidate, "hybrid_score": 0.0, "retrieval": "hybrid"})
            if expansion_only:
                item["relation_expanded"] = True
            item["hybrid_score"] = round(
                float(item.get("hybrid_score", 0.0))
                + RELATION_EXPANSION_RRF_FACTOR / (RRF_K + rank), 6)
            item[f"relation_{source}_rank"] = rank
            old_attach(item, hint)

    merge_related("semantic", related_semantic)
    merge_related("keyword", related_lexical)
    for item in fused.values():
        hint = hints.get(str(item.get("paper_id") or ""))
        if hint is not None and "relation_boost" in item:
            if "_relation_score_applied" not in item:
                item["hybrid_score"] = round(
                    float(item.get("hybrid_score", 0.0)) + float(hint["boost"]), 6)
                item["_relation_score_applied"] = True
        item.pop("_relation_score_applied", None)


# ── Fixtures ──
def cand(pid, title, idx, **extra):
    d = {"paper_id": pid, "title": title, "chunk_index": idx, "text": f"{title} {idx}"}
    d.update(extra)
    return d


SCENARIOS = []


def scenario(name, ranked, hints, sem, lex):
    SCENARIOS.append((name, ranked, hints, sem, lex))


scenario("no relation store", [cand("p1", "A", 0, distance=0.2)], {}, [], [])
scenario("empty ranked", [], {"p9": {"boost": 0.1}}, [], [])
scenario(
    "seed is also target (excluded)",
    [cand("p1", "A", 0, distance=0.2)], {"p1": {"boost": 0.5}}, [], [],
)
scenario("hints raise", [cand("p1", "A", 0, distance=0.2)], "RAISE", [], [])
scenario(
    "secondary query raises",
    [cand("p1", "A", 0, distance=0.2)], {"p2": {"boost": 0.3}}, "RAISE", [],
)
scenario(
    "expansion adds new paper",
    [cand("p1", "A", 0, distance=0.2), cand("p1", "A", 1, distance=0.4)],
    {"p2": {"boost": 0.3, "relation_ids": ["r1"], "relation_types": ["extends"]}},
    [cand("p2", "B", 0, distance=0.3)],
    [cand("p2", "B", 1, keyword_score=2.0)],
)
scenario(
    "semantic cutoff rejects",
    [cand("p1", "A", 0, distance=0.2)], {"p2": {"boost": 0.3}},
    [cand("p2", "B", 0, distance=0.99)], [],
)
scenario(
    "expansion over existing fused entry",
    [cand("p1", "A", 0, distance=0.2), cand("p1", "A", 1, distance=0.3)],
    {"p1": {"boost": 0.4}},
    [cand("p1", "A", 0, distance=0.2)], [],
)
scenario(
    "many papers exceeds max_papers",
    [cand("p1", "A", 0, distance=0.2)],
    {f"q{i}": {"boost": 0.01} for i in range(20)},
    [], [],
)
scenario(
    "equal scores tie-break",
    [cand("p1", "Same", 1, hybrid_score=0.016), cand("p1", "Same", 0, hybrid_score=0.016)],
    {}, [], [],
)


class Store:
    def __init__(self, hints):
        self.hints = hints

    def related_paper_boosts(self, seeds):
        if self.hints == "RAISE":
            raise RuntimeError("relation store down")
        return dict(self.hints)


def make_sem(spec):
    if spec == "RAISE":
        def fn(*a, **k):
            raise RuntimeError("query failed")
        return fn

    def fn(query_text, k, paper_ids=None, section=None):
        return [dict(c) for c in spec]
    return fn


def main() -> int:
    failures: list[str] = []

    for name, ranked, hints, sem, lex in SCENARIOS:
        store = None if hints is None else Store(hints)

        old_fused = {
            old_result_key(c): dict(c, hybrid_score=0.01) for c in ranked
        }
        new_fused = {
            new_mod.result_key(c): dict(c, hybrid_score=0.01) for c in ranked
        }

        old_expand(ranked, old_fused, store, make_sem(sem), make_sem(lex), "q", 10, None)
        new_mod.expand_relation_candidates(
            "q", new_fused, ranked,
            candidate_k=10, section=None,
            relation_store=store,
            query_semantic=make_sem(sem),
            query_lexical=make_sem(lex),
        )

        if old_fused != new_fused:
            failures.append(f"fused differs: {name}")
            if len(failures) <= 2:
                for k in set(old_fused) | set(new_fused):
                    if old_fused.get(k) != new_fused.get(k):
                        print(f"    key={k}\n      old={old_fused.get(k)}\n      new={new_fused.get(k)}")

    # Sorting and key helpers.
    data = [cand("p1", "B", 1, hybrid_score=0.02), cand("p1", "A", 0, hybrid_score=0.02),
            cand("p2", "C", 0, hybrid_score=0.03, semantic_rank=2)]
    if old_sort(data) != new_mod.sort_hybrid_results(data):
        failures.append("sort_hybrid_results differs")
    for c in (data[0], {"title": "X"}, {"paper_id": "p", "chunk_index": "3"}):
        if old_result_key(c) != new_mod.result_key(c):
            failures.append(f"result_key differs for {c}")

    # Seed selection.
    ranked = [cand(f"p{i}", f"T{i}", 0) for i in range(10)]
    if old_seed_ids(ranked) != new_mod.relation_seed_ids(ranked):
        failures.append("relation_seed_ids differs")

    # Internal marker must never leak.
    probe = {new_mod.result_key(cand("p1", "A", 0)): dict(cand("p1", "A", 0), hybrid_score=0.01)}
    new_mod.expand_relation_candidates(
        "q", probe, [cand("p1", "A", 0, distance=0.2)],
        candidate_k=10, section=None,
        relation_store=Store({"p1": {"boost": 0.3}}),
        query_semantic=make_sem([]), query_lexical=make_sem([]),
    )
    if any("_relation_score_applied" in v for v in probe.values()):
        failures.append("internal marker leaked into results")

    if failures:
        print(f"EQUIVALENCE FAILED: {len(failures)} mismatch(es)")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"Equivalence check passed ({len(SCENARIOS)} scenarios + sorting, keys, seeds).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
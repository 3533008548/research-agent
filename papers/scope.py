"""Resolve user-specified paper IDs and titles against the indexed library."""

from papers.artifacts import TITLE_MATCH_THRESHOLD, title_match_score


def resolve_scope(paper_store, raw) -> tuple[list[str] | None, str]:
    """Resolve an explicit paper scope, refusing to silently widen it.

    A scope the user stated must never degrade into an unscoped search: that
    silently reintroduces cross-paper pollution, which is exactly what scoping
    exists to prevent.  An unresolvable reference is therefore an error.
    """
    if raw is None:
        return None, ""
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    values = [str(item or "").strip() for item in values]
    values = [item for item in values if item]
    if not values:
        return None, ""
    papers = list(paper_store.list_papers() or [])
    if not papers:
        return None, "论文库为空，无法限定检索范围。"
    resolved: list[str] = []
    for needle in values:
        by_id = next(
            (item for item in papers if needle == str(item.get("paper_id") or "")), None,
        )
        if by_id is not None:
            resolved.append(str(by_id.get("paper_id")))
            continue
        scored = sorted(
            (
                (title_match_score(needle, str(item.get("title") or "")), index, item)
                for index, item in enumerate(papers)
            ),
            key=lambda entry: (-entry[0], entry[1]),
        )
        best_score, _, best = scored[0]
        if best is None or best_score < TITLE_MATCH_THRESHOLD:
            options = "；".join(
                f"{item.get('title')}（{item.get('paper_id')}）" for item in papers[:5]
            )
            return None, f"无法限定到论文“{needle}”。已索引论文：{options or '无'}"
        resolved.append(str(best.get("paper_id")))
    return list(dict.fromkeys(resolved)), ""

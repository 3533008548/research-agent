"""
🔍 论文查询工具 — search_papers / query_papers / list / delete
"""

import sys
from pathlib import Path
from paper_artifacts import (
    TITLE_MATCH_THRESHOLD,
    load_document_map,
    related_context,
    title_match_score,
)
from paper_relations import relation_type_label
from search_api import list_downloaded_papers, search_public_papers
from runtime_paths import get_runtime_paths


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    """Tool schemas are advisory; malformed model arguments must not abort a run."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def handle_search_papers(args: dict, **kw) -> str:
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "❌ 请提供论文检索关键词。"
    source = str(args.get("source", "all") or "all").lower()
    limit = _bounded_int(args.get("limit", 5), 5, 1, 10)
    if source not in {"all", "openalex", "arxiv"}:
        return "❌ source 仅支持 all、openalex 或 arxiv；IEEE Xplore 暂时停用。"
    return search_public_papers(query, limit=limit, source=source)


def _resolve_scope(paper_store, raw) -> tuple[list[str] | None, str]:
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


def handle_query_papers(args: dict, paper_store=None, **kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "❌ 请提供要在论文库中检索的问题。"
    top_k = _bounded_int(args.get("top_k", 3), 3, 1, 5)
    section_value = args.get("section")
    section = str(section_value).strip() if section_value is not None else None
    section = section or None
    if "paper_id_or_title" in args:
        scope, scope_error = _resolve_scope(paper_store, args.get("paper_id_or_title"))
        if scope_error:
            return f"❌ {scope_error}"
    else:
        scope = None
    results, pending = paper_store.query_with_timeout(
        query, top_k=top_k, paper_ids=scope, section=section,
    )
    if pending and results is None:
        return f"⏳ {pending}"
    if not results:
        prefix = f"⏳ {pending}\n\n" if pending else ""
        return prefix + "📭 未找到相关内容。请先阅读并索引论文（read_pdf 会自动索引）。"
    retrieval_mode = results[0].get("retrieval", "semantic")
    prefix = f"⏳ {pending}\n\n" if pending else ""
    heading = {
        "keyword": "关键词候选",
        "hybrid": "混合检索结果",
    }.get(retrieval_mode, "语义检索结果")
    lines = [f"{prefix}📚 {heading} — 「{query}」（共 {len(results)} 条）\n"]
    paths = get_runtime_paths()
    for i, r in enumerate(results, 1):
        sec = r.get("section", "未标注")
        context = ""
        locator = ""
        element_id = str(r.get("element_id") or "")
        if element_id and r.get("paper_id"):
            document_map = load_document_map(paths, str(r["paper_id"]))
            if document_map is not None:
                context, locator = related_context(document_map, element_id)
        if r.get("retrieval") == "keyword":
            metric = f"关键词分: {r['keyword_score']}"
        elif r.get("retrieval") == "hybrid":
            metric = (
                f"重排分: {r['reranker_score']}"
                if r.get("reranked") else f"混合分: {r['hybrid_score']}"
            )
        else:
            metric = f"距离: {r['distance']}"
        location = f" · {locator}" if locator else ""
        lines.append(f"  {i}. [{r['title']}{location} · {sec}章节]  {metric}\n     {r['text']}")
        relation_types = [
            relation_type_label(str(relation_type))
            for relation_type in (r.get("relation_types") or [])
        ]
        if relation_types:
            seeds = "、".join(str(item) for item in (r.get("relation_seed_titles") or [])[:3])
            if r.get("relation_expanded"):
                # This chunk entered only because a related paper pointed at it.
                # Saying so keeps it distinguishable from a direct hit.
                origin = f"，来自相关论文：{seeds}" if seeds else ""
                lines.append(
                    f"     ↳ 关系扩展候选{origin}（"
                    + "、".join(relation_types)
                    + "；非直接命中，引用时请注明来源论文）"
                )
            else:
                lines.append(
                    "     关系增强候选（仅辅助检索，不构成论文事实）："
                    + "、".join(relation_types)
                )
        if context:
            lines.append(f"     关联上下文：\n{context}")
    summary = _relation_summary(results)
    if summary:
        lines.append(f"\n{summary}")
    return "\n".join(lines)


def _relation_summary(results: list[dict]) -> str:
    """Report what relation expansion contributed, so its value is observable."""
    if not results:
        return ""
    expanded = [item for item in results if item.get("relation_expanded")]
    boosted = [
        item for item in results
        if item.get("relation_boost") and not item.get("relation_expanded")
    ]
    if not expanded and not boosted:
        return ""
    seed_ids = {
        str(paper_id)
        for item in expanded + boosted
        for paper_id in (item.get("relation_seed_paper_ids") or [])
    }
    parts = ["🔗 关系扩展："]
    details = []
    if expanded:
        details.append(f"经关系引入 {len(expanded)} 条")
    if boosted:
        details.append(f"加权已有 {len(boosted)} 条")
    parts.append("、".join(details))
    if seed_ids:
        parts.append(f"；涉及相关论文 {len(seed_ids)} 篇")
    parts.append("。扩展结果为辅助召回，不等同于原文直接命中。")
    return "".join(parts)


def handle_list_papers(args: dict, **kw) -> str:
    return list_downloaded_papers()


def handle_list_indexed(args: dict, paper_store=None, **kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    papers = paper_store.list_papers()
    if not papers:
        return "📭 尚未索引论文。read_pdf 会自动索引。"
    lines = [f"📚 已索引论文（{len(papers)} 篇, {paper_store.chunk_count} 个块）\n"]
    for i, p in enumerate(papers, 1):
        lines.append(f"  {i}. {p['title']} ({p['chunks']} chunks)")
    return "\n".join(lines)


def handle_delete_paper(args: dict, paper_store=None, **kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    pid_or_title = str(args.get("paper_id_or_title", "") or "").strip()
    if not pid_or_title:
        return "❌ 请指定论文标题或 paper_id。"
    papers = paper_store.list_papers()
    found = None
    for p in papers:
        if pid_or_title in p["paper_id"] or pid_or_title.lower() in p["title"].lower():
            found = p; break
    if not found:
        return f"❌ 未找到论文: {pid_or_title}"
    count = paper_store.delete_paper(found["paper_id"])
    # 删除对应的图片文件（标题 → stem 反推）
    deleted_imgs = 0
    try:
        stem = found["title"].replace(" ", "_")[:30]
        img_dir = get_runtime_paths().images_dir
        if img_dir.exists():
            for img in img_dir.glob(f"{stem}_*"):
                img.unlink(missing_ok=True)
                deleted_imgs += 1
    except Exception:
        pass
    extra = f", 图片 {deleted_imgs} 张" if deleted_imgs else ""
    return f"🗑️ 已删除: {found['title']} ({count} 个块{extra})"

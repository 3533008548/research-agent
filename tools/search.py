"""
🔍 论文查询工具 — search_papers / query_papers / list / delete
"""

import sys
from pathlib import Path
from search_api import search_arxiv, search_openalex, list_downloaded_papers
from runtime_paths import get_runtime_paths


def handle_search_papers(args: dict, **kw) -> str:
    query = args.get("query", "")
    source = args.get("source", "openalex")
    limit = min(args.get("limit", 5), 10)
    if source == "arxiv":
        return search_arxiv(query, max_results=limit)
    return search_openalex(query, limit=limit)


def handle_query_papers(args: dict, paper_store=None, **kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    query = args.get("query", "")
    top_k = min(args.get("top_k", 3), 5)
    section = args.get("section", None)
    results, pending = paper_store.query_with_timeout(
        query, top_k=top_k, section=section,
    )
    if pending and results is None:
        return f"⏳ {pending}"
    if not results:
        prefix = f"⏳ {pending}\n\n" if pending else ""
        return prefix + "📭 未找到相关内容。请先阅读并索引论文（read_pdf 会自动索引）。"
    is_keyword_result = results[0].get("retrieval") == "keyword"
    prefix = f"⏳ {pending}\n\n" if pending else ""
    heading = "关键词候选" if is_keyword_result else "检索结果"
    lines = [f"{prefix}📚 {heading} — 「{query}」（共 {len(results)} 条）\n"]
    for i, r in enumerate(results, 1):
        sec = r.get("section", "未标注")
        metric = (
            f"关键词分: {r['keyword_score']}"
            if is_keyword_result else f"距离: {r['distance']}"
        )
        lines.append(f"  {i}. [{r['title']} · {sec}章节]  {metric}\n     {r['text']}")
    return "\n".join(lines)


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
    pid_or_title = args.get("paper_id_or_title", "")
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

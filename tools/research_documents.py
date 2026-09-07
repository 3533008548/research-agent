"""Model-facing tools for durable research documents."""

from __future__ import annotations

from cancellation import raise_if_cancelled
from research_documents import ResearchDocumentStore
from runtime_paths import get_runtime_paths


def _store() -> ResearchDocumentStore:
    return ResearchDocumentStore(get_runtime_paths())


def _limit(value, default: int = 5) -> int:
    try:
        return max(1, min(int(value), 10))
    except (TypeError, ValueError):
        return default


def handle_save_research_document(args: dict, *, cancel_event=None, **_kw) -> str:
    """Save a self-contained document instead of placing it in memory or profile."""
    raise_if_cancelled(cancel_event, "保存研究文档前已取消")
    try:
        document = _store().save(
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            document_id=str(args.get("document_id") or ""),
        )
    except ValueError as exc:
        return f"❌ 保存研究文档失败：{exc}"
    raise_if_cancelled(cancel_event, "保存研究文档后已取消")
    action = "已更新" if document["revision"] > 1 else "已创建"
    return (
        f"✅ {action}研究档案：{document['title']}\n"
        f"文档 ID：`{document['document_id']}`（第 {document['revision']} 版）\n"
        f"Markdown：{document['markdown_path']}\n"
        f"Word：{document['docx_path']}\n"
        "该档案不会写入用户画像或会话摘要；之后请用 `search_research_documents` 按需查阅。"
    )


def handle_search_research_documents(args: dict, **_kw) -> str:
    query = str(args.get("query") or "").strip()
    documents = _store().search(query, limit=_limit(args.get("limit")))
    if not documents:
        return "📭 未找到研究档案。"
    label = f"「{query}」" if query else "最近更新"
    lines = [f"📁 研究档案 — {label}（{len(documents)} 份）"]
    for index, document in enumerate(documents, 1):
        lines.append(
            f"{index}. {document['title']}\n"
            f"   ID：`{document['document_id']}` · 第 {document['revision']} 版 · 更新：{document['updated_at']}\n"
            f"   摘要：{document.get('excerpt') or document.get('summary') or '（无摘要）'}"
        )
    lines.append("如需完整内容，请调用 `read_research_document` 并传入文档 ID。")
    return "\n".join(lines)


def handle_read_research_document(args: dict, **_kw) -> str:
    document_id = str(args.get("document_id") or "").strip()
    document = _store().read(document_id)
    if document is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    return (
        f"# {document['title']}\n\n"
        f"> 文档 ID：`{document['document_id']}` · 第 {document['revision']} 版 · 更新：{document['updated_at']}\n\n"
        f"{document['content']}\n\n"
        f"Word 导出：{document['docx_path']}"
    )

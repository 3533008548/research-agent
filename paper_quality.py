"""Deterministic quality checks for page-grounded PDF retrieval chunks.

The checks deliberately operate on the structured document map rather than on
an LLM judgement.  Their job is not to decide whether a paper is *good*; it is
to stop structurally broken extraction results from silently entering RAG.
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from typing import Any


QUALITY_VERSION = 1
DEFAULT_CHUNK_MAX_CHARS = 1_200
_CHUNK_TOLERANCE = 120
_FRAGMENT_MAX_CHARS = 24
_MEANINGFUL_CHARS = re.compile(r"[A-Za-z\u4e00-\u9fff]")


def prepare_document_map(
    document_map: dict[str, Any],
    *,
    requested_pages: int,
    max_chunk_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair safe text fragments, then return an explicit quality report.

    Only clearly orphaned text fragments are joined to an adjacent chunk on the
    same page and in the same section.  The operation preserves all element
    references, so a repaired chunk remains traceable to the original PDF.
    """
    prepared = copy.deepcopy(document_map)
    repaired_count = _merge_orphan_text_fragments(
        prepared, max_chunk_chars=max_chunk_chars + _CHUNK_TOLERANCE,
    )
    report = assess_document_map(
        prepared, requested_pages=requested_pages, max_chunk_chars=max_chunk_chars,
    )
    report["repairs"] = {"merged_orphan_text_chunks": repaired_count}
    if report["accepted"] and repaired_count:
        report["status"] = "repaired"
    prepared["quality"] = report
    return prepared, report


def assess_document_map(
    document_map: dict[str, Any],
    *,
    requested_pages: int,
    max_chunk_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> dict[str, Any]:
    """Check page coverage, traceability and retrieval-chunk integrity."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def issue(
        severity: str,
        code: str,
        message: str,
        *,
        count: int | None = None,
        pages: list[int] | None = None,
    ) -> None:
        target = errors if severity == "error" else warnings
        item: dict[str, Any] = {"severity": severity, "code": code, "message": message}
        if count is not None:
            item["count"] = count
        if pages:
            item["pages"] = pages
        target.append(item)

    total_pages = _positive_int(document_map.get("total_pages"))
    processed_pages = _non_negative_int(document_map.get("processed_pages"))
    pages = list(document_map.get("pages") or [])
    expected_pages = min(total_pages, max(1, int(requested_pages))) if total_pages else None

    if total_pages is None or processed_pages is None:
        issue("error", "missing_page_metadata", "缺少 PDF 页数元数据，无法确认解析覆盖范围。")
    elif expected_pages is not None and processed_pages != expected_pages:
        issue(
            "error",
            "page_coverage_mismatch",
            f"应处理 {expected_pages} 页，实际只得到 {processed_pages} 页。",
        )

    page_numbers = [record.get("page") for record in pages if isinstance(record, dict)]
    numeric_pages = [number for number in page_numbers if isinstance(number, int) and number > 0]
    if processed_pages and (len(numeric_pages) != processed_pages or len(set(numeric_pages)) != len(numeric_pages)):
        issue(
            "error",
            "page_records_mismatch",
            "页面记录数量或页码不完整，不能可靠建立来源定位。",
        )
    if processed_pages and numeric_pages and numeric_pages != list(range(1, processed_pages + 1)):
        issue("error", "page_sequence_invalid", "页面记录不是从第 1 页连续排列。")

    elements = [
        element
        for page in pages
        if isinstance(page, dict)
        for element in list(page.get("elements") or [])
        if isinstance(element, dict)
    ]
    source_ids = {str(element.get("id") or "") for element in elements if str(element.get("id") or "")}
    if not source_ids:
        issue("error", "no_source_elements", "未提取到可追溯的页面元素。")

    empty_pages = [
        int(page.get("page"))
        for page in pages
        if isinstance(page, dict) and isinstance(page.get("page"), int) and not list(page.get("elements") or [])
    ]
    if empty_pages:
        issue("warning", "empty_pages", "这些页面没有提取到元素，可能是扫描页或版式异常。", pages=empty_pages)

    chunks = [chunk for chunk in list(document_map.get("chunks") or []) if isinstance(chunk, dict)]
    usable_chunks = [chunk for chunk in chunks if str(chunk.get("text") or "").strip()]
    if not usable_chunks:
        issue("error", "no_retrieval_chunks", "没有生成可用于检索的文本、表格或图注块。")

    missing_sources = []
    invalid_chunk_pages = []
    oversized_chunks = []
    for chunk in usable_chunks:
        chunk_sources = [str(value or "") for value in list(chunk.get("source_element_ids") or [])]
        if not chunk_sources or any(not value or value not in source_ids for value in chunk_sources):
            missing_sources.append(str(chunk.get("id") or "未命名块"))
        page = chunk.get("page")
        if not isinstance(page, int) or page not in set(numeric_pages):
            invalid_chunk_pages.append(str(chunk.get("id") or "未命名块"))
        if str(chunk.get("kind") or "text") == "text" and len(str(chunk.get("text") or "")) > max_chunk_chars + _CHUNK_TOLERANCE:
            oversized_chunks.append(str(chunk.get("id") or "未命名块"))
    if missing_sources:
        issue("error", "untraceable_chunks", "部分检索块缺少有效的源元素引用。", count=len(missing_sources))
    if invalid_chunk_pages:
        issue("error", "chunk_page_mismatch", "部分检索块未关联到有效页面。", count=len(invalid_chunk_pages))
    if oversized_chunks:
        issue("error", "oversized_text_chunks", "部分正文块超过允许长度，可能没有按边界切分。", count=len(oversized_chunks))

    expected_text_ids = {
        str(element.get("id"))
        for element in elements
        if element.get("kind") == "text"
        and not element.get("is_heading")
        and str(element.get("text") or "").strip()
    }
    covered_ids = {
        str(source_id)
        for chunk in usable_chunks
        for source_id in list(chunk.get("source_element_ids") or [])
    }
    uncovered = expected_text_ids - covered_ids
    if uncovered:
        issue("error", "uncovered_text_elements", "部分正文元素没有进入任何检索块。", count=len(uncovered))

    fragments = [chunk for chunk in usable_chunks if _is_orphan_text_fragment(chunk)]
    if fragments:
        fragment_pages = sorted({int(chunk["page"]) for chunk in fragments if isinstance(chunk.get("page"), int)})
        severity = "error" if len(fragments) >= max(3, math.ceil(len(usable_chunks) * 0.05)) else "warning"
        issue(
            severity,
            "orphan_text_fragments",
            "发现缺乏语义内容的短正文块；它们不应单独参与检索。",
            count=len(fragments),
            pages=fragment_pages,
        )

    duplicate_count = _duplicate_text_chunk_count(usable_chunks)
    if duplicate_count:
        issue("warning", "duplicate_text_chunks", "发现重复正文块，可能是版式提取重复。", count=duplicate_count)

    text_chars = sum(
        len(str(chunk.get("text") or ""))
        for chunk in usable_chunks
        if str(chunk.get("kind") or "text") == "text"
    )
    if text_chars < 80 and not any(str(chunk.get("kind") or "") in {"table", "figure"} for chunk in usable_chunks):
        issue("error", "insufficient_text", "可检索正文不足 80 个字符。")

    return {
        "version": QUALITY_VERSION,
        "status": "failed" if errors else "passed",
        "accepted": not errors,
        "issues": errors + warnings,
        "stats": {
            "total_pages": total_pages or 0,
            "processed_pages": processed_pages or 0,
            "page_records": len(numeric_pages),
            "source_elements": len(source_ids),
            "retrieval_chunks": len(usable_chunks),
            "text_characters": text_chars,
            "orphan_text_fragments": len(fragments),
        },
    }


def verify_index_round_trip(document_map: dict[str, Any], indexed_chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Ensure the vector store received every generated retrieval chunk once."""
    expected = {
        str(chunk.get("id") or "")
        for chunk in list(document_map.get("chunks") or [])
        if str(chunk.get("text") or "").strip()
    }
    actual = {str(chunk.get("element_id") or "") for chunk in indexed_chunks}
    missing = expected - actual
    unexpected = actual - expected
    accepted = len(indexed_chunks) == len(expected) and not missing and not unexpected
    issues = []
    if not accepted:
        issues.append({
            "severity": "error",
            "code": "index_round_trip_mismatch",
            "message": "向量库中的块数量或来源 ID 与待入库文档不一致。",
            "expected_chunks": len(expected),
            "indexed_chunks": len(indexed_chunks),
            "missing": len(missing),
            "unexpected": len(unexpected),
        })
    return {
        "accepted": accepted,
        "expected_chunks": len(expected),
        "indexed_chunks": len(indexed_chunks),
        "issues": issues,
    }


def quality_failure_summary(report: dict[str, Any]) -> str:
    """Produce a short user-facing reason without dumping PDF content."""
    errors = [item for item in list(report.get("issues") or []) if item.get("severity") == "error"]
    if not errors:
        return "质量检查未通过。"
    return "；".join(str(item.get("message") or "质量检查失败") for item in errors[:2])


def _merge_orphan_text_fragments(document_map: dict[str, Any], *, max_chunk_chars: int) -> int:
    chunks = [chunk for chunk in list(document_map.get("chunks") or []) if isinstance(chunk, dict)]
    repaired = 0
    index = 0
    while index < len(chunks):
        current = chunks[index]
        if not _is_orphan_text_fragment(current):
            index += 1
            continue

        previous = chunks[index - 1] if index else None
        following = chunks[index + 1] if index + 1 < len(chunks) else None
        if _can_merge(previous, current, max_chunk_chars=max_chunk_chars):
            _append_chunk(previous, current)
            chunks.pop(index)
            repaired += 1
            continue
        if _can_merge(following, current, max_chunk_chars=max_chunk_chars):
            _prepend_chunk(following, current)
            chunks.pop(index)
            repaired += 1
            continue
        index += 1
    document_map["chunks"] = chunks
    return repaired


def _is_orphan_text_fragment(chunk: dict[str, Any]) -> bool:
    text = str(chunk.get("text") or "").strip()
    return (
        str(chunk.get("kind") or "text") == "text"
        and bool(text)
        and len(text) <= _FRAGMENT_MAX_CHARS
        and len(_MEANINGFUL_CHARS.findall(text)) < 4
    )


def _can_merge(target: dict[str, Any] | None, fragment: dict[str, Any], *, max_chunk_chars: int) -> bool:
    if not isinstance(target, dict) or str(target.get("kind") or "text") != "text":
        return False
    if target.get("page") != fragment.get("page") or target.get("section") != fragment.get("section"):
        return False
    return len(str(target.get("text") or "")) + len(str(fragment.get("text") or "")) + 2 <= max_chunk_chars


def _append_chunk(target: dict[str, Any], fragment: dict[str, Any]) -> None:
    target["text"] = f"{str(target.get('text') or '').rstrip()}\n\n{str(fragment.get('text') or '').strip()}".strip()
    _merge_references(target, fragment, target_first=True)


def _prepend_chunk(target: dict[str, Any], fragment: dict[str, Any]) -> None:
    target["text"] = f"{str(fragment.get('text') or '').strip()}\n\n{str(target.get('text') or '').lstrip()}".strip()
    _merge_references(target, fragment, target_first=False)


def _merge_references(target: dict[str, Any], fragment: dict[str, Any], *, target_first: bool) -> None:
    for field in ("source_element_ids", "related_ids"):
        current = list(target.get(field) or [])
        incoming = list(fragment.get(field) or [])
        values = current + incoming if target_first else incoming + current
        target[field] = list(dict.fromkeys(str(value) for value in values if str(value)))


def _duplicate_text_chunk_count(chunks: list[dict[str, Any]]) -> int:
    normalized = [
        " ".join(str(chunk.get("text") or "").split()).casefold()
        for chunk in chunks
        if str(chunk.get("kind") or "text") == "text" and str(chunk.get("text") or "").strip()
    ]
    return sum(count - 1 for count in Counter(normalized).values() if count > 1)


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _non_negative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

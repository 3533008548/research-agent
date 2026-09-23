"""Local, rebuildable evidence artifacts for one managed PDF.

The module deliberately keeps extraction and storage file-based.  A source map
is derived from the existing PDF reader output, so it does not introduce a new
database or a second PDF parsing stack.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pdf_reader import PaperReader
from runtime_paths import RuntimePaths


SOURCE_MAP_VERSION = 2
DOCUMENT_MAP_VERSION = 3
DEFAULT_CHUNK_MAX_CHARS = 1_200
_CHUNK_SOFT_OVERFLOW = 120
_MIN_TEXT_CHUNK_CHARS = 160
CARD_HEADINGS = (
    "研究问题与贡献",
    "方法与关键模块",
    "必要公式或技术要点",
    "实验与证据链",
    "结论与适用边界",
    "作者明确说明的局限",
    "批判性分析",
    "与已有知识的连接",
    "可验证的后续想法",
    "阅读结论",
)
# WHY / HOW / WHAT 专为跨篇横向比较设计：单篇卡片适合理解，三段式适合比较。
CARD_COMPARISON_HEADING = "跨文献比较槽（WHY / HOW / WHAT）"
CARD_COMPARISON_SLOTS = (
    ("WHY", "这篇要解决什么问题、为什么重要（动机与缺口）"),
    ("HOW", "用什么策略、方法或技术路线"),
    ("WHAT", "得到了什么结果，留下了什么没解决"),
)
_PAGE_MARKER = re.compile(r"━━━ 第\s*(\d+)\s*页 ━━━")
_TERMINAL_SENTENCE = re.compile(r"[。！？.!?][”’'\")\]）】〕》]*\s*$")
_REFERENCE_SECTION = re.compile(r"reference|bibliograph|参考文献|引用文献", re.IGNORECASE)
_CAPTION_LIKE = re.compile(r"^(?:table|tab\.?|figure|fig\.?|表|图)\s*\d+", re.IGNORECASE)
_UNTAGGED_SECTION_HEADING = re.compile(
    r"^(?:(?:[IVXLCDM]+|\d+)(?:\.\d+)*\.\s+[A-Z][A-Z0-9\s,&:/()\-]{2,}|"
    r"[A-Z]\.\s+[A-Z][A-Za-z0-9\s,&:/()\-]{2,}|"
    r"(?:第[一二三四五六七八九十百]+章|[一二三四五六七八九十]+、|\d+(?:\.\d+)+\s+).{2,80})$"
)


def _safe_artifact_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]", "_", str(value or "")).strip("._")
    return key[:120] or "paper"


def artifact_directory(paths: RuntimePaths, paper_id: str) -> Path:
    """Return a per-paper directory under the rebuildable runtime data layer."""
    directory = paths.paper_artifacts_dir / _safe_artifact_key(paper_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_document_map(
    parsed_document: dict[str, Any],
    *,
    paper_id: str,
    title: str,
    source_file: str,
) -> dict[str, Any]:
    """Turn one reader result into the persistent, page-grounded paper map."""
    # The reader result only contains JSON primitives.  Copy it to avoid a
    # caller accidentally mutating the persisted map while it is being indexed.
    document_map = json.loads(json.dumps(parsed_document, ensure_ascii=False))
    document_map.update({
        "version": DOCUMENT_MAP_VERSION,
        "paper_id": paper_id,
        "title": title,
        "source_file": source_file,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    document_map["logical_blocks"] = build_logical_text_blocks(document_map)
    document_map["chunks"] = build_retrieval_chunks(document_map)
    return document_map


def build_logical_text_blocks(document_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Join only high-confidence adjacent-page body continuations before chunking.

    Page elements remain the audit source of truth.  This intermediate layer
    prevents a page-break fragment from becoming an isolated embedding while
    keeping every contributing element ID and page range visible downstream.
    """
    _annotate_text_structure(document_map)
    runs: list[dict[str, Any]] = []
    for page_record in document_map.get("pages") or []:
        if not isinstance(page_record, dict):
            continue
        page = page_record.get("page")
        if not isinstance(page, int) or page <= 0:
            continue
        current_section = "未标注"
        members: list[dict[str, Any]] = []
        next_run_starts_after_heading = False
        run_starts_after_heading = False

        def flush() -> None:
            nonlocal members, run_starts_after_heading
            if not members:
                return
            text = "\n\n".join(str(item["text"]).strip() for item in members if str(item.get("text") or "").strip())
            if text:
                runs.append({
                    "page_start": page,
                    "page_end": page,
                    "section": current_section or "未标注",
                    "text": text,
                    "source_element_ids": [str(item.get("id") or "") for item in members if str(item.get("id") or "")],
                    "related_ids": [
                        str(related_id)
                        for item in members
                        for related_id in list(item.get("related_ids") or [])
                        if str(related_id)
                    ],
                    "starts_after_heading": run_starts_after_heading,
                })
            members = []
            run_starts_after_heading = False

        for element in list(page_record.get("elements") or []):
            if not isinstance(element, dict) or str(element.get("kind") or "text") != "text":
                continue
            if element.get("is_caption") or element.get("is_boilerplate"):
                continue
            if element.get("is_heading"):
                flush()
                current_section = str(element.get("section") or element.get("text") or "未标注")
                next_run_starts_after_heading = True
                continue
            text = str(element.get("text") or "").strip()
            if not text:
                continue
            section = str(element.get("section") or current_section or "未标注")
            if members and section != current_section:
                flush()
            current_section = section
            if not members:
                run_starts_after_heading = next_run_starts_after_heading
                next_run_starts_after_heading = False
            members.append(element)
        flush()

    merged: list[dict[str, Any]] = []
    for run in runs:
        previous = merged[-1] if merged else None
        if previous is not None and _is_cross_page_continuation(previous, run):
            _append_logical_run(previous, run)
        else:
            merged.append(run)

    blocks: list[dict[str, Any]] = []
    for index, run in enumerate(merged, start=1):
        start = int(run["page_start"])
        end = int(run["page_end"])
        pages = list(range(start, end + 1))
        blocks.append({
            "id": f"logical-p{start:03d}-{index:03d}",
            "kind": "text",
            "page_start": start,
            "page_end": end,
            "source_pages": pages,
            "section": str(run.get("section") or "未标注"),
            "text": str(run.get("text") or "").strip(),
            "source_element_ids": list(dict.fromkeys(run.get("source_element_ids") or [])),
            "related_ids": list(dict.fromkeys(run.get("related_ids") or [])),
            "continuation_confidence": "high" if end > start else "page_local",
        })
    return blocks


def _is_cross_page_continuation(previous: dict[str, Any], following: dict[str, Any]) -> bool:
    """Prefer false negatives: unrelated adjacent pages must never be stitched."""
    # This layer repairs one page break, not arbitrary multi-page chains.  A
    # three-page paragraph is rare; a broad p.3–8 citation is much harder to
    # audit and more likely to contain a missed structural boundary.
    if int(previous.get("page_start") or 0) != int(previous.get("page_end") or 0):
        return False
    if int(following.get("page_start") or 0) != int(previous.get("page_end") or 0) + 1:
        return False
    if following.get("starts_after_heading"):
        return False
    section = str(previous.get("section") or "未标注")
    if section != str(following.get("section") or "未标注") or _REFERENCE_SECTION.search(section):
        return False
    tail = str(previous.get("text") or "").rstrip()
    head = str(following.get("text") or "").lstrip()
    if not tail or not head or _CAPTION_LIKE.match(tail) or _CAPTION_LIKE.match(head):
        return False
    # A completed sentence is a stronger negative signal than proximity alone.
    return not bool(_TERMINAL_SENTENCE.search(tail))


def _annotate_text_structure(document_map: dict[str, Any]) -> None:
    """Mark captions, repeating headers and missed section headings in-place.

    The parser's element list remains intact.  These are conservative labels
    for the derived text path only, so a reader can still inspect every raw
    page element in the document map.
    """
    pages = [page for page in document_map.get("pages") or [] if isinstance(page, dict)]
    caption_ids = {
        str(element.get("caption_element_id") or "")
        for page in pages
        for element in page.get("elements") or []
        if isinstance(element, dict) and str(element.get("caption_element_id") or "")
    }
    header_pages: dict[str, set[int]] = {}
    for page in pages:
        page_number = page.get("page")
        for element in page.get("elements") or []:
            if not isinstance(element, dict) or str(element.get("kind") or "") != "text":
                continue
            if int(element.get("order") or 0) > 2 or not isinstance(page_number, int):
                continue
            normalized = _boilerplate_signature(str(element.get("text") or ""))
            if normalized:
                header_pages.setdefault(normalized, set()).add(page_number)
    repeated_headers = {
        signature for signature, seen_pages in header_pages.items() if len(seen_pages) >= 2
    }

    current_section = "未标注"
    for page in pages:
        for element in page.get("elements") or []:
            if not isinstance(element, dict) or str(element.get("kind") or "") != "text":
                continue
            element_id = str(element.get("id") or "")
            text = str(element.get("text") or "").strip()
            if element_id in caption_ids:
                element["is_caption"] = True
                continue
            if _boilerplate_signature(text) in repeated_headers:
                element["is_boilerplate"] = True
                continue
            if element.get("is_heading") or _looks_like_section_heading(text):
                element["is_heading"] = True
                current_section = text or str(element.get("section") or "未标注")
                element["section"] = current_section
                continue
            if current_section != "未标注":
                # A parser can miss a Roman-numeral heading, leaving every
                # following element with the preceding section's stale label.
                element["section"] = current_section
            elif str(element.get("section") or "").strip():
                current_section = str(element.get("section") or "未标注")


def _looks_like_section_heading(text: str) -> bool:
    value = str(text or "").strip()
    if not value or len(value) > 100 or _TERMINAL_SENTENCE.search(value):
        return False
    return bool(_UNTAGGED_SECTION_HEADING.match(value))


def _boilerplate_signature(text: str) -> str:
    value = re.sub(r"\d+", "#", str(text or "").upper())
    value = re.sub(r"\s+", " ", value).strip(" -–—")
    return value if len(value) >= 16 else ""


def _append_logical_run(target: dict[str, Any], following: dict[str, Any]) -> None:
    left = str(target.get("text") or "").rstrip()
    right = str(following.get("text") or "").lstrip()
    # Preserve English words split by a page break without inventing a space.
    if left.endswith("-") and right[:1].islower():
        target["text"] = left[:-1] + right
    else:
        target["text"] = f"{left}\n\n{right}".strip()
    target["page_end"] = int(following["page_end"])
    for field in ("source_element_ids", "related_ids"):
        target[field] = list(dict.fromkeys([
            *[str(value) for value in target.get(field) or [] if str(value)],
            *[str(value) for value in following.get(field) or [] if str(value)],
        ]))


def build_retrieval_chunks(
    document_map: dict[str, Any], *, max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Build RAG chunks from logical text runs while retaining page-level evidence."""
    chunks: list[dict[str, Any]] = []

    def add_chunk(
        page: int | None,
        page_end: int | None,
        kind: str,
        section: str,
        text: str,
        source_element_ids: list[str],
        related_ids: list[str],
        *,
        logical_block_id: str = "",
    ) -> None:
        content = str(text or "").strip()
        if not content:
            return
        start_page = page if isinstance(page, int) and page > 0 else None
        end_page = page_end if isinstance(page_end, int) and start_page is not None and page_end >= start_page else start_page
        chunk_id = f"p{start_page:03d}-c{sum(1 for item in chunks if item.get('page') == start_page) + 1:02d}" if start_page is not None else f"c{len(chunks) + 1:03d}"
        chunks.append({
            "id": chunk_id,
            "kind": kind,
            "page": start_page,
            "page_end": end_page,
            "section": section or "未标注",
            "text": content,
            "source_element_ids": list(dict.fromkeys(source_element_ids)),
            "related_ids": list(dict.fromkeys(related_ids)),
        })
        if logical_block_id:
            chunks[-1]["logical_block_id"] = logical_block_id

    logical_blocks = [
        block for block in list(document_map.get("logical_blocks") or [])
        if isinstance(block, dict) and str(block.get("text") or "").strip()
    ] or build_logical_text_blocks(document_map)
    for block in logical_blocks:
        page = block.get("page_start")
        page_end = block.get("page_end")
        for part in _split_logical_text(str(block.get("text") or ""), target_chars=max_chars):
            add_chunk(
                page if isinstance(page, int) else None,
                page_end if isinstance(page_end, int) else page if isinstance(page, int) else None,
                "text",
                str(block.get("section") or "未标注"),
                part,
                [str(value) for value in block.get("source_element_ids") or []],
                [str(value) for value in block.get("related_ids") or []],
                logical_block_id=str(block.get("id") or ""),
            )

    for page_record in document_map.get("pages") or []:
        page = page_record.get("page")
        elements = list(page_record.get("elements") or [])
        for element in elements:
            kind = str(element.get("kind") or "")
            if kind not in {"table", "figure"}:
                continue
            label = str(element.get("label") or kind)
            caption = str(element.get("caption") or "").strip()
            if kind == "table":
                content = "\n".join(part for part in (label, caption, str(element.get("text") or "")) if part)
                parts = _split_table_text(content, max_chars)
            else:
                parts = ["\n".join(part for part in (label, caption) if part)]
            for part in parts:
                add_chunk(
                    page, page, kind, str(element.get("section") or "未标注"), part,
                    [str(element.get("id") or "")],
                    [str(value) for value in element.get("related_ids") or []],
                )
    return chunks


def _split_logical_text(text: str, *, target_chars: int) -> list[str]:
    """Split a logical paragraph with a soft maximum and no orphan tail chunk."""
    remaining = str(text or "").strip()
    parts: list[str] = []
    target = max(200, int(target_chars))
    hard_max = target + min(_CHUNK_SOFT_OVERFLOW, max(20, target // 10))
    while len(remaining) > hard_max:
        cut = _preferred_text_cut(remaining, target=target, hard_max=hard_max)
        tail_length = len(remaining) - cut
        if 0 < tail_length < _MIN_TEXT_CHUNK_CHARS:
            cut = _preferred_text_cut(
                remaining,
                target=min(target, len(remaining) - _MIN_TEXT_CHUNK_CHARS),
                hard_max=len(remaining) - _MIN_TEXT_CHUNK_CHARS,
            )
        cut = max(1, min(cut, len(remaining) - 1))
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _preferred_text_cut(text: str, *, target: int, hard_max: int) -> int:
    upper = min(len(text), max(1, hard_max))
    lower = max(1, min(target // 2, upper))
    candidates = []
    for match in re.finditer(r"\n\n|[。！？!?]|\.\s|;\s|；", text[lower:upper]):
        candidates.append(lower + match.end())
    if candidates:
        return min(candidates, key=lambda position: (abs(position - target), -position))
    return min(max(1, target), upper)


def _split_table_text(text: str, max_chars: int) -> list[str]:
    """Keep the Markdown header with every table part instead of cutting rows blindly."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len("\n".join(lines)) <= max_chars:
        return ["\n".join(lines)] if lines else []
    prefix = lines[:3] if len(lines) >= 3 and lines[2].startswith("|") else lines[:1]
    rows = lines[len(prefix):]
    parts: list[str] = []
    current = list(prefix)
    for row in rows:
        if len("\n".join(current + [row])) > max_chars and len(current) > len(prefix):
            parts.append("\n".join(current))
            current = list(prefix)
        current.append(row)
    if current:
        parts.append("\n".join(current))
    return parts


def write_document_map(paths: RuntimePaths, *, paper_id: str, document_map: dict[str, Any]) -> Path:
    """Persist the one rebuildable source of truth for PDF page elements."""
    path = artifact_directory(paths, paper_id) / "document_map.json"
    _write_json(path, document_map)
    return path


def load_document_map(paths: RuntimePaths, paper_id: str) -> dict[str, Any] | None:
    """Load one local map, returning ``None`` when this paper predates the feature."""
    path = paths.paper_artifacts_dir / _safe_artifact_key(paper_id) / "document_map.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def remove_document_map(paths: RuntimePaths, paper_id: str) -> bool:
    """Remove one obsolete, rebuildable document map after a successful replacement."""
    directory = paths.paper_artifacts_dir / _safe_artifact_key(paper_id)
    path = directory / "document_map.json"
    if not path.is_file():
        return False
    path.unlink()
    try:
        directory.rmdir()
    except OSError:
        # The directory may contain a future derived artifact. Keep it intact.
        pass
    return True


def attach_image_assets(document_map: dict[str, Any], image_paths: list[str]) -> None:
    """Associate existing image exports with parsed figures without re-parsing a PDF."""
    candidates = [Path(path) for path in image_paths]
    for page_record in document_map.get("pages") or []:
        page = page_record.get("page")
        figures = [item for item in page_record.get("elements") or [] if item.get("kind") == "figure"]
        page_matches = [path for path in candidates if f"p{page}_" in path.name.casefold()]
        for index, figure in enumerate(figures):
            label_key = re.sub(r"[^a-z0-9]", "", str(figure.get("label") or "").casefold())
            match = next((path for path in candidates if label_key and label_key in re.sub(r"[^a-z0-9]", "", path.stem.casefold())), None)
            match = match or (page_matches[index] if index < len(page_matches) else None)
            if match:
                figure["asset_path"] = str(match)


def build_source_map_from_document(document_map: dict[str, Any]) -> dict[str, Any]:
    """Make evidence-card blocks from the structured page map, not rendered text."""
    blocks: list[dict[str, Any]] = []
    for page_record in document_map.get("pages") or []:
        for element in page_record.get("elements") or []:
            kind = str(element.get("kind") or "text")
            text = str(element.get("text") or "").strip()
            if kind == "figure":
                text = " ".join(part for part in (str(element.get("label") or ""), str(element.get("caption") or "")) if part).strip()
            if not text:
                continue
            blocks.append({
                "id": str(element.get("id") or ""),
                "kind": kind,
                "page": element.get("page"),
                "section": element.get("section") or "未标注",
                "label": element.get("label") or "",
                "text": text,
                "related_ids": list(element.get("related_ids") or []),
                "confidence": "high" if isinstance(element.get("page"), int) else "mixed",
            })
    return {
        "version": SOURCE_MAP_VERSION,
        "paper_id": document_map.get("paper_id", ""),
        "title": document_map.get("title", ""),
        "source_file": document_map.get("source_file", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {
            "total_pages": document_map.get("total_pages"),
            "processed_pages": document_map.get("processed_pages"),
            "locator_mode": "page-grounded" if any(item.get("page") is not None for item in blocks) else "structure-grounded",
            "extraction_confidence": "high" if blocks else "low",
        },
        "blocks": blocks,
    }


def related_context(
    document_map: dict[str, Any], chunk_id: str, *, max_chars: int = 1_400,
) -> tuple[str, str]:
    """Return a bounded, local-only explanation pack for one retrieved chunk."""
    chunks = {str(item.get("id") or ""): item for item in document_map.get("chunks") or []}
    target = chunks.get(str(chunk_id or ""))
    if target is None:
        return "", ""
    elements = {
        str(item.get("id") or ""): item
        for page in document_map.get("pages") or []
        for item in page.get("elements") or []
    }
    related_ids = list(target.get("related_ids") or [])
    for source_id in target.get("source_element_ids") or []:
        related_ids.extend(elements.get(str(source_id), {}).get("related_ids") or [])
    rows: list[str] = []
    used = 0
    for element_id in dict.fromkeys(str(value) for value in related_ids):
        element = elements.get(element_id)
        if element is None:
            continue
        kind = str(element.get("kind") or "正文")
        label = str(element.get("label") or kind)
        text = str(element.get("text") or element.get("caption") or "").strip()
        if not text:
            continue
        entry = f"[{label} · p.{element.get('page')}] {text}"
        if used + len(entry) > max_chars:
            break
        rows.append(entry)
        used += len(entry)
    primary = next(
        (elements.get(str(value)) for value in target.get("source_element_ids") or [] if elements.get(str(value))),
        None,
    )
    target_label = str((primary or {}).get("label") or target.get("kind") or "正文")
    page_start = target.get("page")
    page_end = target.get("page_end", page_start)
    page_locator = f"p.{page_start}–{page_end}" if (
        isinstance(page_start, int)
        and isinstance(page_end, int)
        and page_end > page_start
    ) else f"p.{page_start}"
    locator = f"{page_locator} · {target_label}"
    return "\n".join(rows), locator


def _split_text_blocks(text: str, page: int | None, start_order: int, section: str) -> tuple[list[dict], int, str]:
    """Split reader text into bounded, stable source blocks without inventing layout."""
    blocks: list[dict] = []
    current_section = section
    pending: list[str] = []
    order = start_order

    def flush() -> None:
        nonlocal order
        content = " ".join(" ".join(pending).split())
        pending.clear()
        if not content:
            return
        for offset in range(0, len(content), 1_200):
            part = content[offset:offset + 1_200].strip()
            if not part:
                continue
            order += 1
            blocks.append({
                "id": f"S{order:03d}",
                "kind": "text",
                "page": page,
                "section": current_section or "未标注",
                "text": part,
                "confidence": "high" if page is not None else "mixed",
            })

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "─── 右栏 ───":
            flush()
            continue
        if line.startswith("## "):
            flush()
            current_section = line[3:].strip() or "未标注"
            continue
        pending.append(line)
    flush()
    return blocks, order, current_section


def build_source_map_from_reader_text(
    reader_text: str,
    *,
    paper_id: str,
    title: str,
    source_file: str,
) -> dict[str, Any]:
    """Create a source map from the established ``PaperReader`` text contract."""
    markers = list(_PAGE_MARKER.finditer(reader_text))
    total_match = re.search(r"总页数:\s*(\d+)", reader_text)
    read_match = re.search(r"已读:\s*(\d+)", reader_text)
    total_pages = int(total_match.group(1)) if total_match else None
    processed_pages = int(read_match.group(1)) if read_match else len(markers)
    blocks: list[dict] = []
    order = 0
    section = "未标注"

    for index, marker in enumerate(markers):
        page = int(marker.group(1))
        end = markers[index + 1].start() if index + 1 < len(markers) else len(reader_text)
        page_text = reader_text[marker.end():end]
        page_blocks, order, section = _split_text_blocks(page_text, page, order, section)
        blocks.extend(page_blocks)

    if not blocks:
        # A scanned or unusual PDF may not retain the reader's page delimiters.
        # Keep any extractable text, but never pretend that it came from page 1.
        body = reader_text.split("\n\n", 1)[-1] if "\n\n" in reader_text else reader_text
        page_blocks, order, section = _split_text_blocks(body, None, order, section)
        blocks.extend(page_blocks)

    return {
        "version": SOURCE_MAP_VERSION,
        "paper_id": paper_id,
        "title": title,
        "source_file": source_file,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {
            "total_pages": total_pages,
            "processed_pages": processed_pages,
            "locator_mode": "page-grounded" if any(item["page"] is not None for item in blocks) else "structure-grounded",
            "extraction_confidence": "high" if blocks else "low",
        },
        "blocks": blocks,
    }


def create_source_map(
    pdf_path: str | Path,
    *,
    paper_id: str,
    title: str,
    max_pages: int,
) -> dict[str, Any]:
    """Read one managed PDF and map its actual page elements for evidence cards."""
    source = Path(pdf_path)
    reader = PaperReader(max_pages=max_pages, max_chars=None)
    document_map = build_document_map(
        reader.parse_document(source),
        paper_id=paper_id,
        title=title,
        source_file=source.name,
    )
    return build_source_map_from_document(document_map)


def select_evidence_blocks(source_map: dict[str, Any], *, max_blocks: int = 16, max_chars: int = 12_000) -> list[dict]:
    """Select a bounded, section-aware evidence pack for one card generation call."""
    priority_terms = (
        "abstract", "摘要", "introduction", "background", "method", "approach",
        "model", "algorithm", "experiment", "evaluation", "result", "discussion",
        "conclusion", "limitation", "限制",
    )
    blocks = list(source_map.get("blocks") or [])

    def priority(item: dict) -> tuple[int, int, str]:
        section = str(item.get("section") or "").casefold()
        matched = sum(term in section for term in priority_terms)
        page = item.get("page")
        return (-matched, int(page) if isinstance(page, int) else 99_999, str(item.get("id") or ""))

    selected: list[dict] = []
    used_chars = 0
    for block in sorted(blocks, key=priority):
        text = str(block.get("text") or "").strip()
        if not text or len(selected) >= max_blocks:
            continue
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        copied = dict(block)
        copied["text"] = text[:remaining]
        selected.append(copied)
        used_chars += len(copied["text"])
    return selected


def paper_card_prompt(source_map: dict[str, Any], evidence: list[dict]) -> str:
    """Build a bounded source-only prompt for the existing shared LLM client."""
    coverage = source_map.get("coverage") or {}
    records = [
        {
            "id": item.get("id"),
            "page": item.get("page"),
            "section": item.get("section"),
            "text": item.get("text"),
        }
        for item in evidence
    ]
    headings = "\n".join(f"## {index}. {heading}" for index, heading in enumerate(CARD_HEADINGS, 1))
    slots = "\n".join(f"- {key}：{hint}" for key, hint in CARD_COMPARISON_SLOTS)
    return (
        "你是严谨的科研论文阅读助手。仅依据下方证据块生成中文 Markdown 论文证据卡。"
        "不得补写未提供的方法、实验、数据或外部背景。每条来自论文的具体事实都必须紧随"
        "`【论文 p.N · 元素ID】`，其中 N 和元素ID必须完全来自证据块；没有可靠页码时使用"
        "`【论文 · 元素ID】`。你的推断必须以 `【分析】` 开头；无法判断时写“无法从已解析内容判断”。\n\n"
        f"论文：{source_map.get('title') or '未命名论文'}\n"
        f"来源覆盖：已处理 {coverage.get('processed_pages') or 0}/{coverage.get('total_pages') or '未知'} 页；"
        f"定位模式：{coverage.get('locator_mode') or '未知'}。\n\n"
        "请严格使用以下十个标题，内容简洁、具体；不要写引用列表或宣传性措辞：\n"
        f"{headings}\n\n"
        f"最后必须另起一段 `{CARD_COMPARISON_HEADING}`，按下面三行输出（供多篇论文横向比较；"
        "无法判断时写“无法从已解析内容判断”）：\n"
        f"{slots}\n\n"
        "证据块 JSON：\n"
        + json.dumps(records, ensure_ascii=False)
    )


def fallback_paper_card(source_map: dict[str, Any], evidence: list[dict], reason: str) -> str:
    """Persist a useful, source-bounded draft when the optional model call fails."""
    coverage = source_map.get("coverage") or {}
    lines = [
        f"# {source_map.get('title') or '论文'} — 证据卡（草稿）",
        "",
        f"> 来源覆盖：已处理 {coverage.get('processed_pages') or 0}/{coverage.get('total_pages') or '未知'} 页",
        f"> 定位模式：{coverage.get('locator_mode') or '未知'}",
        f"> 生成状态：模型未完成，已保留可追溯证据。原因：{reason}",
        "",
    ]
    for index, heading in enumerate(CARD_HEADINGS, 1):
        lines.extend([f"## {index}. {heading}", "无法从已解析内容自动完成判断。", ""])
    lines.extend([f"## {CARD_COMPARISON_HEADING}", ""])
    for key, _hint in CARD_COMPARISON_SLOTS:
        lines.append(f"- {key}：无法从已解析内容自动完成判断。")
    lines.append("")
    if evidence:
        lines.extend(["## 附：可用证据摘录", ""])
        for item in evidence[:8]:
            anchor = _anchor_for_block(item)
            lines.extend([f"- {anchor} {item.get('text', '')[:280]}", ""])
    return "\n".join(lines).strip() + "\n"


def _anchor_for_block(block: dict) -> str:
    page = block.get("page")
    block_id = str(block.get("id") or "")
    return f"【论文 p.{page} · {block_id}】" if isinstance(page, int) else f"【论文 · {block_id}】"


def audit_paper_card(card: str, source_map: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic checks for headings and source-anchor validity."""
    known = {str(item.get("id") or ""): item for item in source_map.get("blocks") or []}
    anchors = re.findall(r"【论文(?:\s+p\.(\d+))?\s+·\s+([A-Za-z0-9_-]+)】", card)
    invalid = []
    for page_text, block_id in anchors:
        block = known.get(block_id)
        if block is None:
            invalid.append(f"未知块 ID: {block_id}")
        elif page_text and block.get("page") != int(page_text):
            invalid.append(f"页码与块不一致: p.{page_text} / {block_id}")
    missing_headings = [heading for heading in CARD_HEADINGS if heading not in card]
    # WHY/HOW/WHAT 是可审计的增强项，缺失不影响卡片有效性，只作提示。
    missing_slots = [
        key for key, _hint in CARD_COMPARISON_SLOTS
        if not re.search(rf"^-\s*{key}\s*[：:]", card, re.MULTILINE)
    ]
    return {
        "version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "valid": not invalid and not missing_headings,
        "anchors_found": len(anchors),
        "invalid_anchors": invalid,
        "missing_headings": missing_headings,
        "comparison_slots_present": not missing_slots,
        "missing_comparison_slots": missing_slots,
        "evidence_blocks_available": len(known),
    }


def write_paper_artifact(
    paths: RuntimePaths,
    *,
    paper_id: str,
    source_map: dict[str, Any],
    card: str,
) -> tuple[Path, Path, dict[str, Any]]:
    """Atomically persist the source map, card and audit in one artifact folder."""
    directory = artifact_directory(paths, paper_id)
    source_map_path = directory / "source_map.json"
    card_path = directory / "paper-card.md"
    audit_path = directory / "audit.json"
    audit = audit_paper_card(card, source_map)
    _write_json(source_map_path, source_map)
    _write_text(card_path, card)
    _write_json(audit_path, audit)
    return card_path, source_map_path, audit


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

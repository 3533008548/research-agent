"""Read numbered algorithms from the source PDF, independently of RAG chunks.

Old indexes can contain only a caption, and top-k can truncate a long algorithm.
Neither is evidence of absence. This path returns complete page regions, keeps
OCR provenance, and caches only successful extraction by PDF hash and parser
version. It never replaces the user's index, paper IDs, or relation records.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import pymupdf

from paper_artifacts import artifact_directory, load_document_map
from pdf_ocr import ALGORITHM_LABEL, EXTRACTION_VERSION, attach_visual_assets, extract_visual_text
from pdf_reader import PaperReader


def algorithm_numbers(query: str) -> set[str]:
    digits = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
              "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    return {digits.get(n, n) for n in re.findall(
        r"(?:\balgorithm\s*|算法\s*)(\d+|[一二三四五六七八九十])(?![\d.一二三四五六七八九十百])",
        query, re.I,
    )}


def _number(element: dict) -> str:
    match = ALGORITHM_LABEL.match(str(element.get("label") or ""))
    return match.group(1) if match else ""


def _read_regions(paths, paper_id: str, source: Path, wanted: set[str]) -> dict:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    key = hashlib.sha256(",".join(sorted(wanted)).encode()).hexdigest()[:12]
    cache = artifact_directory(paths, paper_id) / f"algorithms-v{EXTRACTION_VERSION}-{digest[:16]}-{key}.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        elements = [e for page in cached["pages"] for e in page["elements"]]
        if (elements and cached.get("source_sha256") == digest
                and wanted <= {_number(e) for e in elements}
                and all(e.get("text") and Path(e.get("asset_path", "")).is_file() for e in elements)):
            return cached
    except (OSError, ValueError, TypeError, KeyError):
        pass

    document = {"pages": [], "fallback_pages": [], "source_sha256": digest, "diagnostics": []}
    reader = PaperReader()
    with pymupdf.open(source) as pdf:
        # The stored map may cover only the first five pages. Inspect the actual
        # source, not that old coverage limit, and report the bound explicitly.
        document["total_pages"] = len(pdf)
        document["inspected_pages"] = min(len(pdf), 100)
        for index in range(document["inspected_pages"]):
            page = pdf[index]
            native = page.get_text()
            labels = {m.group(1) for m in re.finditer(r"^(?:Algorithm|算法)\s*(\d+)\b", native, re.I | re.M)}
            requested_here = labels & wanted if wanted else labels
            if not requested_here and len(native.strip()) >= 40:
                continue
            elements, diagnostics = extract_visual_text(page, reader._extract_text_blocks(page))
            selected = []
            for number, element in enumerate(elements, 1):
                if element["kind"] == "ocr_text":
                    # A fully scanned page has no native caption. Keep the
                    # whole OCR page, explicitly marked, rather than guessing
                    # where its pseudocode ends.
                    labels_in_ocr = algorithm_numbers(element["text"])
                    if wanted:
                        labels_in_ocr &= wanted
                    if labels_in_ocr:
                        element = {**element, "label": "Algorithm " + sorted(labels_in_ocr)[0],
                                   "caption": "整页 OCR（含算法及邻近正文）"}
                if not _number(element) or (wanted and _number(element) not in wanted):
                    continue
                element.update(id=f"p{index + 1:03d}-v{number:02d}", page=index + 1)
                selected.append(element)
            document["pages"].append({"page": index + 1, "elements": selected})
            unresolved = requested_here - {_number(e) for e in selected}
            if unresolved:
                # If OCR or panel boundaries fail, the agent still receives
                # the source page image. The user need not create a screenshot.
                document["fallback_pages"].append({"page": index + 1, "elements": [{
                    "id": f"p{index + 1:03d}-algorithm-page", "page": index + 1,
                    "kind": "ocr_text", "label": ", ".join("Algorithm " + n for n in sorted(unresolved)),
                    "bbox": list(page.rect), "text": "", "extraction_method": "unresolved",
                }]})
            document["diagnostics"].extend(
                {**item, "page": index + 1} for item in diagnostics
                if item.get("status") == "failed" and (_number(item) in wanted or not item.get("label"))
            )
    attach_visual_assets(document, source, paths.images_dir)
    attach_visual_assets({"pages": document["fallback_pages"]}, source, paths.images_dir)
    found = {_number(e) for page in document["pages"] for e in page["elements"]}
    if found and wanted <= found and not document["diagnostics"] and not document["fallback_pages"]:
        # Concurrent conversations must not share a fixed .tmp filename.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent,
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, ensure_ascii=False)
            os.replace(temporary, cache)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return document


def query_algorithm_evidence(query: str, paper_ids, paths, *, section=None) -> str | None:
    wanted = algorithm_numbers(query)
    if not paper_ids or (not wanted and not re.search(r"伪代码|pseudo[\s-]?code", query, re.I)):
        return None
    lines = ["📚 算法原页取证（完整区域，不按 top_k 截断算法正文）"]
    if section:
        lines.append("按明确的算法编号定位原页；章节标签不用于排除算法框。")
    for paper_id in paper_ids:
        document_map = load_document_map(paths, paper_id)
        title = (document_map or {}).get("title") or paper_id
        lines.append(f"\n[{title} · {paper_id}]")
        try:
            if not document_map or not document_map.get("source_file"):
                raise ValueError("旧索引缺少原 PDF 路径，需先用 read_pdf 关联本地原文件")
            source = (paths.papers_dir / document_map["source_file"]).resolve()
            if not source.is_relative_to(paths.papers_dir.resolve()) or not source.is_file():
                raise ValueError("索引关联的本地 PDF 不可读；不能据此断言原文没有算法")
            result = _read_regions(paths, paper_id, source, wanted)
        except Exception as exc:
            lines.append(f"❌ 算法原页取证未完成：{type(exc).__name__}: {exc}。不得用标题或正文解释补写伪代码。")
            continue
        found = set()
        for page in result["pages"]:
            for element in page["elements"]:
                found.add(_number(element))
                method = "OCR 转写，数学符号和缩进尚需核对原图" if element["extraction_method"] == "ocr" else "原生文本"
                lines.extend([
                    f"\n{element['label']} · p.{page['page']} · {method}",
                    str(element.get("caption") or element["label"]),
                    f"原页区域（调用 describe_image，task=transcribe_algorithm 核对，无需用户截图）：{element['asset_path']}",
                    element["text"],
                ])
        for item in result["diagnostics"]:
            lines.append(f"⚠️ p.{item['page']} {item.get('label', '')}：{item.get('detail') or item['code']}")
        for page in result.get("fallback_pages", []):
            for element in page["elements"]:
                lines.append(f"原页备用图 · p.{page['page']} · {element['label']}：{element['asset_path']}。"
                             "请调用 describe_image(task=transcribe_algorithm) 直接核对原页，无需用户截图。")
        missing = wanted - found
        if missing or not found:
            lines.append("⚠️ 尚未恢复算法 " + ("、".join(sorted(missing)) if missing else "伪代码") +
                         f" 的完整原文；已检查原 PDF 前 {result['inspected_pages']}/{result['total_pages']} 页。"
                         "这不证明原文不存在算法；不得用相似段落或标题补写伪代码。")
    return "\n".join(lines)

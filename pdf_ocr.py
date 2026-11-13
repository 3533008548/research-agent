"""Local OCR for algorithm panels and pages with no usable text layer.

Never infer pseudocode from its caption. Retain the region and extraction method
so OCR output can be checked against the PDF; unresolved regions fail ingestion.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pymupdf

ALGORITHM_CAPTION = re.compile(r"^(?:Algorithm|算法)\s*\d+\b(?=\s*[:.：]|\s*$)", re.I)
ALGORITHM_LABEL = re.compile(r"^(?:Algorithm|算法)\s*(\d+)\b(?!\.\d)", re.I)
EXTRACTION_VERSION = 3
_CODE_WORD = re.compile(r"\b(?:function|input|output|return|for|while|if|end)\b", re.I)


class OCRLanguageDataError(RuntimeError):
    """The OCR engine exists but the requested language resources do not."""


def resolve_ocr_tessdata(language: str) -> str:
    languages = language.split("+")
    if not languages or any(not re.fullmatch(r"[A-Za-z0-9_]+", name) for name in languages):
        raise OCRLanguageDataError("PDF_OCR_LANGUAGE 必须是语言代码，例如 eng 或 eng+chi_sim")
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates = [os.getenv("TESSDATA_PREFIX"), root / "tessdata",
                  Path(__file__).resolve().parent / "build" / "ocr" / "tessdata"]
    try:
        candidates.append(pymupdf.get_tessdata())
    except RuntimeError:
        pass
    for candidate in candidates:
        if candidate and all((Path(candidate) / f"{name}.traineddata").is_file() for name in languages):
            return str(candidate)
    raise OCRLanguageDataError(
        f"缺少 OCR 语言数据（{language}）。桌面端请安装包含 OCR 资源的新版客户端；"
        "源码运行请执行 python scripts/fetch_ocr_data.py，或设置 TESSDATA_PREFIX。"
    )


def _ocr_failure(exc: Exception) -> dict:
    return {"code": "ocr_language_data_missing" if isinstance(exc, OCRLanguageDataError) else "ocr_failed",
            "status": "failed", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}


def ocr_region(page, bbox: list[float]) -> str:
    """Render explicitly: partial OCR in older MuPDF misses vector glyphs."""
    language = os.getenv("PDF_OCR_LANGUAGE", "eng")
    tessdata = resolve_ocr_tessdata(language)
    rect = pymupdf.Rect(bbox) & page.rect
    # Bound raster allocation even for oversized poster pages.
    dpi = min(300, max(36, int(72 * (12_000_000 / max(rect.get_area(), 1)) ** 0.5)))
    pix = page.get_pixmap(clip=rect, dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    data = pix.pdfocr_tobytes(
        language=language,
        tessdata=tessdata,
    )
    with pymupdf.open(stream=data, filetype="pdf") as result:
        # sort=True can insert hundreds of padding spaces around ruled lines.
        return "\n".join(re.sub(r"[ \t]{4,}", "   ", line).rstrip()
                         for line in result[0].get_text(sort=True).splitlines()).strip()


def attach_visual_assets(document: dict, pdf_path, output_dir) -> list[str]:
    """Keep renderings for exact-symbol verification through describe_image."""
    from pathlib import Path
    import hashlib

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    # Content addressing prevents a replacement PDF from reusing a stale crop.
    digest = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()[:16]
    assets = []
    with pymupdf.open(pdf_path) as pdf:
        for page in document.get("pages", []):
            for element in page.get("elements", []):
                if element.get("kind") not in {"algorithm", "ocr_text"}:
                    continue
                region_key = hashlib.sha256(repr(element["bbox"]).encode()).hexdigest()[:10]
                path = output / f"{digest}_v{EXTRACTION_VERSION}_{region_key}_{element['id']}.png"
                if not path.exists():
                    rect = pymupdf.Rect(element["bbox"])
                    dpi = min(180, max(36, int(72 * (12_000_000 / max(rect.get_area(), 1)) ** 0.5)))
                    pdf[page["page"] - 1].get_pixmap(clip=rect, dpi=dpi).save(path)
                element["asset_path"] = str(path)
                assets.append(str(path))
    return assets


def extract_visual_text(page, blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return dedicated algorithm elements plus explicit extraction diagnostics."""
    elements, diagnostics = [], []
    # IEEE and algorithm2e commonly omit punctuation after the number. A
    # matching label is only a candidate: nearby panel rules distinguish such
    # captions from prose like "Algorithm 4 provides the optimized path".
    # Work at line level: MuPDF can merge a short caption with a tall block
    # from the other column, giving it a page-wide bounding box.
    native_lines = [line
        for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
        for line in block.get("lines", [])
    ]
    captions = [{"text": "".join(span["text"] for span in line.get("spans", [])),
                 "bbox": list(line["bbox"])} for line in native_lines
                if ALGORITHM_LABEL.match("".join(span["text"] for span in line.get("spans", [])))]
    rules = []
    drawings = page.get_drawings() if captions else []
    if captions:
        for drawing in drawings:
            rect = drawing["rect"]
            if rect.width > 80 and rect.height < 3:
                rules.append(rect)
    for caption in captions:
        x0, y0, x1, y1 = caption["bbox"]
        label = ALGORITHM_LABEL.match(caption["text"]).group()
        aligned = sorted(
            [r for r in rules if r.x0 <= x0 + 12 and r.x1 >= x1 - 12],
            key=lambda r: r.y0,
        )
        # The first rule just below a caption is its separator; the next is
        # the bottom of the panel. A top rule of the next panel is never used
        # when this panel has a closing rule.
        below = [r for r in aligned if r.y0 >= y1 - 3]
        separator = below[0] if below and below[0].y0 <= y1 + 12 else None
        bottom = next((r for r in below if r.y0 > y1 + 12), None)
        top = next((r for r in reversed(aligned) if y0 - 16 <= r.y0 <= y0), None)
        if bottom is None or (top is None and separator is None):
            if not ALGORITHM_CAPTION.match(caption["text"]):
                continue
            # Unboxed pseudocode: do not silently accept a title with no body.
            nearby = " ".join(b["text"] for b in blocks if b is not caption
                               and abs(b["bbox"][0] - x0) < 40
                               and y1 <= b["bbox"][1] < y1 + 300)
            if len(_CODE_WORD.findall(nearby)) < 2 and len(_CODE_WORD.findall(caption["text"])) < 2:
                diagnostics.append({"code": "algorithm_region_unresolved", "label": label,
                                    "bbox": caption["bbox"], "status": "failed"})
            continue
        edge = top if top is not None else separator
        bbox = [float(edge.x0), float(y1 + 1), float(edge.x1), float(bottom.y0)]
        body = page.get_text("text", clip=pymupdf.Rect(bbox), sort=True).strip()
        method = "native"
        region = pymupdf.Rect(bbox)
        # A native Input/Output header does not prove that the remaining lines
        # are native. Detect image content and outlined glyphs outside native
        # spans, otherwise a mixed panel can pass the simple text-length gate.
        spans = [pymupdf.Rect(span["bbox"]) + (-1, -1, 1, 1)
                 for line in native_lines for span in line.get("spans", [])]
        outlined = sum(
            "f" in drawing["type"] and len(drawing["items"]) >= 6
            and 1 <= drawing["rect"].width <= 120 and 2 <= drawing["rect"].height <= 24
            and region.contains(drawing["rect"])
            and not any(rect.contains(drawing["rect"]) for rect in spans)
            for drawing in drawings
        )
        raster = any((region & pymupdf.Rect(info["bbox"])).get_area() > 1024
                     for info in page.get_image_info())
        if len(body) < 60 or len(_CODE_WORD.findall(body)) < 2 or outlined >= 8 or raster:
            method = "ocr"
            try:
                body = ocr_region(page, bbox)
            except Exception as exc:
                diagnostics.append({**_ocr_failure(exc), "label": label, "bbox": bbox})
                continue
        if len(body) < 60 or len(_CODE_WORD.findall(body)) < 2:
            diagnostics.append({"code": "algorithm_body_missing", "label": label,
                                "bbox": bbox, "status": "failed"})
            continue
        elements.append({"kind": "algorithm", "label": label, "caption": caption["text"],
                         "text": body, "bbox": bbox, "extraction_method": method,
                         "related_ids": []})
        diagnostics.append({"code": "algorithm_extracted", "label": label,
                            "bbox": bbox, "status": method})

    # Sparse text alone is not enough: require visible image/vector content.
    if not captions and sum(len(b["text"]) for b in blocks) < 40:
        if page.get_image_info() or page.get_drawings():
            bbox = list(page.rect)
            try:
                body = ocr_region(page, bbox)
                if len(body.strip()) < 40:
                    raise ValueError("No usable OCR text")
                elements.append({"kind": "ocr_text", "label": "Page OCR", "caption": "",
                                 "text": body, "bbox": bbox, "extraction_method": "ocr",
                                 "related_ids": []})
                diagnostics.append({"code": "page_ocr", "status": "ocr", "bbox": bbox})
            except Exception as exc:
                diagnostics.append({**_ocr_failure(exc), "bbox": bbox})
    return elements, diagnostics

"""Page references shared by the library reader, chat history and model requests."""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from library_store import LibraryStore
from runtime_paths import RuntimePaths


UNIT = 1_000_000  # displayed page coordinates, independent of zoom and rotation
AREA_LINK = re.compile(
    r"^📎 \[(S[12]) · [^\n]*\]\(#paper-ref/(doc_[0-9a-f]{64})/(\d{1,7})/area/"
    r"(\d{1,7},\d{1,7},\d{1,7},\d{1,7})\)$", re.MULTILINE,
)


class PaperSelection(BaseModel):
    document_id: str = Field(pattern=r"^doc_[0-9a-f]{64}$")
    page: int = Field(ge=1)
    kind: Literal["text", "area"]
    bounds: tuple[int, int, int, int]
    quote: str = Field(default="", max_length=12000)

    @model_validator(mode="after")
    def validate_region(self):
        x, y, right, bottom = self.bounds
        if not (0 <= x < right <= UNIT and 0 <= y < bottom <= UNIT):
            raise ValueError("选区超出页面或为空")
        if self.kind == "text" and not self.quote.strip():
            raise ValueError("文字引用不能为空")
        if self.kind == "area" and self.quote:
            raise ValueError("框选区域应使用原页裁图")
        return self


def referenced_message(question: str, selections: list[PaperSelection], library: LibraryStore) -> str:
    parts = [question]
    for index, selection in enumerate(selections, 1):
        doc = library.get(selection.document_id)
        if selection.page > doc["pages"]:
            raise ValueError("引用页码超出论文范围")
        name = re.sub(r"[\[\]\r\n\\]", " ", doc["name"])
        bounds = ",".join(map(str, selection.bounds))
        target = f"#paper-ref/{selection.document_id}/{selection.page}/{selection.kind}/{bounds}"
        label = f"S{index} · {name} · 第 {selection.page} 页"
        source = f"📎 [{label}]({target})"
        if selection.quote:
            source += "\n" + "\n".join("> " + line for line in selection.quote.splitlines())
        parts.append(source)
    return "\n\n".join(parts)


def visual_parts(message: str, paths: RuntimePaths | None) -> list[dict]:
    """Reopen immutable library snapshots only for crops cited by this user turn."""
    if paths is None or "#paper-ref/" not in message:
        return []
    matches = list(AREA_LINK.finditer(message))
    if not matches:
        return []
    if len(matches) > 2:
        raise ValueError("每条消息最多可引用两处选区")
    library = LibraryStore(paths)
    parts = []
    for match in matches:
        label, document_id, number, bounds = match.groups()
        selection = PaperSelection(document_id=document_id, page=int(number), kind="area",
                                   bounds=tuple(map(int, bounds.split(","))))
        image, _ = region_preview(library, selection)
        parts.extend([{"type": "text", "text": f"{label} · 第 {number} 页原始选区"},
                      {"type": "image_url", "image_url": {"url": image}}])
    return parts


def region_preview(library, selection, max_dimension=1800):
    import fitz

    with fitz.open(library.file(selection.document_id)) as pdf:
        if selection.page > len(pdf):
            raise ValueError("引用页码超出论文范围")
        page = pdf[selection.page - 1]
        width, height = page.rect.width, page.rect.height
        x, y, right, bottom = selection.bounds
        clip = fitz.Rect(x * width / UNIT, y * height / UNIT, right * width / UNIT, bottom * height / UNIT)
        scale = min(3, max_dimension / max(clip.width, clip.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        if not pix.width or not pix.height:
            raise ValueError("引用区域太小")
        encoded = base64.b64encode(pix.tobytes("png")).decode("ascii")
        text = page.get_textbox(clip * page.derotation_matrix).strip()
        return "data:image/png;base64," + encoded, text


def element_citations(paths, document_map, source_ids=None):
    """Bind parser elements to the PDF snapshot used by the reader."""
    import fitz

    source = Path(document_map.get("source_file") or "")
    root = paths.papers_dir.resolve()
    path = (root / source).resolve()
    if not document_map.get("document_id") and (
        not path.is_relative_to(root) or path.suffix.lower() != ".pdf" or not path.is_file()
    ):
        return {}, None
    try:
        library = LibraryStore(paths)
        snapshot = document_map.get("document_id")
        if not snapshot:
            snapshot = path.stem if path.parent == library.files else library.register(path, path.name)["id"]
        links = {}
        with fitz.open(library.file(snapshot)) as pdf:
            for record in document_map.get("pages", []):
                number = record["page"]
                if not 1 <= number <= len(pdf):
                    continue
                elements = [e for e in record["elements"] if e.get("bbox") and
                            (source_ids is None or e.get("id") in source_ids)]
                if not elements:
                    continue
                page = pdf[number - 1]
                legacy = source_ids is not None and not document_map.get("document_id") and path.parent != library.files
                textpage = page.get_textpage() if legacy else None
                for element in elements:
                    box = fitz.Rect(element["bbox"])
                    # pdfplumber table boxes are already in the displayed orientation.
                    if element.get("kind") != "table" and element.get("asset_kind") != "page":
                        box = box * page.rotation_matrix
                    box &= page.rect
                    if box.is_empty:
                        continue
                    if legacy:
                        # Older indexes name a mutable file. Verify text at the actual location.
                        expected = "".join((element.get("text") or "").split())
                        actual = "".join(page.get_textbox(box * page.derotation_matrix, textpage=textpage).split())
                        if not expected or expected not in actual:
                            continue
                    bounds = [round(n * UNIT / d) for n, d in zip(box, (page.rect.width, page.rect.height) * 2)]
                    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                        continue
                    coords = ",".join(map(str, bounds))
                    links[element["id"]] = f"[第{number}页](#paper-ref/{snapshot}/{number}/area/{coords})"
        return links, snapshot
    except (OSError, ValueError, RuntimeError, KeyError):
        return {}, None

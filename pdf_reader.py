"""
📄 PDF 增强阅读器 — 表格感知提取 + 双栏布局重排 + 章节自动标注

改进摘要：
  1. 表格感知 — 用 pdfplumber 提取表格并格式化为 Markdown 表格
  2. 双栏布局 — 用 PyMuPDF 的文本块坐标，先读左栏再读右栏
  3. 章节标注 — 自动识别 Introduction / Method / Experiments 等标题并标注 ##
"""

import re
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Optional

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import pymupdf
except ImportError:
    pymupdf = None


# ═══════════════════════════════════════════════════════════════
#  章节标题检测模式
# ═══════════════════════════════════════════════════════════════

# 常见学术论文章节标题（不区分大小写）
_SECTION_NAMES = [
    "abstract",
    "introduction",
    "background",
    "related work",
    "preliminaries",
    "preliminary",
    "problem (?:formulation|definition|statement)",
    "method(?:ology)?",
    "proposed (?:method|approach|framework|architecture|model|algorithm|system)",
    "approach",
    "architecture",
    "framework",
    "model",
    "network",
    "algorithm",
    "design",
    "implementation",
    "experiment(?:al)? (?:setup|results?|study|evaluation)?",
    "evaluation",
    "performance (?:evaluation|analysis|study)",
    "result(?:s)?",
    "analysis",
    "discussion",
    "ablation study",
    "comparison",
    "conclusion",
    "reference(?:s)?",
    "appendix",
    "supplementary",
]

# 编译为完整正则（匹配行首）
_SECTION_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*\s+|(?:[IVXLCDM]+)\.\s+)?"  # 可选编号: "1. " 或 "I. " 或 "2.1 "
    r"(" + "|".join(_SECTION_NAMES) + r")"           # 章节名
    r"\s*:?\s*$",                                    # 可选的冒号和结尾空白
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════
#  PaperReader 类
# ═══════════════════════════════════════════════════════════════

class PaperReader:
    """增强型 PDF 阅读器，先保留页面元素关系，再渲染为兼容的 Markdown 文本。"""

    _TABLE_CAPTION = re.compile(r"(?:table|tab\\.?|表)\\s*\\d+", re.IGNORECASE)
    _FIGURE_CAPTION = re.compile(r"(?:figure|fig\\.?|图)\\s*\\d+", re.IGNORECASE)

    def __init__(self, max_pages: int = 15, max_chars: int | None = None):
        if pymupdf is None:
            raise ImportError(
                "需要安装 PyMuPDF 才能解析 PDF：\n"
                "   pip install PyMuPDF"
            )
        self.max_pages = max_pages
        self.max_chars = max_chars

    def parse_document(self, pdf_path: str | Path, *, extract_tables: bool = True) -> dict[str, Any]:
        """Parse a PDF into page-scoped text, table and figure elements.

        This is deliberately a plain dictionary so it can be persisted as JSON
        and consumed by both the RAG indexer and evidence-card generator.

        ``extract_tables=False`` is the deterministic fallback used only when
        the table-aware pass fails its structural quality check.  It preserves
        readable text and figures instead of letting an unreliable table mask
        remove body blocks from the retrieval index.
        """
        source = Path(pdf_path)
        if not source.exists():
            raise FileNotFoundError(f"文件不存在: {source}")
        if extract_tables and pdfplumber is None:
            raise ImportError(
                "需要安装 pdfplumber 才能进行表格感知解析：\n"
                "   pip install pdfplumber"
            )

        table_context = pdfplumber.open(str(source)) if extract_tables else nullcontext(None)
        with pymupdf.open(source) as text_pdf, table_context as table_pdf:
            total_pages = len(text_pdf)
            pages_to_read = min(total_pages, self.max_pages)
            pages: list[dict[str, Any]] = []
            section = "未标注"

            for index in range(pages_to_read):
                page_number = index + 1
                text_blocks = self._extract_text_blocks(text_pdf[index])
                table_elements = (
                    self._extract_table_elements(table_pdf.pages[index], page_number, text_blocks)
                    if table_pdf is not None else []
                )
                table_boxes = [element["bbox"] for element in table_elements]
                text_blocks = [
                    block for block in text_blocks
                    if not any(self._center_in_box(block["bbox"], box) for box in table_boxes)
                ]
                text_elements, section = self._text_elements(
                    text_blocks, page_number, section,
                )
                figure_elements = self._extract_figure_elements(
                    text_pdf[index], page_number, text_blocks,
                )
                elements = text_elements + table_elements + figure_elements
                self._link_related_elements(elements)
                pages.append({"page": page_number, "elements": elements})

        self._link_table_continuations(pages)
        return {
            "version": 1,
            "source_file": source.name,
            "total_pages": total_pages,
            "processed_pages": pages_to_read,
            "pages": pages,
        }

    def read(self, pdf_path: str) -> str:
        """Keep the original text-returning interface for callers and users."""
        try:
            return self.render_document(self.parse_document(pdf_path))
        except FileNotFoundError as exc:
            return f"❌ {exc}"

    def render_document(self, document: dict[str, Any]) -> str:
        """Render page elements as readable Markdown without losing page boundaries."""
        page_texts: list[str] = []
        for page_record in document.get("pages") or []:
            page = page_record.get("page")
            lines = [f"━━━ 第 {page} 页 ━━━"]
            elements = list(page_record.get("elements") or [])
            for element in elements:
                kind = element.get("kind")
                if kind == "text":
                    text = str(element.get("text") or "").strip()
                    if text:
                        lines.append(f"## {text}" if element.get("is_heading") else text)
                elif kind == "table":
                    label = str(element.get("label") or "表格")
                    caption = str(element.get("caption") or "").strip()
                    lines.extend(["", f"📊 **{label}**" + (f"：{caption}" if caption else ""), str(element.get("text") or "")])
                elif kind == "figure":
                    label = str(element.get("label") or "图片")
                    caption = str(element.get("caption") or "").strip()
                    if caption:
                        lines.extend(["", f"🖼️ **{label}**：{caption}"])
            page_texts.append("\n".join(lines).strip())

        full_text = "\n\n".join(page_texts)
        if document.get("total_pages", 0) > document.get("processed_pages", 0):
            full_text += (
                f"\n\n...（共 {document['total_pages']} 页，已读取前 "
                f"{document['processed_pages']} 页）"
            )
        char_count = len(full_text)
        if self.max_chars is not None and char_count > self.max_chars:
            full_text = full_text[:self.max_chars] + (
                f"\n\n...（内容过长，已截断至前 {self.max_chars} 字符）"
            )
            char_count = self.max_chars
        return (
            "📄 **PDF 解析完成**\n"
            f"   文件名: {document.get('source_file', '未知')}  |  "
            f"总页数: {document.get('total_pages', 0)}  |  "
            f"已读: {document.get('processed_pages', 0)}  |  提取: {char_count} 字符\n\n"
            f"{full_text}"
        )

    # ── 页面元素提取 ──

    def _extract_text_blocks(self, page) -> list[dict[str, Any]]:
        """Return text blocks in reading order while retaining their bounding boxes."""
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        blocks = []
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            content = " ".join(text.split())
            if not content or y0 >= page_height * 0.94 or y1 - y0 > page_height * 0.5:
                continue
            blocks.append({
                "text": content,
                "bbox": [float(x0), float(y0), float(x1), float(y1)],
                "x0": float(x0), "x1": float(x1), "y": float(y0),
                "width": float(x1 - x0),
            })
        if not blocks:
            return []

        mid_x = page_width / 2
        column_width = page_width * 0.58
        is_centered = lambda block: 0.4 <= (block["x0"] + block["x1"]) / (2 * page_width) <= 0.6
        columns = [block for block in blocks if block["width"] <= column_width and not is_centered(block)]
        left = [block for block in columns if (block["x0"] + block["x1"]) / 2 < mid_x]
        right = [block for block in columns if (block["x0"] + block["x1"]) / 2 >= mid_x]
        if len(left) < 3 or len(right) < 3:
            return sorted(blocks, key=lambda block: (block["y"], block["x0"]))

        first_body_y = min(block["y"] for block in left + right)
        header = [block for block in blocks if (block["width"] > column_width or is_centered(block)) and block["y"] < first_body_y]
        trailing = [block for block in blocks if (block["width"] > column_width or is_centered(block)) and block["y"] >= first_body_y]
        return (
            sorted(header, key=lambda block: (block["y"], block["x0"]))
            + sorted(left, key=lambda block: block["y"])
            + sorted(right, key=lambda block: block["y"])
            + sorted(trailing, key=lambda block: (block["y"], block["x0"]))
        )

    def _text_elements(
        self, blocks: list[dict[str, Any]], page: int, current_section: str,
    ) -> tuple[list[dict[str, Any]], str]:
        elements = []
        for index, block in enumerate(blocks, start=1):
            text = block["text"]
            is_heading = len(text) <= 80 and bool(_SECTION_PATTERN.match(text))
            if is_heading:
                current_section = text
            elements.append({
                "id": f"p{page:03d}-t{index:02d}",
                "kind": "text",
                "page": page,
                "order": index,
                "section": current_section,
                "is_heading": is_heading,
                "text": text,
                "bbox": block["bbox"],
                "related_ids": [],
            })
        return elements, current_section

    def _extract_table_elements(
        self, page, page_number: int, text_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        elements = []
        try:
            found = page.find_tables()
        except Exception:
            return elements
        for index, table in enumerate(found, start=1):
            try:
                bbox = [float(value) for value in table.bbox]
                if not self._valid_table_bbox(bbox, page):
                    continue
                data = table.extract()
                if not data or len(data) < 2:
                    continue
                header = [str(cell or "").strip() for cell in data[0]]
                rows = [
                    [str(cell or "").strip() for cell in row]
                    for row in data[1:]
                    if any(str(cell or "").strip() for cell in row)
                ]
                cell_count = len(header) * (len(rows) + 1)
                nonempty = sum(bool(cell) for row in [header, *rows] for cell in row)
                if not rows or not cell_count or nonempty / cell_count < 0.35:
                    continue
                col_count = len(header)
                markdown = self._table_markdown(header, rows)
                caption_block = self._find_caption(text_blocks, bbox, self._TABLE_CAPTION, prefer_below=False)
                caption = caption_block["text"] if caption_block else ""
                label = self._caption_label(caption, self._TABLE_CAPTION, f"Table {index}")
                elements.append({
                    "id": f"p{page_number:03d}-b{len(elements) + 1:02d}",
                    "kind": "table", "page": page_number, "order": 10_000 + index,
                    "section": "未标注", "label": label, "caption": caption,
                    "caption_element_id": "", "text": markdown, "bbox": bbox,
                    "columns": col_count, "related_ids": [],
                })
            except Exception:
                continue
        return elements

    def _extract_figure_elements(
        self, page, page_number: int, text_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        images = []
        try:
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = (float(value) for value in bbox)
                if (x1 - x0) * (y1 - y0) >= 5_000:
                    images.append([x0, y0, x1, y1])
        except Exception:
            return []
        groups: list[list[list[float]]] = []
        for bbox in sorted(images, key=lambda item: (item[1], item[0])):
            if groups and bbox[1] - max(item[3] for item in groups[-1]) < 20:
                groups[-1].append(bbox)
            else:
                groups.append([bbox])

        elements = []
        for index, group in enumerate(groups, start=1):
            bbox = [min(item[0] for item in group), min(item[1] for item in group), max(item[2] for item in group), max(item[3] for item in group)]
            caption_block = self._find_caption(text_blocks, bbox, self._FIGURE_CAPTION, prefer_below=True)
            caption = caption_block["text"] if caption_block else ""
            label = self._caption_label(caption, self._FIGURE_CAPTION, f"Figure {index}")
            elements.append({
                "id": f"p{page_number:03d}-f{index:02d}",
                "kind": "figure", "page": page_number, "order": 20_000 + index,
                "section": "未标注", "label": label, "caption": caption,
                "caption_element_id": "", "asset_path": "", "bbox": bbox,
                "related_ids": [],
            })
        return elements

    @staticmethod
    def _table_markdown(header: list[str], rows: list[list[str]]) -> str:
        col_count = len(header)
        lines = [
            "| " + " | ".join(header + [""] * (col_count - len(header))) + " |",
            "| " + " | ".join(["---"] * col_count) + " |",
        ]
        for row in rows:
            lines.append("| " + " | ".join((row + [""] * col_count)[:col_count]) + " |")
        return "\n".join(lines)

    @staticmethod
    def _valid_table_bbox(bbox: list[float], page) -> bool:
        """Reject impossible pdfplumber boxes before a visual page becomes a fake table."""
        if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return False
        page_height = float(getattr(page, "height", 0) or 0)
        return not page_height or bbox[3] <= page_height + 2

    @staticmethod
    def _center_in_box(inner: list[float], outer: list[float]) -> bool:
        center_x = (inner[0] + inner[2]) / 2
        center_y = (inner[1] + inner[3]) / 2
        return outer[0] <= center_x <= outer[2] and outer[1] <= center_y <= outer[3]

    @staticmethod
    def _caption_label(caption: str, pattern: re.Pattern, fallback: str) -> str:
        match = pattern.search(caption or "")
        return match.group(0).replace("Fig.", "Figure").replace("fig.", "Figure") if match else fallback

    @staticmethod
    def _find_caption(
        text_blocks: list[dict[str, Any]], bbox: list[float], pattern: re.Pattern, *, prefer_below: bool,
    ) -> dict[str, Any] | None:
        candidates = []
        for block in text_blocks:
            if not pattern.search(block["text"]):
                continue
            x0, y0, x1, y1 = block["bbox"]
            before = bbox[1] - y1
            after = y0 - bbox[3]
            if prefer_below:
                direction, distance = (0, after) if after >= 0 else (1, before)
            else:
                direction, distance = (0, before) if before >= 0 else (1, after)
            if 0 <= distance <= 160:
                candidates.append((direction, distance, block))
        return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None

    @staticmethod
    def _link_related_elements(elements: list[dict[str, Any]]) -> None:
        text_elements = [element for element in elements if element.get("kind") == "text"]
        for element in elements:
            if element.get("kind") == "text":
                continue
            bbox = element.get("bbox") or [0, 0, 0, 0]
            caption = PaperReader._find_caption(
                [{"text": text["text"], "bbox": text["bbox"], "id": text["id"]} for text in text_elements],
                bbox,
                PaperReader._TABLE_CAPTION if element.get("kind") == "table" else PaperReader._FIGURE_CAPTION,
                prefer_below=element.get("kind") == "figure",
            )
            related = []
            if caption:
                element["caption_element_id"] = caption["id"]
                related.append(caption["id"])
            before = [text for text in text_elements if text["bbox"][3] <= bbox[1]]
            after = [text for text in text_elements if text["bbox"][1] >= bbox[3]]
            if before:
                related.append(max(before, key=lambda text: text["bbox"][3])["id"])
            if after:
                related.append(min(after, key=lambda text: text["bbox"][1])["id"])
            element["related_ids"] = list(dict.fromkeys(related))
            nearest = next((text for text in reversed(before)), None) or next(iter(after), None)
            if nearest:
                element["section"] = nearest.get("section", "未标注")

    @staticmethod
    def _link_table_continuations(pages: list[dict[str, Any]]) -> None:
        previous = None
        for page in pages:
            tables = [element for element in page.get("elements") or [] if element.get("kind") == "table"]
            if previous and tables:
                current = tables[0]
                if current["bbox"][1] < 60 and current.get("columns") == previous.get("columns"):
                    current["continued_from"] = previous["id"]
                    previous["continued_to"] = current["id"]
            if tables:
                previous = tables[-1]


# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
#  图片提取 — PyMuPDF 从 PDF 提取嵌入图片
# ═══════════════════════════════════════════════════════════════

def extract_images(
    pdf_path: str,
    max_pages: int = 15,
    output_dir: str | Path | None = None,
) -> list[str]:
    """
    图注感知图片提取：
      1. 找图注文字（Figure N: / Fig. N:）作为锚点
      2. 同图注下方的图片合并为一个 figure 分组
      3. 渲染合并区域的外接矩形为一张 PNG（保留子图关系）
      4. 无图注时按位置相邻（gap < 20px）分组回退
      5. 过滤面积 < 5000 px² 的小图（图标/logo）
    """
    try:
        import pymupdf
    except ImportError:
        print("      ⚠ PyMuPDF 未安装，跳过图片提取。pip install PyMuPDF", file=sys.stderr)
        return []

    from pathlib import Path
    import re as _re

    if output_dir is None:
        from runtime_paths import get_runtime_paths
        output_dir = get_runtime_paths().images_dir
    img_dir = Path(output_dir)
    img_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(pdf_path).stem[:30]

    caption_pat = _re.compile(r'(?:Figure|Fig\.?|图)\s*(\d+)')

    # ── 缓存：已有该论文图片则跳过 ──
    existing = list(img_dir.glob(f"{stem}_*.png")) if img_dir.exists() else []
    if existing:
        print(f"      🖼 缓存命中: {len(existing)} 张图片", file=sys.stderr)
        return [str(f) for f in existing]

    saved = []
    captions_map = {}  # label → caption text（用于描述时附带）
    doc = pymupdf.open(pdf_path)
    total_pages = min(len(doc), max_pages)

    for page_num in range(total_pages):
        page = doc[page_num]

        # ── 1. 找图注（文字块 + y 坐标 + 完整文本）──
        captions = []  # [(num, y_mid, text)]
        try:
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", []))
                    m = caption_pat.search(text)
                    if m and len(text) < 120:  # 图注通常较短
                        captions.append((int(m.group(1)), line.get("bbox", (0, 0, 0, 0))[3], text.strip()))
        except Exception:
            pass
        captions.sort(key=lambda c: c[1])

        # ── 2. 找图片 bbox ──
        imgs = []
        try:
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if not bbox:
                    continue
                w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                if w * h < 5000:  # 过滤小图
                    continue
                imgs.append((bbox[0], bbox[1], bbox[2], bbox[3]))
        except Exception:
            pass

        if not imgs:
            continue

        # ── 3. 分组：有图注 → 归入下方最近的图注 ──
        groups = []  # [(label, [bboxes])]
        if captions:
            for (x0, y0, x1, y1) in imgs:
                nearest = None
                for num, cap_y, cap_text in captions:
                    if cap_y >= y1:  # 图注在图片下方
                        nearest = (num, cap_y, cap_text)
                        break
                if nearest:
                    label = f"Figure{nearest[0]}"
                    captions_map.setdefault(label, nearest[2])
                else:
                    label = f"p{page_num+1}_fig"
                found = None
                for g in groups:
                    if g[0] == label:
                        found = g; break
                if found:
                    found[1].append((x0, y0, x1, y1))
                else:
                    groups.append((label, [(x0, y0, x1, y1)]))
        else:
            # ── 无图注 → 位置相邻分组回退 ──
            sorted_imgs = sorted(imgs, key=lambda b: (b[1], b[0]))
            cur_group = [sorted_imgs[0]] if sorted_imgs else []
            for i in range(1, len(sorted_imgs)):
                prev = cur_group[-1]
                cur = sorted_imgs[i]
                gap_y = cur[1] - prev[3]
                gap_x = cur[0] - prev[2]
                if gap_y < 20 and gap_x < 20:
                    cur_group.append(cur)
                else:
                    groups.append((f"p{page_num+1}_fig{len(groups)+1}", cur_group))
                    cur_group = [cur]
            if cur_group:
                groups.append((f"p{page_num+1}_fig{len(groups)+1}", cur_group))

        # ── 4. 渲染每个分组的外接矩形 ──
        for label, bboxes in groups:
            try:
                min_x = min(b[0] for b in bboxes)
                min_y = min(b[1] for b in bboxes)
                max_x = max(b[2] for b in bboxes)
                max_y = max(b[3] for b in bboxes)
                # 稍微扩展一点，避免边缘裁剪
                clip = pymupdf.Rect(min_x - 5, min_y - 5, max_x + 5, max_y + 5)
                pix = page.get_pixmap(clip=clip, dpi=150)
                fname = img_dir / f"{stem}_{label}.png"
                pix.save(str(fname))
                saved.append(str(fname))
            except Exception:
                pass

    doc.close()

    # ── 保存图注 sidecar（供 describe_image 附带）──
    if captions_map:
        try:
            cap_file = img_dir / f"{stem}_captions.json"
            import json as _json
            cap_file.write_text(_json.dumps(captions_map, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    if saved:
        print(f"      🖼 提取 {len(saved)} 张图到 {img_dir}/", file=sys.stderr)
    return saved


# ═══════════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════════

def read_pdf_enhanced(pdf_path: str, max_pages: int = 15, max_chars: int | None = None) -> str:
    """
    读取 PDF，提取文本 + 表格 + 图片。

    参数:
      pdf_path : str — PDF 文件路径
      max_pages : int — 最多读取页数
      max_chars : int — 最多返回字符数

    返回:
      str — 结构化文本
    """
    reader = PaperReader(max_pages=max_pages, max_chars=max_chars)
    return reader.read(pdf_path)


# ═══════════════════════════════════════════════════════════════
#  独立测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python pdf_reader.py <path_to_pdf> [max_pages]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    max_pgs = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    try:
        result = read_pdf_enhanced(pdf_path, max_pages=max_pgs, max_chars=2000)
        print(result)
    except ImportError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 读取失败: {e}")
        sys.exit(1)

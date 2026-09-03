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
from pathlib import Path
from typing import Optional

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
    """增强型 PDF 阅读器 — 表格感知 + 双栏排序 + 章节标注"""

    def __init__(self, max_pages: int = 15, max_chars: int | None = None):
        if pdfplumber is None or pymupdf is None:
            raise ImportError(
                "需要安装 pdfplumber 和 PyMuPDF 才能解析 PDF：\n"
                "   pip install pdfplumber PyMuPDF"
            )
        self.max_pages = max_pages
        self.max_chars = max_chars

    # ── 对外接口 ──

    def read(self, pdf_path: str) -> str:
        """
        读取 PDF 并返回结构化文本。

        返回格式：
          📄 PDF 解析完成
            文件名: xxx.pdf  |  总页数: N  |  已读: M  |  提取: X 字符

          ## Abstract
          ...

          ## 1. Introduction
          ...
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            return f"❌ 文件不存在: {pdf_path}"

        with pymupdf.open(pdf_path) as text_pdf, pdfplumber.open(str(pdf_path)) as table_pdf:
            total_pages = len(text_pdf)
            pages_to_read = min(total_pages, self.max_pages)

            page_texts = []
            raw_tables = []  # (page_num, table_index, header: list, rows: list, y0: float, y1: float)

            for i in range(pages_to_read):
                table_page = table_pdf.pages[i]
                text = self._extract_text(text_pdf[i])
                if text:
                    page_texts.append(f"━━━ 第 {i+1} 页 ━━━\n{text}")

                # 提取表格（带位置，用于跨页检测）
                found = table_page.find_tables()
                for ti, tbl in enumerate(found):
                    try:
                        data = tbl.extract()
                        if not data or len(data) < 2:
                            continue
                        header = [str(c or "").strip() for c in data[0]]
                        rows = [[str(c or "").strip() for c in row] for row in data[1:] if any(str(c or "").strip() for c in row)]
                        cell_count = len(header) * (len(rows) + 1)
                        nonempty_cells = sum(bool(cell) for row in [header, *rows] for cell in row)
                        if not rows or not cell_count or nonempty_cells / cell_count < 0.35:
                            continue
                        raw_tables.append((i, ti, header, rows, float(tbl.bbox[1]), float(tbl.bbox[3])))
                    except Exception:
                        pass

            # ── 跨页表格合并 ──
            merged_tables = []
            skip_next = set()
            for idx in range(len(raw_tables)):
                if idx in skip_next:
                    continue
                pn, ti, hdr, rows, y0, y1 = raw_tables[idx]
                # 检查下一页是否有延续：同页号 + 2，且下一页表格列数匹配
                for j in range(idx + 1, min(idx + 4, len(raw_tables))):
                    pn2, ti2, hdr2, rows2, y0_2, y1_2 = raw_tables[j]
                    if pn2 == pn + 1 and ti2 == 0 and y0_2 < 60:  # 下一页顶部
                        if len(hdr) == len(hdr2):
                            rows.extend(rows2)
                            skip_next.add(j)
                merged_tables.append((pn, hdr, rows))

            # ── 格式化为 Markdown ──
            table_mds = []
            for pn, hdr, rows in merged_tables:
                col_count = len(hdr)
                md = []
                hdr_padded = hdr + [""] * (col_count - len(hdr))
                md.append("| " + " | ".join(hdr_padded) + " |")
                md.append("| " + " | ".join(["---"] * col_count) + " |")
                for row in rows:
                    cells = (row + [""] * col_count)[:col_count]
                    md.append("| " + " | ".join(cells) + " |")
                table_mds.append("\n".join(md))

            if table_mds:
                # 表格直接插入对应页位置
                page_texts.append("\n\n📊 **表格**:\n" + "\n\n".join(table_mds))

        # 3) 章节标注（对整个文本做一次）
        full_text = "\n\n".join(page_texts)
        full_text = self._mark_sections(full_text)

        # 页码截断提示
        if total_pages > self.max_pages:
            full_text += f"\n\n...（共 {total_pages} 页，已读取前 {self.max_pages} 页）"

        # Token 截断（max_chars=None 时不截断）
        char_count = len(full_text)
        if self.max_chars is not None and char_count > self.max_chars:
            full_text = full_text[:self.max_chars] + (
                f"\n\n...（内容过长，已截断至前 {self.max_chars} 字符）"
            )
            char_count = self.max_chars

        return (
            f"📄 **PDF 解析完成**\n"
            f"   文件名: {pdf_path.name}  |  "
            f"总页数: {total_pages}  |  已读: {pages_to_read}  |  "
            f"提取: {char_count} 字符\n\n"
            f"{full_text}"
        )

    # ── 1️⃣ 双栏布局感知提取 ──

    def _extract_text(self, page) -> str:
        """按文本块坐标重建阅读顺序，避免按词分组造成跨列混排。"""
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        blocks = []
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            content = " ".join(text.split())
            if not content:
                continue
            # 竖排版权文字和页脚会干扰双栏判断，也不属于论文正文。
            if y0 >= page_height * 0.94 or y1 - y0 > page_height * 0.5:
                continue
            blocks.append({
                "text": content,
                "x0": float(x0),
                "x1": float(x1),
                "y": float(y0),
                "width": float(x1 - x0),
            })

        if not blocks:
            return ""

        mid_x = page_width / 2
        column_width = page_width * 0.58
        def _is_centered(block: dict) -> bool:
            center = (block["x0"] + block["x1"]) / (2 * page_width)
            return 0.4 <= center <= 0.6

        columns = [
            block for block in blocks
            if block["width"] <= column_width and not _is_centered(block)
        ]
        left = [block for block in columns if (block["x0"] + block["x1"]) / 2 < mid_x]
        right = [block for block in columns if (block["x0"] + block["x1"]) / 2 >= mid_x]

        # 两侧各至少有三个正文块才按双栏阅读；否则保持自上而下顺序。
        if len(left) < 3 or len(right) < 3:
            return "\n".join(
                block["text"] for block in sorted(blocks, key=lambda block: (block["y"], block["x0"]))
            )

        first_body_y = min(block["y"] for block in left + right)
        header = [
            block for block in blocks
            if (block["width"] > column_width or _is_centered(block)) and block["y"] < first_body_y
        ]
        trailing = [
            block for block in blocks
            if (block["width"] > column_width or _is_centered(block)) and block["y"] >= first_body_y
        ]
        ordered = (
            sorted(header, key=lambda block: (block["y"], block["x0"]))
            + sorted(left, key=lambda block: block["y"])
            + sorted(right, key=lambda block: block["y"])
            + sorted(trailing, key=lambda block: (block["y"], block["x0"]))
        )
        return "\n".join(block["text"] for block in ordered)

    # ── 2️⃣ 表格感知提取 ──

    def _extract_tables(self, page) -> str:
        """提取页面中的表格，格式化为 Markdown"""
        tables = page.extract_tables()
        if not tables:
            return ""

        result_parts = []
        for table in tables:
            if not table or len(table) < 2:
                continue  # 至少需要表头+一行数据

            # 清理 None 和空白
            cleaned = []
            for row in table:
                cleaned_row = [str(cell or "").strip() for cell in row]
                # 跳过全空行
                if any(cell for cell in cleaned_row):
                    cleaned.append(cleaned_row)

            if len(cleaned) < 2:
                continue

            # 对齐列数（以最长行为准）
            col_count = max(len(row) for row in cleaned)

            md = []
            # 表头
            header = self._pad_row(cleaned[0], col_count)
            md.append("| " + " | ".join(header) + " |")
            # 分隔线
            md.append("| " + " | ".join(["---"] * col_count) + " |")
            # 数据行
            for row in cleaned[1:]:
                cells = self._pad_row(row, col_count)
                md.append("| " + " | ".join(cells) + " |")

            result_parts.append("\n".join(md))

        return "\n\n".join(result_parts)

    @staticmethod
    def _pad_row(row: list[str], n: int) -> list[str]:
        """补齐/截断行到 n 列"""
        if len(row) > n:
            return row[:n]
        return row + [""] * (n - len(row))

    # ── 3️⃣ 章节自动标注 ──

    def _mark_sections(self, text: str) -> str:
        """在文本中检测章节标题，添加 ## 标记"""
        lines = text.split("\n")
        marked = []
        prev_empty = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                marked.append(line)
                prev_empty = True
                continue

            # 章节标题的特征：短（≤80 字符）、独立行、匹配模式
            if len(stripped) > 80:
                marked.append(line)
                prev_empty = False
                continue

            if _SECTION_PATTERN.match(stripped):
                # 前面加空行（除非已有）
                if not prev_empty:
                    marked.append("")
                marked.append(f"## {stripped}")
                marked.append("")  # 标题后空行
                prev_empty = True
            else:
                marked.append(line)
                prev_empty = False

        return "\n".join(marked)


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

"""
📄 PDF 增强阅读器 — 表格感知提取 + 双栏布局识别 + 章节自动标注

改进摘要（对比原始的 fitz.get_text()）：
  1. 表格感知 — 用 pdfplumber 提取表格并格式化为 Markdown 表格
  2. 双栏布局 — 检测双栏排版，先读左栏再读右栏，避免文字交叉混排
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

    def __init__(self, max_pages: int = 15, max_chars: int = 50000):
        if pdfplumber is None:
            raise ImportError(
                "需要安装 pdfplumber 才能解析 PDF：\n"
                "   pip install pdfplumber"
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

        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            pages_to_read = min(total_pages, self.max_pages)

            page_texts = []
            page_tables = []

            for i in range(pages_to_read):
                page = pdf.pages[i]
                # 1) 按布局提取文本（感知双栏）
                text = self._extract_text(page)
                if text:
                    page_texts.append(f"━━━ 第 {i+1} 页 ━━━\n{text}")

                # 2) 提取表格
                tables_md = self._extract_tables(page)
                if tables_md:
                    page_tables.append(f"━━━ 第 {i+1} 页（表格）━━━\n{tables_md}")

        # 合并：文本 + 表格
        full_text = "\n\n".join(page_texts)
        if page_tables:
            full_text += "\n\n" + "\n\n".join(page_tables)

        # 3) 章节标注（对整个文本做一次）
        full_text = self._mark_sections(full_text)

        # 页码截断提示
        if total_pages > self.max_pages:
            full_text += f"\n\n...（共 {total_pages} 页，已读取前 {self.max_pages} 页）"

        # Token 截断
        char_count = len(full_text)
        if char_count > self.max_chars:
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
        """从一页中提取文本，自动处理双栏布局"""
        words = page.extract_words(keep_blank_chars=True, x_tolerance=3)
        if not words:
            return ""

        # 分组为行（按 y 坐标）
        lines = self._group_into_lines(words, page.height)

        # 检测双栏
        is_two_column, mid_x = self._detect_columns(lines, page.width)

        if not is_two_column:
            # 单栏：简单按 y 排序
            lines.sort(key=lambda l: l["y"])
            return "\n".join(l["text"] for l in lines)

        # 双栏：分离出 左栏/右栏/跨栏 行
        left, right, full = [], [], []
        for line in lines:
            avg_x = (line["x0"] + line["x1"]) / 2
            # 跨栏：同时覆盖左右两侧
            if line["x0"] < mid_x - 20 and line["x1"] > mid_x + 20:
                full.append(line)
            elif avg_x < mid_x:
                left.append(line)
            else:
                right.append(line)

        full.sort(key=lambda l: l["y"])
        left.sort(key=lambda l: l["y"])
        right.sort(key=lambda l: l["y"])

        # 输出顺序：跨栏行插入左栏的对应 y 位置 → 然后输出右栏
        ordered = []
        left_idx = 0
        for header_line in full:
            # 在 left 中找到所有 y < header_line["y"] 的，先输出
            while left_idx < len(left) and left[left_idx]["y"] <= header_line["y"]:
                ordered.append(left[left_idx])
                left_idx += 1
            ordered.append(header_line)
        # 剩余左栏
        while left_idx < len(left):
            ordered.append(left[left_idx])
            left_idx += 1

        result = [l["text"] for l in ordered]
        result.append("")  # 分隔左栏和右栏
        result.append("─── 右栏 ───")
        result.extend(l["text"] for l in right)

        return "\n".join(result)

    def _group_into_lines(self, words: list[dict], page_height: float) -> list[dict]:
        """将单词按行分组（基于 y 坐标容差）"""
        if not words:
            return []

        # 估算行高：取最常见单词高度
        heights = [w.get("height", 10) for w in words if w.get("height")]
        y_tolerance = (max(heights) if heights else 10) * 0.6

        lines = []
        # 按 (y, x) 排序
        sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))

        current = None
        for w in sorted_words:
            if current is None or abs(w["top"] - current["y"]) > y_tolerance:
                if current is not None:
                    lines.append(current)
                current = {
                    "text": w.get("text", ""),
                    "x0": w["x0"],
                    "x1": w["x1"],
                    "y": w["top"],
                    "words": [w],
                }
            else:
                # 同行单词，x0/x1 延展
                current["x0"] = min(current["x0"], w["x0"])
                current["x1"] = max(current["x1"], w["x1"])
                current["words"].append(w)
                # 行文本：按 x 排序
                current["words"].sort(key=lambda x: x["x0"])
                current["text"] = " ".join(
                    ww.get("text", "") for ww in current["words"]
                )

        if current is not None:
            lines.append(current)

        return lines

    def _detect_columns(self, lines: list[dict], page_width: float) -> tuple:
        """
        检测页面是否为双栏布局。

        返回: (is_two_column: bool, mid_x: float)
        - 算法: 统计"仅左""仅右""跨栏"行的比例
        - 若左右都有 >15% 的行，且跨栏行 <50%，判定为双栏
        """
        if not lines or page_width <= 0:
            return False, page_width / 2

        mid = page_width / 2
        left_count = right_count = full_count = 0

        for line in lines:
            span = line["x1"] - line["x0"]
            # 跨栏：行宽超过半页的 70%
            if span > page_width * 0.35:
                full_count += 1
            elif (line["x0"] + line["x1"]) / 2 < mid:
                left_count += 1
            else:
                right_count += 1

        total = left_count + right_count + full_count
        if total == 0:
            return False, mid

        left_ratio = left_count / total
        right_ratio = right_count / total
        full_ratio = full_count / total

        is_two_col = (
            left_ratio > 0.15
            and right_ratio > 0.15
            and full_ratio < 0.50
        )
        return is_two_col, mid

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
#  便捷函数（兼容现有 read_pdf 接口）
# ═══════════════════════════════════════════════════════════════

def read_pdf_enhanced(pdf_path: str, max_pages: int = 15, max_chars: int = 50000) -> str:
    """
    便捷函数：用 PaperReader 读取 PDF。

    参数:
      pdf_path : str — PDF 文件路径
      max_pages : int — 最多读取的页数
      max_chars : int — 最多返回的字符数

    返回:
      str — 结构化文本，含表格 Markdown / 双栏排序 / ## 章节标注
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

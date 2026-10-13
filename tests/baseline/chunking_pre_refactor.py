"""
Frozen copy of the pre-refactor chunking implementation (commit29d1418~1).

Used as a behavioural baseline: scripts/verify_chunking_equivalence.py diffs
papers/chunking.py against this file.  It is deliberately a *frozen* snapshot
rather than something read from git history -- once Phase 1 is committed, HEAD
holds the refactored code and a "compare against HEAD" check silently stops
comparing anything.
"""

import bisect
import re

_PDF_READER_PREAMBLE = "\U0001f4c4 **PDF 解析完成**"

# ── 按 token 的分块守卫 ──
# 字符数无法预测 token 数：实测 token/字符密度在 0.21–0.73 之间波动（约 3.5 倍），
# 因为公式、参考文献、非拉丁字符都会让密度飙升。因此按字符切好的块，
# 仍需在编码前按模型真实的 token 上限复核，超限的再切一次，
# 否则模型会静默丢掉尾部（当前模型下 45% 的块受影响）。

def _longest_prefix_chars(text: str, tokenize, max_tokens: int) -> int:
    """返回仍在 max_tokens 之内的最长前缀长度（字符）。"""
    if len(tokenize(text)) <= max_tokens:
        return len(text)
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(tokenize(text[:mid])) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return max(1, lo)


def _overlap_chars(piece: str, tokenize, overlap_tokens: int) -> int:
    """返回 piece 末尾承载最后 overlap_tokens 个字符数（用于保留重叠）。"""
    total = len(tokenize(piece))
    target = max(0, total - overlap_tokens)
    if target == 0:
        return 0
    lo, hi = 1, len(piece)
    while lo < hi:
        mid = (lo + hi) // 2
        if len(tokenize(piece[:mid])) >= target:
            hi = mid
        else:
            lo = mid + 1
    return max(0, len(piece) - lo)


def split_oversized_text(
    text: str,
    tokenize,
    max_tokens: int,
    overlap_tokens: int = 32,
) -> list[tuple[int, str]]:
    """把超长文本切成 (在原文本中的偏移, 片段) 列表，每片都不超过 token 上限。"""
    if len(tokenize(text)) <= max_tokens:
        return [(0, text)]
    pieces: list[tuple[int, str]] = []
    start = 0
    guard = 0
    while start < len(text) and guard < 500:
        guard += 1
        remaining = text[start:]
        end = _longest_prefix_chars(remaining, tokenize, max_tokens)
        piece = remaining[:end].strip()
        if piece:
            pieces.append((start, piece))
        if end >= len(remaining):
            break
        keep = _overlap_chars(remaining[:end], tokenize, overlap_tokens)
        start += max(1, end - keep)
    return pieces


def _tokenize_is_unavailable(tokenize) -> bool:
    """True when a backend exposes ``tokenize()`` but cannot actually count.

    Some backends only ship a usable tokenizer once their weights are downloaded.
    Until then the call returns None, and the guard must fall back to character
    sizing instead of crashing every import.
    """
    try:
        return tokenize("tokenizer availability probe") is None
    except Exception:  # noqa: BLE001 - a broken tokenizer is the same as none
        return True


def limit_chunks_to_tokens(
    chunks: list[dict],
    tokenize,
    max_tokens: int,
    overlap_tokens: int = 32,
) -> list[dict]:
    """对超过模型 token 上限的块做二次切分，避免尾部被静默截断。

    tokenize 不可用或 max_tokens <= 0 时原样返回，保证降级路径安全。
    """
    if tokenize is None or max_tokens <= 0:
        return list(chunks)
    if not chunks:
        return []
    if _tokenize_is_unavailable(tokenize):
        return list(chunks)
    limited: list[dict] = []
    for chunk in chunks:
        text = chunk.get("text") or ""
        if not text:
            continue
        pieces = split_oversized_text(text, tokenize, max_tokens, overlap_tokens)
        unchanged = len(pieces) == 1 and pieces[0][0] == 0 and pieces[0][1] == text
        if unchanged:
            limited.append(chunk)
            continue
        base = int(chunk.get("char_start") or 0)
        for index, (offset, piece) in enumerate(pieces):
            limited.append({
                **chunk,
                "text": piece,
                "index": index,
                "char_start": base + offset,
                "char_end": base + offset + len(piece),
            })
    return limited


def clean_index_text(text: str) -> str:
    """Remove reader status text that is useful to people but not retrieval."""
    normalized = str(text or "").strip()
    if not normalized.startswith(_PDF_READER_PREAMBLE):
        return normalized
    _header, separator, body = normalized.partition("\n\n")
    return body.strip() if separator else normalized


# ═══════════════════════════════════════════════════════════════
#  Chunk 分块策略
# ═══════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 150,
) -> list[dict]:
    """
    将论文文本切分为有重叠的语义块。

    策略：
      1. 先按段落（双换行）切分
      2. 块大小控制在 chunk_size 左右，相邻块保持 overlap 字符重叠
      3. 公式区域（$$...$$ 和 $...$）标记为不可切割，切点自动避开

    返回: [{"text": "...", "section": "Method", "index": 0, "char_start": 0, "char_end": 580}, ...]
    """
    # 预扫描：找到所有公式的不可切割区间
    # [^$]* 允许 $$..$$ 空公式，避免正则漏掉边界情况
    _formula_pat = re.compile(r'\$\$[^$]*\$\$|\$[^$]+\$')
    _sent_end = re.compile(r'[。．.!?！？;；]')

    def _find_formula_ranges(para: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in _formula_pat.finditer(para)]

    def _safe_cut(para: str, ideal: int, forbidden: list[tuple[int, int]], para_len: int) -> int:
        """调整切点，避免落在公式区间内（公式区间已排序，二分查找）"""
        import bisect
        starts = [s for s, e in forbidden]
        idx = bisect.bisect_right(starts, ideal) - 1
        if idx >= 0:
            s, e = forbidden[idx]
            if s <= ideal <= e:
                return s if (ideal - s) < (e - ideal) else e
        return ideal

    def _sentence_cut(para: str, end: int, para_len: int) -> int:
        """把切点挪到最近的句子结束符后（在 ±30 字符窗口内）"""
        if end >= para_len:
            return end
        window_start = max(0, end - 30)
        window = para[window_start:end]
        # 找窗口内最后一个句子结束符
        last = None
        for m in _sent_end.finditer(window):
            last = m
        if last:
            return window_start + last.end()
        return end

    # ── 第1层：章节切分（## 和 ### 两级）──
    section_blocks = []  # [(section_label, text)]
    current_section = "未标注"
    current_sub = ""
    lines = text.split("\n")
    buf = []

    def flush_buf():
        nonlocal buf
        if buf:
            body = "\n".join(buf).strip()
            if body:
                label = current_section if not current_sub else f"{current_section} > {current_sub}"
                section_blocks.append((label, body))
        buf = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            flush_buf()
            if not stripped.startswith("### "):
                current_section = stripped[3:].strip()
                current_sub = ""
            else:
                current_sub = stripped[4:].strip()
        elif stripped.startswith("### "):
            flush_buf()
            current_sub = stripped[4:].strip()
        else:
            buf.append(line)
    flush_buf()

    # ── 第2/3层：段落切分 + 尺寸切分 ──
    chunks = []
    char_pos = 0

    for section_label, section_text in section_blocks:
        paragraphs = section_text.split("\n\n")
        # 前缀只取一级章节（## Method），子章节存 metadata 不注入文本
        prefix_label = section_label.split(" > ")[0]
        prefix = f"## {prefix_label}\n" if section_label != "未标注" else ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                char_pos += 2
                continue

            para_start = char_pos
            para_len = len(para)
            adaptive_size = 1500 if section_label != "未标注" else chunk_size
            formula_ranges = sorted(_find_formula_ranges(para))

            if para_len + len(prefix) <= adaptive_size:
                chunks.append({
                    "text": prefix + para,
                    "section": section_label,
                    "char_start": para_start,
                    "char_end": para_start + para_len,
                })
                char_pos += para_len + 2
                continue

            # 长段落：滑动窗口，切点避开公式 + 句子边界
            start = 0
            while start < para_len:
                ideal_end = min(start + adaptive_size - len(prefix), para_len)
                end = _safe_cut(para, ideal_end, formula_ranges, para_len)
                # end 必须 > start，防止巨大公式边界把 end 拨回 start 之前 → 1字符chunk
                if end <= start:
                    end = ideal_end
                end = min(max(end, start + 1), para_len)
                # 句子切割同样不能后退
                sent_end = _sentence_cut(para, end, para_len)
                if sent_end > start:
                    end = sent_end
                end = min(max(end, start + 1), para_len)

                chunks.append({
                    "text": prefix + para[start:end],
                    "section": section_label,
                    "char_start": para_start + start,
                    "char_end": para_start + end,
                })

                if end >= para_len:
                    break
                # 下一块起点：必须前进，防止公式边界把 start 拨回 → 死循环
                next_start = _safe_cut(para, ideal_end - overlap, formula_ranges, para_len)
                if next_start <= start:
                    next_start = end
                start = min(next_start, para_len - 1)

            char_pos += para_len + 2

    for i, chunk in enumerate(chunks):
        chunk["index"] = i

    return chunks

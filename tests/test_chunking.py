"""papers.chunking 的独立单元测试。

这些用例过去无法独立存在：切块逻辑埋在 ``PaperStore`` 里，而构造
``PaperStore`` 需要 ChromaDB + 嵌入模型 + 数据目录，测试只能用
``PaperStore.__new__`` 绕过构造函数再手工注入私有字段。抽成纯函数模块后，
这里全部走正常调用路径——这正是本次重构要换来的东西。
"""

import unittest

from papers.chunking import (
    _find_formula_ranges,
    _overlap_chars,
    _safe_cut,
    _sentence_cut,
    _tokenize_is_unavailable,
    chunk_text,
    clean_index_text,
    limit_chunks_to_tokens,
    split_oversized_text,
)


def whitespace_tokenize(text):
    """Deterministic stand-in for a real tokenizer: one token per word."""
    return text.split()


def _run_isolated(source):
    """Execute ``source`` in a clean interpreter with only the repo on sys.path.

    ``python -c`` puts ``sys.path[0]`` at the current directory, but relying on
    that is fragile; PYTHONPATH makes it explicit.  Isolation matters because
    ``sys.modules`` is per-interpreter -- an in-process check would only see
    what earlier tests happened to import.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=root, env=env, capture_output=True, text=True,
    )


class TokenizerGuardTest(unittest.TestCase):
    """The token re-split guard exists because character counts lie.

    Measured density varies 0.21-0.73 tokens per character (~3.5x), because
    formulas, references and non-Latin text all inflate it.  Chunks sized by
    characters therefore still overflow, and the model silently truncates.
    """

    def test_split_returns_single_piece_when_within_limit(self):
        text = "short enough"
        self.assertEqual(split_oversized_text(text, whitespace_tokenize, 100), [(0, text)])

    def test_split_breaks_overlong_text_and_respects_limit(self):
        text = " ".join(["word"] * 500)
        pieces = split_oversized_text(text, whitespace_tokenize, 50)
        self.assertGreater(len(pieces), 1)
        for _offset, piece in pieces:
            self.assertLessEqual(len(piece.split()), 50)

    def test_split_offsets_increase_monotonically(self):
        text = " ".join(["word"] * 500)
        offsets = [offset for offset, _piece in split_oversized_text(text, whitespace_tokenize, 50)]
        self.assertEqual(offsets, sorted(offsets))

    def test_overlap_keeps_trailing_tokens(self):
        piece = " ".join(["w"] * 100)
        kept = _overlap_chars(piece, whitespace_tokenize, 20)
        self.assertGreaterEqual(kept, 1)
        self.assertLessEqual(kept, len(piece))

    def test_overlap_returns_zero_when_piece_shorter_than_target(self):
        self.assertEqual(_overlap_chars("two words", whitespace_tokenize, 100), 0)

    def test_unavailable_tokenizer_is_detected(self):
        """Backends only ship a usable tokenizer after weights download."""
        self.assertTrue(_tokenize_is_unavailable(lambda _s: None))

    def test_raising_tokenizer_counts_as_unavailable(self):
        """A broken tokenizer must degrade, not crash every import."""
        def broken(_s):
            raise RuntimeError("weights not downloaded")

        self.assertTrue(_tokenize_is_unavailable(broken))

    def test_available_tokenizer_is_not_flagged(self):
        self.assertFalse(_tokenize_is_unavailable(whitespace_tokenize))


class LimitChunksToTokensTest(unittest.TestCase):
    """Degraded paths must pass data through untouched, never crash on import."""

    def test_none_tokenize_passes_chunks_through(self):
        chunks = [{"text": "abc"}]
        self.assertEqual(limit_chunks_to_tokens(chunks, None, 512), chunks)

    def test_non_positive_limit_passes_chunks_through(self):
        chunks = [{"text": "abc"}]
        self.assertEqual(limit_chunks_to_tokens(chunks, whitespace_tokenize, 0), chunks)

    def test_unavailable_tokenizer_passes_chunks_through(self):
        chunks = [{"text": "abc"}]
        self.assertEqual(limit_chunks_to_tokens(chunks, lambda _s: None, 512), chunks)

    def test_empty_input_returns_empty(self):
        self.assertEqual(limit_chunks_to_tokens([], whitespace_tokenize, 512), [])

    def test_within_limit_chunk_is_passed_through_identically(self):
        chunk = {"text": "a b c", "char_start": 0, "index": 0}
        self.assertEqual(limit_chunks_to_tokens([chunk], whitespace_tokenize, 512), [chunk])

    def test_oversized_chunk_is_split_and_offsets_advance(self):
        chunk = {"text": " ".join(["w"] * 400), "char_start": 100, "index": 0}
        result = limit_chunks_to_tokens([chunk], whitespace_tokenize, 50)
        self.assertGreater(len(result), 1)
        starts = [c["char_start"] for c in result]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(starts[0], 100)

    def test_empty_text_chunk_is_dropped(self):
        result = limit_chunks_to_tokens([{"text": "", "char_start": 0}], whitespace_tokenize, 512)
        self.assertEqual(result, [])


class FormulaProtectionTest(unittest.TestCase):
    """Cut points must not land inside a formula.

    A formula sliced in half produces garbage tokens on both sides, which
    measurably hurts retrieval, so ``_safe_cut`` moves the cut to the nearer
    formula boundary instead.
    """

    def test_find_formula_ranges_detects_both_delimiters(self):
        para = "text $inline$ more $$block$$ end"
        ranges = _find_formula_ranges(para)
        self.assertEqual(len(ranges), 2)
        for start, end in ranges:
            self.assertNotEqual(start, end)

    def test_find_formula_ranges_on_text_without_formula(self):
        self.assertEqual(_find_formula_ranges("no math here"), [])

    def test_safe_cut_moves_point_out_of_formula(self):
        para = "aaaa $E=mc^2$ bbbb"
        forbidden = sorted(_find_formula_ranges(para))
        ideal = 7  # lands inside $E=mc^2$
        cut = _safe_cut(para, ideal, forbidden, len(para))
        self.assertTrue(cut <= 5 or cut >= 13, f"cut {cut} still inside the formula")

    def test_safe_cut_keeps_ideal_point_when_no_formula(self):
        self.assertEqual(_safe_cut("aaaa bbbb", 4, [], 9), 4)

    def test_chunks_do_not_split_inline_formula(self):
        text = "## Math\n\n" + ("填充内容。" * 60) + " 关键公式 $E=mc^2$ " + ("后续内容。" * 60)
        for chunk in chunk_text(text, chunk_size=300, overlap=50):
            body = chunk["text"]
            for _s, e in _find_formula_ranges(body):
                self.assertEqual(
                    body.count("$") % 2, 0,
                    f"公式被切开：{body[max(0, e - 12):e + 12]!r}",
                )


class SentenceBoundaryTest(unittest.TestCase):
    """Cuts prefer sentence ends so chunks stay semantically readable."""

    def test_cut_snaps_back_to_preceding_sentence_end(self):
        """The window looks *backwards* from the cut, not forwards.

        A cut landing mid-sentence should snap to the end of the sentence it
        started in, so the chunk ends on a real boundary. The search window is
        the preceding 30 characters.
        """
        para = "第一句话。第二句话还没结束"
        end = _sentence_cut(para, 12, len(para))
        self.assertEqual(end, 5)
        self.assertEqual(para[:end], "第一句话。")

    def test_cut_stays_put_when_window_has_no_punctuation(self):
        para = "no punctuation here at all"
        self.assertEqual(_sentence_cut(para, 10, len(para)), 10)

    def test_cut_at_or_past_end_is_unchanged(self):
        para = "abc"
        self.assertEqual(_sentence_cut(para, 3, 3), 3)


class ChunkTextTest(unittest.TestCase):
    """Structure preservation: sections, paragraphs, offsets, progress."""

    def test_short_text_becomes_one_chunk(self):
        chunks = chunk_text("很短的一段文本。", chunk_size=1000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["index"], 0)

    def test_section_heading_becomes_section_label_and_prefix(self):
        chunks = chunk_text("## Method\n\n方法正文。", chunk_size=1000)
        self.assertEqual(chunks[0]["section"], "Method")
        self.assertTrue(chunks[0]["text"].startswith("## Method\n"))

    def test_nested_heading_keeps_full_label_but_injects_only_parent(self):
        chunks = chunk_text("## Method\n### Step 1\n\n细节。", chunk_size=1000)
        self.assertEqual(chunks[0]["section"], "Method > Step 1")
        self.assertTrue(chunks[0]["text"].startswith("## Method\n"))
        self.assertNotIn("### Step 1", chunks[0]["text"])

    def test_unsectioned_text_is_labelled_placeholder(self):
        chunks = chunk_text("没有章节的普通文本。", chunk_size=1000)
        self.assertEqual(chunks[0]["section"], "未标注")

    def test_empty_input_yields_no_chunks(self):
        self.assertEqual(chunk_text("", chunk_size=1000), [])

    def test_newlines_only_yields_no_chunks(self):
        self.assertEqual(chunk_text("\n\n\n\n", chunk_size=1000), [])

    def test_indices_are_sequential(self):
        text = "## A\n\n" + "内容甲。" * 200 + "\n\n## B\n\n" + "内容乙。" * 200
        chunks = chunk_text(text, chunk_size=300, overlap=50)
        self.assertEqual([c["index"] for c in chunks], list(range(len(chunks))))

    def test_long_paragraph_is_split_with_overlap(self):
        chunks = chunk_text("## Results\n\n" + "结果描述。" * 300, chunk_size=300, overlap=50)
        self.assertGreater(len(chunks), 1)

    def test_char_offsets_are_monotonic_and_within_bounds(self):
        text = "## A\n\n" + "甲内容。" * 200 + "\n\n## B\n\n" + "乙内容。" * 200
        chunks = chunk_text(text, chunk_size=300, overlap=50)
        starts = [c["char_start"] for c in chunks]
        self.assertEqual(starts, sorted(starts))
        for chunk in chunks:
            self.assertLessEqual(chunk["char_start"], chunk["char_end"])

    def test_every_chunk_carries_the_required_keys(self):
        chunks = chunk_text("## A\n\n" + "内容。" * 400, chunk_size=300, overlap=50)
        for chunk in chunks:
            for key in ("text", "section", "index", "char_start", "char_end"):
                self.assertIn(key, chunk)

    def test_no_zero_length_chunks(self):
        """Formula-aware cut points can be nudged backwards; guard must win."""
        text = "## M\n\n" + ("填充。" * 100) + "$" + ("公式内容" * 40) + "$" + ("尾巴。" * 100)
        for chunk in chunk_text(text, chunk_size=200, overlap=40):
            self.assertGreater(len(chunk["text"].strip()), 0)

    def test_terminates_on_pathological_formula_boundaries(self):
        """A huge formula must not pin the window start and loop forever."""
        text = "## M\n\n" + ("短。" * 200) + "$$" + ("x" * 3000) + "$$" + ("尾。" * 200)
        chunks = chunk_text(text, chunk_size=200, overlap=40)  # must not hang
        self.assertIsInstance(chunks, list)


class CleanIndexTextTest(unittest.TestCase):
    """Reader-facing status lines help humans but pollute retrieval."""

    def test_preamble_is_stripped(self):
        raw = "📄 **PDF 解析完成**\n\n真正的正文内容"
        self.assertEqual(clean_index_text(raw), "真正的正文内容")

    def test_plain_text_untouched(self):
        self.assertEqual(clean_index_text("普通文本"), "普通文本")

    def test_empty_input(self):
        self.assertEqual(clean_index_text(""), "")

    def test_none_input_is_coerced(self):
        self.assertEqual(clean_index_text(None), "")

    def test_preamble_without_body_is_kept(self):
        raw = "📄 **PDF 解析完成**"
        self.assertEqual(clean_index_text(raw), raw)


class NoStoreConstructionTest(unittest.TestCase):
    """The point of the refactor, asserted so it cannot silently regress.

    Chunking must stay importable and callable without ChromaDB, an embedder
    or a data directory.  If a future change reaches for the network or the
    filesystem here, this fails at import time.

    The check runs in a **subprocess** on purpose: ``sys.modules`` is shared
    per interpreter, so running it in-process would merely observe whatever
    ``tests.test_core`` happened to import first and report a false failure.
    """

    def test_chunking_needs_no_vector_store(self):
        result = _run_isolated(
            "import sys\n"
            "import papers.chunking as c\n"
            "assert c.chunk_text('## A\\n\\n内容。' * 80, 300, 50)\n"
            "banned = [m for m in ('chromadb', 'paper_store', 'embeddings',\n"
            "                       'sentence_transformers') if m in sys.modules]\n"
            "assert not banned, 'chunking pulled in: ' + ','.join(banned)\n"
            "print('OK')\n"
        )
        self.assertEqual(
            result.returncode, 0,
            f"chunking is not self-contained:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("OK", result.stdout)

    def test_chunking_imports_only_stdlib(self):
        """No third-party import may be pulled in at module import time."""
        result = _run_isolated(
            "import builtins\n"
            "real = builtins.__import__\n"
            "banned = {'chromadb', 'numpy', 'torch', 'onnxruntime', 'transformers'}\n"
            "def guard(name, *a, **k):\n"
            "    if name.split('.')[0] in banned:\n"
            "        raise ImportError('blocked: ' + name)\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = guard\n"
            "import papers.chunking as c\n"
            "assert c.chunk_text('文本', 1000)\n"
            "assert c.limit_chunks_to_tokens([{'text': 'x'}], lambda s: s.split(), 4)\n"
            "print('OK')\n"
        )
        self.assertEqual(
            result.returncode, 0,
            f"chunking required a third-party import:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("OK", result.stdout)

    def test_result_shape_is_plain_data(self):
        chunks = chunk_text("## A\n\n内容。" * 100, chunk_size=300, overlap=50)
        self.assertIsInstance(chunks, list)
        self.assertIsInstance(chunks[0], dict)
        self.assertIsInstance(chunks[0]["text"], str)


if __name__ == "__main__":
    unittest.main()
"""抽取等价性验证：papers/chunking.py 与原 paper_store 实现必须逐字节一致。

用法（需先切出原始实现）：
    python scripts/verify_chunking_equivalence.py

做法是把原函数体从 git 里取出来、动态编译成一个独立模块，然后对同一批
输入同时跑新旧两版，逐块比对 text / section / index / char_start / char_end。
任何一项不同即判定为回归。
"""

from __future__ import annotations

import re
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from papers import chunking as new_mod  # noqa: E402


def load_original() -> types.ModuleType:
    """Rebuild the pre-split chunking functions from git HEAD.

    The extracted region keeps its own local imports so the rebuilt module is
    self-contained -- an earlier version omitted them and every case then
    "differed" simply because the old code raised NameError.  If that happens
    again, suspect this loader before trusting the diff.
    """
    source = subprocess.run(
        ["git", "show", "HEAD:paper_store.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    lines = source.splitlines(keepends=True)

    # Module-level region that moves verbatim into papers/chunking.py:
    #   L53..L163  token guard helpers
    #   L230..L389 clean_index_text + chunk_text
    #
    # The preamble is a plain string literal; decode it with ast.literal_eval so
    # no arbitrary code from the repo is ever executed.
    import ast

    raw_preamble = re.search(r"^_PDF_READER_PREAMBLE = (.+)$", source, re.M).group(1)
    preamble_value = ast.literal_eval(raw_preamble)

    bootstrap = (
        "import re\n"
        "import bisect\n"
        f"_PDF_READER_PREAMBLE = {preamble_value!r}\n"
    )
    parts = [bootstrap, "".join(lines[52:163]), "\n\n".join(lines[229:389])]

    mod = types.ModuleType("original_chunking")
    exec(compile("".join(parts), "original_chunking", "exec"), mod.__dict__)
    assert mod._PDF_READER_PREAMBLE == preamble_value, "preamble mismatch in loader"
    return mod


def sample_texts() -> list[tuple[str, str]]:
    """Inputs that exercise sections, formulas, long paragraphs, edge cases."""
    return [
        ("plain-short", "这是一段很短的文本，不超过块大小，应当整块保留。"),
        ("two-sections", "## Introduction\n\n第一段内容。\n\n## Method\n\n第二段内容。"),
        ("nested-sub", "## Method\n### Step 1\n\n细节一。\n\n### Step 2\n\n细节二。"),
        ("long-paragraph", "## Results\n\n" + ("这是一个很长的句子用来测试滑动窗口切分逻辑。" * 120)),
        ("formula", "## Math\n\n公式 $E=mc^2$ 与块 $$x^2 + y^2 = z^2$$ 不能被切断。" + ("补充说明。" * 200)),
        ("empty", ""),
        ("only-newlines", "\n\n\n\n"),
        ("no-section", "没有章节标记的一段普通文本。" * 80),
    ]


def main() -> int:
    old = load_original()
    failures: list[str] = []

    for name, text in sample_texts():
        for chunk_size, overlap in ((1000, 150), (300, 50), (1500, 200)):
            label = f"{name}[size={chunk_size},overlap={overlap}]"
            try:
                a = old.chunk_text(text, chunk_size, overlap)
            except Exception as exc:  # noqa: BLE001
                a = f"RAISED {type(exc).__name__}: {exc}"
            try:
                b = new_mod.chunk_text(text, chunk_size, overlap)
            except Exception as exc:  # noqa: BLE001
                b = f"RAISED {type(exc).__name__}: {exc}"

            if a != b:
                failures.append(f"{label}: chunk_text 输出不一致")
                continue

            if isinstance(a, list):
                total = sum(len(c.get("text", "")) for c in a)
                if total == 0 and text.strip():
                    failures.append(f"{label}: 产出 0 字符，疑似吞内容")

    # Token guard equivalence, including the degraded paths.
    def fake_tokenize(s: str) -> list[str]:
        return s.split()

    cases = [
        ([{"text": "short", "char_start": 0}], 512),
        ([{"text": " ".join(["word"] * 400), "char_start": 0}], 100),
        ([{"text": "", "char_start": 0}], 100),
        ([{"text": "abc", "char_start": 0}], 0),          # max_tokens<=0 降级
    ]
    for i, (chunks, max_tokens) in enumerate(cases):
        for tk, label in ((fake_tokenize, "ok"), (None, "no-tokenize"),
                          (lambda s: None, "tokenizer-none")):
            try:
                a = old.limit_chunks_to_tokens(chunks, tk, max_tokens)
            except Exception as exc:  # noqa: BLE001
                a = f"RAISED {exc}"
            try:
                b = new_mod.limit_chunks_to_tokens(chunks, tk, max_tokens)
            except Exception as exc:  # noqa: BLE001
                b = f"RAISED {exc}"
            if a != b:
                failures.append(f"limit_chunks_to_tokens[{i}/{label}] 不一致")

    # clean_index_text
    for raw in ("📄 **PDF 解析完成**\n\n正文", "普通文本", "", "📄 **PDF 解析完成**"):
        if old.clean_index_text(raw) != new_mod.clean_index_text(raw):
            failures.append(f"clean_index_text({raw[:12]!r}) 不一致")

    if failures:
        print("EQUIVALENCE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    total_cases = len(sample_texts()) * 3 + len(cases) * 3 + 4
    print(f"Equivalence check passed ({total_cases} cases).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
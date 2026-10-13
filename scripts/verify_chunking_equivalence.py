"""抽取等价性验证：papers/chunking.py 与重构前实现必须逐块一致。

用法：
    python scripts/verify_chunking_equivalence.py

基线是 `tests/baseline/chunking_pre_refactor.py`（重构前的冻结快照），
对同一批输入同时跑新旧两版，逐块比对 text / section / index / char_start /
char_end，任何一项不同即判定为回归。

为什么用冻结快照而不是 git 历史：
Phase 1 提交之后，HEAD 里就是重构后的代码，"与 HEAD 比对"会静默地什么都不比。
这个脚本曾经正是这样失效的——提交当天就报 `AttributeError: NoneType`，
看起来像工具坏了，实际是它已经不再做任何比较。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "baseline"))

from papers import chunking as new_mod  # noqa: E402

try:
    import chunking_pre_refactor as old_mod  # noqa: E402
except ImportError as exc:  # pragma: no cover - surfaced as a clear failure
    raise SystemExit(
        f"baseline module missing: {exc}\n"
        f"expected {ROOT / 'tests' / 'baseline' / 'chunking_pre_refactor.py'}"
    ) from exc


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
    old = old_mod
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
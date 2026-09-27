"""Audit the indexed paper corpus against embedding-model token limits.

Answers the practical migration questions with the real corpus rather than
averages:

  * What is the language mix, and how many tokens does each chunk really use?
  * How much content does the current 256-token ceiling discard?
  * If we switch to a model with a longer limit (512 / 8192 / 32768), how many
  chunks would still be truncated?
  * What does the index cost at 384 vs 1024 vs 4096 dimensions?

Caveat: token counts use the currently installed tokenizer.  Other models use
different vocabularies, so cross-model figures are close estimates for English
and rougher for mixed content.

Run:  python scripts/audit_chunks.py
"""

from __future__ import annotations

import pathlib
import re
import sys

import numpy as np

CHROMA_DIR = pathlib.Path(__file__).resolve().parents[1] / "runtime" / "derived" / "chroma"
TOKENIZER_DIR = (
    pathlib.Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2" / "onnx"
)
CJK = re.compile(r"[一-鿿]")

# token ceiling of the current model, and of candidate replacements
LIMITS = [("当前 all-MiniLM-L6-v2", 256), ("bge-large-en-v1.5", 512), ("bge-m3", 8192), ("Qwen3-Embedding", 32768)]
DIMS = [("当前 384 维", 384), ("bge-m3 / Qwen3-0.6B 1024 维", 1024), ("Qwen3-4B 2560 维", 2560), ("Qwen3-8B 4096 维", 4096)]
# bytes per float32 + HNSW graph overhead per vector, roughly
BYTES_PER_DIM = 4 + 0.13


def main() -> int:
    try:
        from tokenizers import Tokenizer
        from chromadb.config import Settings
        import chromadb
    except ImportError as exc:  # pragma: no cover - environment dependent
        print(f"缺少依赖: {exc}")
        return 1

    if not TOKENIZER_DIR.is_dir():
        print(f"未找到 tokenizer: {TOKENIZER_DIR}")
        return 1
    tok = Tokenizer.from_file(str(TOKENIZER_DIR / "tokenizer.json"))
    tok.no_truncation()
    tok.no_padding()

    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR), settings=Settings(anonymized_telemetry=False)
    )
    try:
        docs = client.get_collection("papers").get(include=["documents"])["documents"]
    except Exception as exc:  # noqa: BLE001
        print(f"读取集合失败: {exc}")
        return 1
    if not docs:
        print("语料为空")
        return 1

    tokens, ratios, langs = [], [], []
    for text in docs:
        text = text or ""
        n = len(tok.encode(text).ids)
        tokens.append(n)
        ratios.append(n / max(1, len(text)))
        langs.append("zh" if len(CJK.findall(text)) / max(1, len(text)) > 0.15 else "en")

    t = np.array(tokens)
    r = np.array(ratios)
    zh = sum(1 for x in langs if x == "zh")

    print("=== 1. 语料概况 ===")
    print(f"  切块总数        : {len(docs)}")
    print(f"  语言分布        : 英文 {len(docs) - zh} 块 / 中文 {zh} 块")
    print(f"  块字符数        : 中位 {int(np.median([len(d or '') for d in docs]))}")
    print(f"  块 token 数     : 中位 {int(np.median(t))}  p90 {int(np.percentile(t, 90))}  最大 {int(t.max())}")
    print(f"  token/字符密度  : p10 {np.percentile(r, 10):.2f}  中位 {np.median(r):.2f}  p90 {np.percentile(r, 90):.2f}  最大 {r.max():.2f}")
    print("  → 密度波动大，说明按字符切块无法预测 token 数")

    print("\n=== 2. 各模型上限下的截断情况 ===")
    print(f"  {'模型':<28}{'上限':>8}{'被截断块数':>12}{'占比':>8}{'整体丢弃token':>14}")
    for name, limit in LIMITS:
        over = t[t > limit]
        lost = int((t - np.minimum(t, limit)).sum())
        pct = len(over) / len(t) * 100
        share = lost / max(1, t.sum()) * 100
        print(f"  {name:<28}{limit:>8}{len(over):>12}{pct:>7.0f}%{share:>13.1f}%")

    print("\n=== 3. 不同维度的索引成本 ===")
    total = len(t)
    print(f"  {'维度':<28}{'向量体积':>12}{'HNSW 常驻(估)':>16}")
    for name, dim in DIMS:
        raw = total * dim * 4 / 1024**2
        mem = total * dim * BYTES_PER_DIM / 1024**2
        print(f"  {name:<28}{raw:>10.1f} MB{mem:>14.1f} MB")

    print("\n=== 4. 结论提示 ===")
    print("  * 换成长上下文模型后，截断基本归零，但必须重建向量集合。")
    print("  * 维度变化后 ChromaDB 需新建集合（维度在创建时确定）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

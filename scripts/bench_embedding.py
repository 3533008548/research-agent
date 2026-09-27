"""Benchmark the embedding model this project actually uses.

ChromaDB's built-in ONNX ``DefaultEmbeddingFunction`` (all-MiniLM-L6-v2, 384-d,
CPU).  The point is to answer three questions with measurements instead of
guesswork:

  1. What is the real token ceiling, and does this project's character-based
     chunking (1000 / 1500 chars) silently exceed it?
  2. What throughput does this machine actually sustain?
  3. How does that convert into "papers per hour"?

Two channels are compared side by side, because paper_store.py no longer calls
ChromaDB's wrapper directly:

* ``MiniLMEmbedder`` — the production path.  One ONNX session, padding to the
  longest sequence in each batch.
* ``DefaultEmbeddingFunction`` — ChromaDB's own wrapper.  It rebuilds the
  session on every call and always pads to 256 positions.

Run:  python scripts/bench_embedding.py
"""

from __future__ import annotations

import pathlib
import time

import numpy as np
from chromadb.utils import embedding_functions

from embeddings import MiniLMEmbedder

# The embedder paper_store.py actually uses.
fn = MiniLMEmbedder()
# ChromaDB's wrapper, kept only to show what the production path avoids.
legacy_fn = embedding_functions.DefaultEmbeddingFunction()


def report_model() -> None:
    print("=== 1. 模型身份 ===")
    print("  embed fn      :", type(fn).__name__)
    home = pathlib.Path.home()
    for cand in (
        home / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2",
        home / ".cache" / "chroma" / "onnx_models",
    ):
        if cand.exists():
            print("  model dir     :", cand)
            for j in list(cand.rglob("*.json"))[:20]:
                try:
                    txt = j.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "max_seq_length" in txt or "max_position" in txt:
                    print(f"  {j.name:<28}-> {txt.strip()[:160]}")
            break


def load_tokenizer():
    """Return a callable text -> token ids, or None if unavailable."""
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
        return lambda s: tok(s, add_special_tokens=True)["input_ids"]
    except Exception as exc:  # noqa: BLE001 - offline / not cached
        print(f"  (tokenizer unavailable: {type(exc).__name__})")
        return None


def make_text(n_chars: int, lang: str) -> str:
    if lang == "zh":
        base = "本文提出一种基于深度神经网络的文本表示方法，并在多个公开数据集上验证了其有效性。"
    else:
        base = (
            "We propose a deep neural representation for text and evaluate it "
            "on several public benchmarks, reporting accuracy and latency. "
        )
    out = base
    while len(out) < n_chars:
        out += base
    return out[:n_chars]


def token_report(tokenize) -> None:
    print("\n=== 2. 字符块 vs 256 token 上限 ===")
    print(f"  {'块类型':<26}{'字符数':>8}{'token 数':>10}   {'是否超上限':<10}")
    cases = [
        ("普通块 1000 字符(英文)", 1000, "en"),
        ("普通块 1000 字符(中文)", 1000, "zh"),
        ("章节块 1500 字符(英文)", 1500, "en"),
        ("章节块 1500 字符(中文)", 1500, "zh"),
    ]
    for label, n, lang in cases:
        text = make_text(n, lang)
        if tokenize is None:
            print(f"  {label:<26}{len(text):>8}{'?':>10}   tokenizer 不可用")
            continue
        ntok = len(tokenize(text))
        over = "超! 被截断" if ntok > 256 else "ok"
        print(f"  {label:<26}{len(text):>8}{ntok:>10}   {over}")

    # Prove truncation: a long chunk must embed identically to its first 256 tokens.
    if tokenize is not None:
        long_text = make_text(1500, "zh")
        ids = tokenize(long_text)
        head_ids = ids[:256]
        try:
            from transformers import AutoTokenizer

            tok = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
            head_text = tok.decode(head_ids, skip_special_tokens=True)
        except Exception:  # noqa: BLE001
            head_text = long_text[:600]
        e_full = np.asarray(fn([long_text])[0])
        e_head = np.asarray(fn([head_text])[0])
        cos = float(e_full @ e_head / (np.linalg.norm(e_full) * np.linalg.norm(e_head)))
        print(f"\n  截断验证(中文 1500 字符): 全文 vs 仅前 256 token 的余弦相似度 = {cos:.4f}")
        print("  → 接近 1.0 即证明超出上限的部分被直接丢弃（静默截断）" if cos > 0.999 else "  → 未观察到完全截断")


def channel_comparison() -> None:
    print("\n=== 2b. 两条通道对比（真实查询长度 vs 块长度）===")
    # A query is a sentence; a chunk is what the token guard produced.
    short = ["What benchmark does the paper evaluate on?"]
    chunk = [make_text(1000, "en")]
    print(f"  {'场景':<22}{'ChromaDB 默认':>14}{'本项目通道':>14}{'提速':>10}")
    for label, texts, repeats in (("单条查询（短句）", short, 5), ("单个块（1000 字符）", chunk, 3)):
        timings = {}
        for name, channel in (("legacy", legacy_fn), ("fast", fn)):
            channel(texts)  # warm the session / tokenizer
            t0 = time.perf_counter()
            for _ in range(repeats):
                channel(texts)
            timings[name] = (time.perf_counter() - t0) / repeats
        speedup = timings["legacy"] / max(timings["fast"], 1e-9)
        print(
            f"  {label:<22}{timings['legacy'] * 1000:>12.1f} ms"
            f"{timings['fast'] * 1000:>12.1f} ms{speedup:>9.1f}x"
        )


def throughput() -> float:
    print("\n=== 3. 本机实测吞吐（每块约 1000 字符中文）===")
    print(f"  {'批大小':>8}{'耗时(s)':>12}{'块/秒':>12}{'字符/秒':>12}")
    measured: list[tuple[int, float]] = []
    for n in (32, 128, 512, 1024, 2048):
        texts = [make_text(1000, "zh") for _ in range(n)]
        try:
            t0 = time.perf_counter()
            vecs = fn(texts)
            dt = time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001 - OOM etc.
            print(f"  {n:>8}  失败: {type(exc).__name__}: {exc}")
            break
        if not vecs or len(vecs) != n:
            print(f"  {n:>8}  返回数量异常 {len(vecs)}")
            break
        rate = n / dt
        measured.append((n, rate))
        print(f"  {n:>8}{dt:>12.2f}{rate:>12.1f}{rate * 1000:>12.0f}")
    if not measured:
        return 0.0
    # Use the largest successful batch: the steady-state rate.
    best_rate = measured[-1][1]
    print(f"\n  稳态吞吐 ≈ {best_rate:.0f} 块/秒（批大小 {measured[-1][0]}）")
    print(f"  向量维度 = {len(np.asarray(fn(['维度探测'])[0]))}")
    return best_rate


def extrapolate(rate_per_s: float) -> None:
    print("\n=== 4. 换算成论文 ===")
    if rate_per_s <= 0:
        print("  无有效吞吐数据")
        return
    # chunk_text(): 1000 chars with 150 overlap -> ~850 chars of new text per chunk.
    step = 1000 - 150
    for label, chars in (("短篇 ~20k 字符", 20_000), ("典型 ~40k 字符", 40_000), ("20 页上限 ~60k 字符", 60_000)):
        chunks = max(1, round(chars / step))
        sec = chunks / rate_per_s
        print(f"  {label:<18}≈ {chunks:>3} 块/篇 → 约 {sec:>5.1f} 秒/篇 "
              f"（{3600 / sec:>6.0f} 篇/小时）")
    print(f"\n  1 万篇典型论文 ≈ {(40_000 / step) * 10_000 / rate_per_s / 3600:.1f} 小时（纯嵌入，不含解析）")


def main() -> None:
    report_model()
    tokenize = load_tokenizer()
    token_report(tokenize)
    rate = throughput()
    extrapolate(rate)


if __name__ == "__main__":
    main()

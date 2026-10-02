"""扫描 cross-encoder 重排的参数组合，找出延迟与召回质量的最佳平衡点。

重排目前占整条检索链路约 95% 的时间（20 个候选约 1076 ms，而召回只要 43 ms）。
两个可调杠杆：

* ``candidate_limit`` —— 送进重排的候选条数（线性影响）
* ``max_length``      —— cross-encoder 的输入截断长度（近似平方影响，
                         因为 self-attention 随序列长度二次增长）

用法：
    python -m scripts.bench_reranker
    python -m scripts.bench_reranker --candidates 20,12,8 --max-lengths 512,256
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

QUERIES = [
    "What benchmark does the paper evaluate?",
    "How is the loss function defined?",
    "transformer self-attention mechanism",
    "branch and bound pruning strategy",
    "What dataset is used for evaluation?",
    "latency analysis of traffic shaping",
]


def _make_store(config):
    """One store for the whole sweep.

    Rebuilding it per configuration also re-loads the embedding model and the
    ChromaDB connection, which swamped the signal on a noisy host.  Only the
    cross-encoder differs between configurations, so only that is swapped.
    """
    from paper_store import PaperStore

    return PaperStore(
        persist_dir=Path(config.chroma_dir),
        collection_name=config.rag_embedding_collection,
        embedding_max_length=config.rag_embedding_max_length,
        reranker_enabled=False,
    )


def _rerankers(config, max_lengths):
    from sentence_transformers import CrossEncoder

    return {
        value: CrossEncoder(
            config.rag_reranker_model, max_length=value, local_files_only=True
        )
        for value in max_lengths
    }


def measure(store, queries, top_k: int = 3) -> float:
    """Median latency of one pass over every query."""
    samples = []
    for query in queries:
        started = time.perf_counter()
        store.query_hybrid(query, top_k)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="20,12,8,5")
    parser.add_argument("--max-lengths", default="512,384,256")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=3, help="轮数，配置间交叉执行")
    args = parser.parse_args()

    from config import Config

    config = Config.load()
    print(f"集合 {config.rag_embedding_collection} | 模型 {config.rag_reranker_model}")
    print(f"块数 {_chunk_count(config)}")
    print()

    limits = [int(v) for v in args.candidates.split(",")]
    max_lengths = [int(v) for v in args.max_lengths.split(",")]
    store = _make_store(config)
    rerankers = _rerankers(config, max_lengths)

    # Interleave configurations across rounds so a host-level hiccup lands on
    # every configuration instead of skewing one of them.
    results: dict[tuple[int, int], list[float]] = {}
    for _ in range(args.rounds):
        for limit in limits:
            for max_length in max_lengths:
                store._reranker_enabled = True
                store._reranker = rerankers[max_length]
                store._reranker_load_attempted = True
                store._reranker_candidate_limit = limit
                results.setdefault((limit, max_length), []).append(
                    measure(store, QUERIES, top_k=args.top_k)
                )

    print(f"{'候选数':>7}{'截断长度':>10}{'中位耗时':>12}{'各轮':>26}")
    for limit in limits:
        for max_length in max_lengths:
            samples = sorted(results[(limit, max_length)])
            median = samples[len(samples) // 2]
            spread = " ".join(f"{value:.0f}" for value in samples)
            print(f"{limit:>7}{max_length:>10}{median:>11.0f}ms{spread:>26}")

    print()
    store._reranker_enabled = False
    store._reranker = None
    plain = [measure(store, QUERIES, top_k=args.top_k) for _ in range(args.rounds)]
    plain.sort()
    print(f"{'对照：关闭重排':>17}{plain[len(plain) // 2]:>11.0f}ms"
          f"{' '.join(f'{v:.0f}' for v in plain):>26}")
    return 0


def _chunk_count(config) -> int:
    from paper_store import PaperStore

    store = PaperStore(
        persist_dir=Path(config.chroma_dir),
        collection_name=config.rag_embedding_collection,
        embedding_max_length=config.rag_embedding_max_length,
        reranker_enabled=False,
    )
    return store.chunk_count


if __name__ == "__main__":
    raise SystemExit(main())

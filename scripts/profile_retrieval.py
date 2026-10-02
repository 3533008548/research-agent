"""拆分真实语料上 RAG 检索的耗时构成。

猜瓶颈不如量瓶颈：本脚本分别给 semantic / lexical / hybrid 计时，
并把两者各自的内部阶段（查询编码、向量检索、全库 get、分词、打分）拆开，
输出 mean / p50 / p95。

⚠️ 默认走**生产配置**（集合名、是否重排、重排预算都从 config.yaml 读）。
   这不是可选项：config.yaml 里 reranker.enabled=true，而 PaperStore 的
   reranker_enabled 默认是 False。曾经因为没显式传参，测出 hybrid 只要 50 ms
   的假数字，线上实际是 1127 ms —— 重排占了 95%。
   想看"不重排"的对照，显式加 --no-reranker，别靠默认值。

用法（容器内）：
    python -m scripts.profile_retrieval --repeat 2
    python -m scripts.profile_retrieval --no-reranker        # 对照组
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path


def _idf(document_count: int, df: int) -> float:
    return math.log(1 + (document_count - df + 0.5) / (df + 0.5))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def _fmt(values_ms: list[float]) -> str:
    if not values_ms:
        return "n/a"
    return (
        f"mean {statistics.mean(values_ms):7.1f} ms | "
        f"p50 {_pct(values_ms, 0.5):7.1f} ms | p95 {_pct(values_ms, 0.95):7.1f} ms"
    )


def build_parser() -> argparse.ArgumentParser:
    """Exposed so tests can assert the defaults follow config.yaml."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default=None,
                        help="默认用 config.yaml 的 rag.embedding.collection")
    parser.add_argument("--cases", default="evals/fixtures/rag_retrieval_cases_en.json")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-reranker", action="store_true",
                        help="关闭重排做对照组（默认跟随 config.yaml）")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from config import Config
    from paper_store import PaperStore

    config = Config.load()
    collection = args.collection or config.rag_embedding_collection
    persist_dir = Path(config.chroma_dir or "runtime/derived/chroma")

    # 必须显式传：PaperStore 的默认值是"不重排"，跟生产配置不一致。
    reranker_enabled = config.rag_reranker_enabled and not args.no_reranker

    cases_path = PROJECT_ROOT / args.cases
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    queries = [str(c.get("query", "")).strip() for c in cases]
    queries = [q for q in queries if q]
    print(f"语料集合: {collection}  查询数: {len(queries)}  重复: {args.repeat}")
    if reranker_enabled:
        print(
            f"重排: 启用  候选数 {config.rag_reranker_candidate_limit}  "
            f"截断 {config.rag_reranker_max_length}"
        )
    else:
        print("重排: 关闭（对照组）")

    store = PaperStore(
        persist_dir=persist_dir,
        collection_name=collection,
        embedding_model=config.rag_embedding_model,
        embedding_dimensions=config.rag_embedding_dimensions,
        embedding_max_length=config.rag_embedding_max_length,
        embedding_batch_size=config.rag_embedding_batch_size,
        embedding_device=config.rag_embedding_device,
        reranker_enabled=reranker_enabled,
        reranker_model=config.rag_reranker_model,
        reranker_candidate_limit=config.rag_reranker_candidate_limit,
        reranker_max_length=config.rag_reranker_max_length,
    )
    total = store._collection.count()
    print(f"块数: {total}")

    # 预热：首次编码 / 首次全量拉取都要计入冷启动之外的稳态
    store.query_hybrid(queries[0], args.top_k)
    print("预热完成，开始计时\n")

    timings: dict[str, list[float]] = {
        "semantic": [], "lexical": [], "hybrid": [],
        "lex_get": [], "lex_build": [], "lex_tokenize": [], "lex_score": [],
    }

    for _ in range(args.repeat):
        for query in queries:
            t0 = time.perf_counter()
            store.query(query, args.top_k)
            t1 = time.perf_counter()
            timings["semantic"].append((t1 - t0) * 1000)

            t0 = time.perf_counter()
            store.query_lexical(query, args.top_k)
            t1 = time.perf_counter()
            timings["lexical"].append((t1 - t0) * 1000)

            t0 = time.perf_counter()
            store.query_hybrid(query, args.top_k)
            t1 = time.perf_counter()
            timings["hybrid"].append((t1 - t0) * 1000)

    # 拆开 lexical 内部：collection.get / 构造 candidate / 分词 / 打分
    for query in queries:
        terms = store._query_terms(query)
        if not terms:
            continue

        t0 = time.perf_counter()
        raw = store._collection.get(include=["documents", "metadatas"])
        t1 = time.perf_counter()
        timings["lex_get"].append((t1 - t0) * 1000)

        docs = raw.get("documents") or []
        metas = raw.get("metadatas") or []
        t0 = time.perf_counter()
        candidates = [
            {
                "text": doc or "",
                "title": (meta or {}).get("title", "Unknown"),
                "chunk_index": (meta or {}).get("chunk_index", 0),
            }
            for doc, meta in zip(docs, metas)
        ]
        t1 = time.perf_counter()
        timings["lex_build"].append((t1 - t0) * 1000)

        t0 = time.perf_counter()
        tokenized = [store._lexical_tokens(item["text"]) for item in candidates]
        t1 = time.perf_counter()
        timings["lex_tokenize"].append((t1 - t0) * 1000)

        t0 = time.perf_counter()
        document_count = len(tokenized)
        average_length = sum(len(tokens) for tokens in tokenized) / max(document_count, 1)
        df = {term: sum(term in set(tokens) for tokens in tokenized) for term in terms}
        for tokens in tokenized:
            length = len(tokens)
            score = 0.0
            for term in terms:
                frequency = tokens.count(term)
                if not frequency:
                    continue
                idf = _idf(document_count, df[term])
                score += idf * frequency * 2.5 / (
                    frequency + 1.5 * (1 - 0.75 + 0.75 * length / average_length)
                )
        t1 = time.perf_counter()
        timings["lex_score"].append((t1 - t0) * 1000)

    print("=" * 72)
    print(f"{'阶段':<14}{'耗时'}")
    print("=" * 72)
    for key in ("semantic", "lexical", "hybrid"):
        print(f"{key:<14}{_fmt(timings[key])}")
    print("-" * 72)
    print("lexical 内部拆分：")
    for key in ("lex_get", "lex_build", "lex_tokenize", "lex_score"):
        print(f"  {key:<12}{_fmt(timings[key])}")

    # ── semantic 下钻：查询编码 vs Chroma 向量检索 ──
    sem_embed: list[float] = []
    sem_search: list[float] = []
    for query in queries:
        t0 = time.perf_counter()
        try:
            store._embed_fn.embed_query([query]) if hasattr(
                store._embed_fn, "embed_query"
            ) else store._embed_fn([query])
        except Exception as exc:  # noqa: BLE001
            print(f"  查询编码失败: {type(exc).__name__}: {exc}")
            break
        t1 = time.perf_counter()
        sem_embed.append((t1 - t0) * 1000)

        t0 = time.perf_counter()
        store._collection.query(
            query_texts=[query],
            n_results=min(total, args.top_k * 3),
            include=["documents", "metadatas", "distances"],
        )
        t1 = time.perf_counter()
        sem_search.append((t1 - t0) * 1000)

    print("-" * 72)
    print("semantic 内部拆分：")
    print(f"  {'查询编码':<12}{_fmt(sem_embed)}")
    print(f"  {'向量检索':<12}{_fmt(sem_search)}")
    print(f"  嵌入后端: {getattr(store, 'embedding_model_name', '?')}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

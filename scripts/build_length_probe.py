"""用指定的 token 上限重建一个探针集合，用于对比"块大小"对召回的影响。

默认不动线上集合（config.yaml 指向哪个就是哪个）。建好探针后
用 scripts/run_rag_retrieval_eval.py --collection <探针名> 评测。

用法：
    python -m scripts.build_length_probe --max-length 512 \
        --collection papers_minilm512_probe
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-length", type=int, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 篇")
    parser.add_argument(
        "--match-collection",
        help="只索引该集合中已有的论文，保证与对照集合 1:1 可比",
    )
    args = parser.parse_args()

    from config import Config
    from embeddings import build_embedder
    from paper_store import PaperStore

    config = Config.load()
    embedder = build_embedder(
        config.rag_embedding_model,
        dimensions=config.rag_embedding_dimensions,
        max_length=args.max_length,
        batch_size=config.rag_embedding_batch_size,
        device=config.rag_embedding_device,
    )
    store = PaperStore(
        persist_dir=Path(config.chroma_dir),
        collection_name=args.collection,
        embedder=embedder,
        reranker_enabled=False,
    )
    print(f"集合 {args.collection} | 模型 {store.embedding_model_name} "
          f"| token 上限 {store.embedding_max_length}")

    patterns = str(Path(config.data_dir) / "derived" / "paper_artifacts" / "*" / "document_map.json")
    files = sorted(glob.glob(patterns))

    allowed: set[str] | None = None
    if args.match_collection:
        reference = PaperStore(
            persist_dir=Path(config.chroma_dir), collection_name=args.match_collection
        )
        allowed = {str(item.get("paper_id") or "") for item in reference.list_papers()}
        print(f"对齐集合 {args.match_collection}：{len(allowed)} 篇")

    if args.limit:
        files = files[:args.limit]
    print(f"待索引 {len(files)} 篇")

    started = time.perf_counter()
    total_chunks = 0
    for index, path in enumerate(files, 1):
        document_map = json.loads(Path(path).read_text(encoding="utf-8"))
        paper_id = str(document_map.get("paper_id") or "")
        if not paper_id:
            print(f"  [skip] 无 paper_id: {path}")
            continue
        if allowed is not None and paper_id not in allowed:
            continue
        before = store.chunk_count
        store.index_document_map(
            document_map, title=str(document_map.get("title") or "Unknown"), paper_id=paper_id
        )
        added = store.chunk_count - before
        total_chunks += added
        print(f"  [{index}/{len(files)}] {str(document_map.get('title'))[:48]:<50} {added:>4} 块",
              flush=True)

    elapsed = time.perf_counter() - started
    print(f"\n完成: {total_chunks} 块，用时 {elapsed:.1f} 秒（{total_chunks / max(elapsed, 1e-9):.1f} 块/秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

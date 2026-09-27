"""Rebuild the paper vector collection for a new embedding model or chunking.

Why this can reuse stored text instead of re-parsing PDFs: truncation happens
at *encode* time, not at storage time.  The documents kept in Chroma are the
full chunk texts, so a migration only has to re-split them against the target
model's token ceiling and re-encode.

The source collection is never modified, so it stays a rollback backup: point
``rag.embedding.collection`` back at it if the new one misbehaves.

Usage:
    # 同模型，只修掉 token 截断
    python scripts/migrate_embedding.py --target papers_v2

    # 换成 Qwen3（需要能访问模型仓库）
    python scripts/migrate_embedding.py --target papers_v2 \
        --model Qwen/Qwen3-Embedding-0.6B --dimensions 1024 --max-length 8192
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from embeddings import build_embedder  # noqa: E402
from paper_store import DEFAULT_COLLECTION_NAME, PaperStore  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.getenv("APP_DATA_DIR", "runtime"),
                        help="运行时目录，默认读取环境变量 APP_DATA_DIR")
    parser.add_argument("--source", default=DEFAULT_COLLECTION_NAME, help="源集合名（保留作备份）")
    parser.add_argument("--target", default="papers_v2", help="目标集合名")
    parser.add_argument("--model", default="", help="目标嵌入模型；留空沿用配置")
    parser.add_argument("--dimensions", type=int, default=0, help="目标维度；0 用模型原生维度")
    parser.add_argument("--max-length", type=int, default=0, help="目标 token 上限；0 用模型默认")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 篇，便于试跑")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="跳过目标集合里已存在的论文，用于中断后续跑（否则会重算一遍）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from runtime_paths import RuntimePaths

    paths = RuntimePaths.from_root(args.data_dir)
    paths.ensure_initialized()
    chroma_dir = str(paths.chroma_dir)

    source = PaperStore(persist_dir=chroma_dir, collection_name=args.source)
    papers = source.list_papers()
    if args.limit:
        papers = papers[: args.limit]
    if not papers:
        print(f"源集合 {args.source!r} 没有论文，退出")
        return 1

    embedder = None
    if args.model:
        embedder = build_embedder(
            args.model,
            dimensions=args.dimensions,
            max_length=args.max_length,
            device=args.device,
        )
        print(f"目标模型: {embedder.model_name} "
              f"({embedder.dimensions} 维 / {embedder.max_length} token)")
    else:
        probe = build_embedder(
            args.model,
            dimensions=args.dimensions,
            max_length=args.max_length,
            device=args.device,
        )
        print(f"目标模型(沿用配置): {probe.model_name} "
              f"({probe.dimensions} 维 / {probe.max_length} token)")

    target = PaperStore(
        persist_dir=chroma_dir,
        collection_name=args.target,
        embedder=embedder,
        embedding_dimensions=args.dimensions,
        embedding_max_length=args.max_length,
        embedding_device=args.device,
    )

    existing_ids = set()
    if args.skip_existing:
        existing_ids = {str(item.get("paper_id") or "") for item in target.list_papers()}

    print(f"源集合: {args.source} ({source.paper_count} 篇 / {source.chunk_count} 块)")
    print(f"目标集合: {args.target}")
    print("-" * 60)

    started = time.perf_counter()
    total_chunks = 0
    for index, paper in enumerate(papers, start=1):
        paper_id = str(paper.get("paper_id") or "")
        title = str(paper.get("title") or "Unknown")
        if paper_id in existing_ids:
            print(f"  [skip] {title[:52]:<54} 已在目标集合中", flush=True)
            continue
        chunks = source.get_paper_chunks(paper_id)
        if not chunks:
            continue
        target.index_chunks(chunks, title=title, paper_id=paper_id)
        total_chunks += len(chunks)
        elapsed = time.perf_counter() - started
        rate = index / elapsed if elapsed > 0 else 0
        print(f"  [{index}/{len(papers)}] {title[:42]:<44} "
              f"{len(chunks):>4} 块  ({rate:.1f} 篇/秒)", flush=True)

    elapsed = time.perf_counter() - started
    migrated = len(papers) - len(existing_ids)
    print("-" * 60)
    print(f"完成: 本次迁移 {migrated} 篇 / {total_chunks} 块，用时 {elapsed:.1f} 秒")
    print(f"目标集合现有: {target.paper_count} 篇 / {target.chunk_count} 块")
    print()
    print("验证建议:")
    print(f"  1) python scripts/audit_chunks.py   # 确认截断归零")
    print(f"  2) 把 config.yaml 的 rag.embedding.collection 改为 {args.target} 后重启")
    print(f"  3) 出问题就改回 {args.source}（源集合未被修改）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

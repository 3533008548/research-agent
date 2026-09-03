"""Rebuild the local Chroma entries from managed PDF files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from paper_store import PaperStore
from pdf_reader import PaperReader
from runtime_paths import RuntimePaths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.getenv("APP_DATA_DIR", "runtime"))
    parser.add_argument("--max-pages", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_pages < 1:
        parser = build_parser()
        parser.error("--max-pages 必须大于 0")
    paths = RuntimePaths.from_root(args.data_dir)
    paths.ensure_initialized()
    pdf_files = sorted(paths.papers_dir.glob("*.pdf"))
    if not pdf_files:
        print("没有可重建的本地 PDF。")
        return 0

    store = PaperStore(persist_dir=str(paths.chroma_dir))
    reader = PaperReader(max_pages=args.max_pages, max_chars=None)
    existing = {item["title"]: item["paper_id"] for item in store.list_papers()}
    failures = []
    try:
        for pdf_path in pdf_files:
            title = pdf_path.stem.replace("_", " ")
            text = reader.read(str(pdf_path))
            if text.startswith("❌"):
                failures.append(f"{pdf_path.name}: {text}")
                continue
            paper_id = existing.get(title)
            if paper_id:
                store.delete_paper(paper_id)
            paper_id = store.index_paper(text, title=title, paper_id=paper_id)
            print(f"已重建: {title} ({paper_id})")
    finally:
        store._query_executor.shutdown(wait=False, cancel_futures=True)

    if failures:
        print("重建失败：", *failures, sep="\n", file=sys.stderr)
        return 1
    print(f"完成：{len(pdf_files)} 篇，{store.chunk_count} 个块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

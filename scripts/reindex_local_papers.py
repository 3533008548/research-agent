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

from paper_artifacts import attach_image_assets, build_document_map, remove_document_map, write_document_map
from paper_quality import prepare_document_map, quality_failure_summary, verify_index_round_trip
from paper_store import PaperStore
from pdf_reader import PaperReader, extract_images
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
    existing = {}
    for item in store.list_papers():
        existing.setdefault(str(item["title"]), []).append(str(item["paper_id"]))
    failures = []
    try:
        for pdf_path in pdf_files:
            title = pdf_path.stem.replace("_", " ")
            from uuid import uuid4

            paper_id = f"paper_{uuid4().hex[:12]}"
            selected = None
            attempt_failures = []
            for label, table_aware in (("表格感知解析", True), ("备用文本解析", False)):
                try:
                    reader = PaperReader(max_pages=args.max_pages, max_chars=None)
                    parsed_document = (
                        reader.parse_document(str(pdf_path))
                        if table_aware else reader.parse_document(str(pdf_path), extract_tables=False)
                    )
                    document_map = build_document_map(
                        parsed_document, paper_id=paper_id, title=title, source_file=pdf_path.name,
                    )
                    document_map, quality = prepare_document_map(
                        document_map, requested_pages=args.max_pages,
                    )
                except Exception as exc:
                    attempt_failures.append(f"{label}: {type(exc).__name__}: {exc}")
                    continue
                if quality["accepted"]:
                    selected = (parsed_document, document_map, quality, label)
                    break
                attempt_failures.append(f"{label}: {quality_failure_summary(quality)}")
            if selected is None:
                failures.append(f"{pdf_path.name}: 切块质量检查失败（{'；'.join(attempt_failures[:2])}）")
                continue

            parsed_document, document_map, quality, parser_label = selected
            try:
                images = extract_images(
                    str(pdf_path), max_pages=args.max_pages, output_dir=paths.images_dir,
                )
                attach_image_assets(parsed_document, images)
            except Exception:
                # A PDF can still have useful text and tables when image rendering fails.
                pass
            document_map = build_document_map(
                parsed_document, paper_id=paper_id, title=title, source_file=pdf_path.name,
            )
            document_map, quality = prepare_document_map(
                document_map, requested_pages=args.max_pages,
            )
            quality["attempts"] = 2 if parser_label == "备用文本解析" else 1
            quality["parser"] = parser_label
            try:
                indexed_id = store.index_document_map(document_map, title=title, paper_id=paper_id)
                if indexed_id != paper_id:
                    raise RuntimeError("向量库未返回新论文 ID")
                index_check = verify_index_round_trip(
                    document_map, store.get_paper_chunks(paper_id),
                )
                quality["index_round_trip"] = index_check
                if not index_check["accepted"]:
                    store.delete_paper(paper_id)
                    raise RuntimeError("向量库入库后自检失败，已回滚新索引")
                write_document_map(paths, paper_id=paper_id, document_map=document_map)
                for existing_id in existing.get(title, []):
                    if existing_id != paper_id:
                        store.delete_paper(existing_id)
                        remove_document_map(paths, existing_id)
                print(f"已重建: {title} ({paper_id})，{quality['status']}")
            except Exception as exc:
                try:
                    store.delete_paper(paper_id)
                except Exception:
                    pass
                failures.append(f"{pdf_path.name}: 入库自检失败（{type(exc).__name__}: {exc}）")
    finally:
        store._query_executor.shutdown(wait=False, cancel_futures=True)

    if failures:
        print("重建失败：", *failures, sep="\n", file=sys.stderr)
        return 1
    print(f"完成：{len(pdf_files)} 篇，{store.chunk_count} 个块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

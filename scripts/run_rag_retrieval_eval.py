"""Run a human-labelled local RAG recall evaluation without invoking an LLM."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from evals.rag_retrieval import (
    RagEvalError,
    evaluate_paper_store,
    load_cases,
    parse_ks,
    summary_lines,
    write_report,
)

def _default_report_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "evals" / "reports" / f"rag-retrieval-{timestamp}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, help="人工核验的 RAG 金标 JSON 文件")
    parser.add_argument(
        "--mode",
        choices=("timed_hybrid", "hybrid", "keyword"),
        default="timed_hybrid",
        help="timed_hybrid 与网页 query_papers 的正常路径一致（默认）",
    )
    parser.add_argument("--ks", default="1,3,5", help="统计的 k，例如 1,3,5")
    parser.add_argument("--timeout-seconds", type=float, default=8.0, help="timed_hybrid 的前台等待上限")
    parser.add_argument("--reranker", action="store_true", help="启用本地 cross-encoder 重排后评测")
    parser.add_argument("--reranker-model", help="覆盖默认的本地重排模型")
    parser.add_argument("--data-dir", help="运行时根目录；默认使用当前 APP_DATA_DIR/runtime")
    parser.add_argument("--output", help="报告 JSON 路径；默认写入被 Git 忽略的 evals/reports/")
    parser.add_argument("--no-write", action="store_true", help="只打印摘要，不保存报告")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cases = load_cases(args.cases)
        ks = parse_ks(args.ks)
        if args.timeout_seconds <= 0:
            raise RagEvalError("timeout-seconds 必须大于 0")
        from paper_store import PaperStore

        persist_dir = None
        if args.data_dir:
            persist_dir = Path(args.data_dir).expanduser().resolve() / "derived" / "chroma"
        store = PaperStore(
            persist_dir=str(persist_dir) if persist_dir else None,
            reranker_enabled=args.reranker,
            reranker_model=args.reranker_model or "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        )
        try:
            if args.reranker and store._get_reranker() is None:
                raise RagEvalError("重排模型尚未下载；请先运行 scripts/warm_reranker.py")
            report = evaluate_paper_store(
                store,
                cases,
                ks=ks,
                mode=args.mode,
                timeout_seconds=args.timeout_seconds,
            )
        finally:
            store._query_executor.shutdown(wait=True, cancel_futures=True)
    except (RagEvalError, ImportError) as exc:
        print(f"❌ RAG 召回评测无法运行：{exc}", file=sys.stderr)
        return 2

    print("\n".join(summary_lines(report)))
    if not args.no_write:
        output = write_report(report, args.output or _default_report_path())
        print(f"报告: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

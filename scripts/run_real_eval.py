"""Run a small real-model evaluation sample without contaminating app sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.real_eval import DEFAULT_REPORTS_DIR, run_real_evaluation


def main() -> int:
    parser = argparse.ArgumentParser(description="在隔离运行时执行小样本真实模型评测")
    parser.add_argument("--task", action="append", default=[], help="任务 ID；可重复，例如 --task T04 --task T13")
    parser.add_argument("--data-dir", help="保留的评测运行时目录；必须与 --keep-runtime 同用")
    parser.add_argument("--keep-runtime", action="store_true", help="保留评测运行时供复盘")
    parser.add_argument(
        "--allow-production-runtime",
        action="store_true",
        help="危险：仅与 --keep-runtime 同用时允许主 runtime，通常不应使用",
    )
    parser.add_argument("--with-rag", action="store_true", help="在隔离运行时启用 RAG（默认关闭）")
    parser.add_argument("--research-scope", choices=("both", "local", "public"), default="public")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="脱敏报告输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只校验任务和运行时隔离策略，不调用模型")
    args = parser.parse_args()

    if args.allow_production_runtime and not args.keep_runtime:
        parser.error("--allow-production-runtime 必须与 --keep-runtime 同用")
    try:
        summary = run_real_evaluation(
            args.task,
            data_dir=args.data_dir,
            keep_runtime=args.keep_runtime,
            allow_production_runtime=args.allow_production_runtime,
            with_rag=args.with_rag,
            research_scope=args.research_scope,
            reports_dir=args.reports_dir,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

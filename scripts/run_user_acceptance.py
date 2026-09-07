"""Run versioned real-user acceptance journeys in an isolated runtime."""

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

from evals.real_eval import DEFAULT_REPORTS_DIR
from evals.user_acceptance import run_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description="在隔离运行时执行真实用户验收集")
    parser.add_argument("--scenario", action="append", default=[], help="验收场景 ID；可重复，例如 --scenario UA01")
    parser.add_argument("--suite", action="append", default=[], help="验收分组；可重复，例如 --suite core")
    parser.add_argument("--data-dir", help="保留的验收运行时目录；必须与 --keep-runtime 同用")
    parser.add_argument("--keep-runtime", action="store_true", help="保留验收运行时供复盘")
    parser.add_argument(
        "--allow-production-runtime",
        action="store_true",
        help="危险：仅与 --keep-runtime 同用时允许主 runtime，通常不应使用",
    )
    parser.add_argument("--with-rag", action="store_true", help="在隔离运行时启用 RAG（默认关闭）")
    parser.add_argument("--research-scope", choices=("both", "local", "public"), default="public")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="脱敏报告输出目录")
    parser.add_argument("--compare", help="上一份同版本验收报告，输出汇总和人工评分变化")
    parser.add_argument("--dry-run", action="store_true", help="只校验验收集与隔离策略，不调用模型或外部来源")
    args = parser.parse_args()

    if args.allow_production_runtime and not args.keep_runtime:
        parser.error("--allow-production-runtime 必须与 --keep-runtime 同用")
    try:
        summary = run_acceptance(
            args.scenario,
            suites=args.suite,
            data_dir=args.data_dir,
            keep_runtime=args.keep_runtime,
            allow_production_runtime=args.allow_production_runtime,
            with_rag=args.with_rag,
            research_scope=args.research_scope,
            reports_dir=args.reports_dir,
            compare_path=args.compare,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

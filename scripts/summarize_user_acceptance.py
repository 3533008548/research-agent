"""Validate completed manual review scores and write a refreshed safe report."""

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
from evals.user_acceptance import compare_reports, refresh_review_summary, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总真实用户验收报告中的人工评分")
    parser.add_argument("report", help="已填写 manual_review.score 的验收报告 JSON")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="更新报告输出目录")
    parser.add_argument("--compare", help="上一份同版本、已人工评分的验收报告")
    args = parser.parse_args()

    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        refreshed = refresh_review_summary(report)
        if args.compare:
            previous = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            refreshed["comparison"] = compare_reports(refreshed, previous)
        path = write_report(refreshed, args.reports_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "report_path": str(path),
        "summary": refreshed["summary"],
        "comparison": refreshed.get("comparison"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

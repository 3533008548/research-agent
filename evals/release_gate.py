"""One offline quality gate for a releasable Research Agent revision.

This composes the versioned capability scorer with the real orchestration
replays.  It deliberately uses synthetic results and transport fakes only:
running it must never call a model, fetch papers, or read user runtime data.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .benchmark import (
    ROOT as EVALS_ROOT,
    load_manifest,
    score_submission,
    validate_manifest,
)
from .runtime_replay import run_suite


GATE_VERSION = "release-gate-v1"
DEFAULT_RESULTS_PATH = EVALS_ROOT / "example_results.json"
_COMPARABLE_FIELDS = (
    "success_rate",
    "capability_success_rate",
    "runtime_success_rate",
    "hard_check_pass_rate",
    "runtime_p95_duration_ms",
)


def _compact_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": report.get("benchmark_version"),
        "passed": report.get("passed", 0),
        "total": report.get("total", 0),
        "summary": report.get("summary", {}),
        "failed_task_ids": [
            str(task.get("task_id"))
            for task in report.get("tasks", [])
            if not task.get("passed")
        ],
    }


def _compact_replay(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": report.get("suite_version"),
        "passed": report.get("passed", 0),
        "total": report.get("total", 0),
        "summary": report.get("summary", {}),
        "failed_case_ids": [
            str(case.get("case_id"))
            for case in report.get("cases", [])
            if not case.get("passed")
        ],
    }


def build_report(
    manifest_errors: list[str],
    benchmark_report: dict[str, Any],
    replay_report: dict[str, Any],
) -> dict[str, Any]:
    """Build a compact, comparison-safe report from the two offline suites."""
    benchmark = _compact_benchmark(benchmark_report)
    replay = _compact_replay(replay_report)
    checks = [
        {
            "name": "capability_manifest",
            "passed": not manifest_errors,
            "details": manifest_errors,
        },
        {
            "name": "capability_benchmark",
            "passed": benchmark["passed"] == benchmark["total"] and benchmark["total"] > 0,
            "details": benchmark["failed_task_ids"],
        },
        {
            "name": "runtime_replay",
            "passed": replay["passed"] == replay["total"] and replay["total"] > 0,
            "details": replay["failed_case_ids"],
        },
    ]
    total = int(benchmark["total"]) + int(replay["total"])
    passed = int(benchmark["passed"]) + int(replay["passed"])
    benchmark_summary = benchmark.get("summary") or {}
    replay_summary = replay.get("summary") or {}
    return {
        "gate_version": GATE_VERSION,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "summary": {
            "success_rate": round(passed / total, 4) if total else 0.0,
            "capability_success_rate": benchmark_summary.get("success_rate", 0.0),
            "runtime_success_rate": replay_summary.get("success_rate", 0.0),
            "hard_check_pass_rate": benchmark_summary.get("hard_check_pass_rate"),
            "runtime_p95_duration_ms": replay_summary.get("p95_duration_ms"),
        },
        "capability": benchmark,
        "runtime": replay,
    }


def run_release_gate(
    results_path: str | Path = DEFAULT_RESULTS_PATH,
    *,
    seed: int = 42,
) -> dict[str, Any]:
    """Run both suites against a supplied, non-sensitive captured result file."""
    manifest = load_manifest()
    manifest_errors = validate_manifest(manifest)
    try:
        raw_results = json.loads(Path(results_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取评测结果文件：{exc}") from exc
    if not isinstance(raw_results, list):
        raise ValueError("评测结果文件必须是 JSON 数组")
    benchmark_report = score_submission(manifest, raw_results)
    replay_report = run_suite(seed=seed)
    return build_report(manifest_errors, benchmark_report, replay_report)


def compare_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Compare the small set of release-level metrics across matching gates."""
    if previous.get("gate_version") != GATE_VERSION:
        raise ValueError("comparison report belongs to a different release gate version")
    now_summary = current.get("summary") or {}
    before_summary = previous.get("summary") or {}
    deltas = {}
    for field in _COMPARABLE_FIELDS:
        now, before = now_summary.get(field), before_summary.get(field)
        if isinstance(now, (int, float)) and isinstance(before, (int, float)):
            deltas[field] = round(now - before, 4)
    return {
        "baseline_generated_at": previous.get("generated_at"),
        "baseline_version": previous.get("gate_version"),
        "deltas": deltas,
    }


def write_report(report: dict[str, Any], reports_dir: str | Path) -> Path:
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = directory / timestamp.strftime("release-gate-%Y%m%dT%H%M%SZ.json")
    path.write_text(
        json.dumps({"generated_at": timestamp.isoformat(), **report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline Research Agent release quality gate")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS_PATH))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--strict", action="store_true", help="Return non-zero when any gate fails")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--reports-dir", default=str(EVALS_ROOT / "reports"))
    parser.add_argument("--compare", help="Previous release-gate JSON report")
    args = parser.parse_args()

    try:
        report = run_release_gate(args.results, seed=args.seed)
        if args.compare:
            previous = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            report["comparison"] = compare_reports(report, previous)
    except ValueError as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    if args.write_report:
        report["report_path"] = str(write_report(report, args.reports_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.strict and not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())

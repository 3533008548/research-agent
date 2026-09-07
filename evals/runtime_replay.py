"""Command-line runner for isolated, offline reliability replays.

This suite complements the capability benchmark.  It executes real
orchestration and SQLite persistence, but never contacts a model or an
academic provider.  Results contain only assertions and compact stage events.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime_cases import CASES, RuntimeCaseResult, RuntimeContext, select_cases


ROOT = Path(__file__).resolve().parent
SUITE_VERSION = "runtime-replay-v1"
_COMPARABLE_FIELDS = (
    "success_rate",
    "average_duration_ms",
    "p95_duration_ms",
    "total_model_calls",
    "source_failure_rate",
)


def _git_revision() -> str:
    injected = os.getenv("APP_GIT_REVISION", "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", injected):
        return injected.lower()
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT.parent,
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * percentile) - 1)]


def _source_health(results: list[RuntimeCaseResult]) -> dict[str, int | float]:
    requested = 0
    failed = 0
    for result in results:
        for keyword_stats in result.source_stats.values():
            if not isinstance(keyword_stats, dict):
                continue
            values = [keyword_stats] if "status" in keyword_stats else keyword_stats.values()
            for detail in values:
                if not isinstance(detail, dict) or "status" not in detail:
                    continue
                requested += 1
                failed += int(detail.get("status") != "ok")
    return {
        "source_requests": requested,
        "source_failures": failed,
        "source_failure_rate": round(failed / requested, 4) if requested else 0.0,
    }


def summarize(results: list[RuntimeCaseResult]) -> dict[str, int | float | None]:
    durations = [result.duration_ms for result in results]
    model_calls = [int(result.metrics.get("model_calls", 0)) for result in results]
    passed = sum(result.passed for result in results)
    return {
        "success_rate": round(passed / len(results), 4) if results else 0.0,
        "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else None,
        "p95_duration_ms": _percentile(durations, 0.95),
        "total_model_calls": sum(model_calls),
        **_source_health(results),
    }


def run_suite(*, case_ids: list[str] | None = None, seed: int = 42) -> dict[str, Any]:
    """Run selected cases in fresh temporary directories and return a safe report."""
    selected = select_cases(case_ids)
    results: list[RuntimeCaseResult] = []
    fixtures_dir = ROOT / "fixtures" / "providers"
    with tempfile.TemporaryDirectory(prefix="research-agent-runtime-replay-") as temp_root:
        root = Path(temp_root)
        for index, case in enumerate(selected):
            context = RuntimeContext(
                work_dir=root / case.case_id,
                fixtures_dir=fixtures_dir,
                seed=seed + index,
            )
            context.work_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = case.execute(context)
            except Exception as exc:
                result = RuntimeCaseResult(
                    case_id=case.case_id,
                    title=case.title,
                    assertions=[{"label": "replay completes without an unhandled exception", "passed": False}],
                    duration_ms=0.0,
                    events=list(context.events),
                    error_type=type(exc).__name__,
                )
            results.append(result)
    passed = sum(result.passed for result in results)
    return {
        "suite_version": SUITE_VERSION,
        "passed": passed,
        "total": len(results),
        "summary": summarize(results),
        "cases": [result.to_dict() for result in results],
        "metadata": {
            "seed": seed,
            "selected_cases": [case.case_id for case in selected],
            "python": platform.python_version(),
            "platform": platform.system(),
            "git_revision": _git_revision(),
        },
    }


def compare_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    if previous.get("suite_version") != SUITE_VERSION:
        raise ValueError("comparison report belongs to a different runtime replay suite version")
    current_summary = current.get("summary") or {}
    previous_summary = previous.get("summary") or {}
    deltas = {}
    for field in _COMPARABLE_FIELDS:
        now, before = current_summary.get(field), previous_summary.get(field)
        if isinstance(now, (int, float)) and isinstance(before, (int, float)):
            deltas[field] = round(now - before, 4)
    return {
        "baseline_generated_at": previous.get("generated_at"),
        "baseline_version": previous.get("suite_version"),
        "deltas": deltas,
    }


def write_report(report: dict[str, Any], reports_dir: str | Path) -> Path:
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = directory / timestamp.strftime("runtime-replay-%Y%m%dT%H%M%SZ.json")
    path.write_text(
        json.dumps({"generated_at": timestamp.isoformat(), **report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _requested_case_ids(raw_values: list[str] | None) -> list[str] | None:
    if not raw_values:
        return None
    return [item.strip().upper() for value in raw_values for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated offline runtime reliability replays")
    parser.add_argument("--case", action="append", help="Case id(s), e.g. R04 or R01,R05")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic fixture scheduling seed")
    parser.add_argument("--strict", action="store_true", help="Return non-zero if any assertion fails")
    parser.add_argument("--write-report", action="store_true", help="Write a timestamped ignored JSON report")
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--compare", help="Prior runtime-replay JSON report")
    parser.add_argument("--list", action="store_true", help="List available replay cases and exit")
    args = parser.parse_args()

    if args.list:
        print(json.dumps([{"id": case.case_id, "title": case.title} for case in CASES], ensure_ascii=False, indent=2))
        return 0

    report = run_suite(case_ids=_requested_case_ids(args.case), seed=args.seed)
    if args.compare:
        previous = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        report["comparison"] = compare_reports(report, previous)
    if args.write_report:
        report["report_path"] = str(write_report(report, args.reports_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["passed"] != report["total"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Versioned, API-free scorer and metrics reporter for research-agent evals."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "research_tasks.json"
_EVIDENCE_REFERENCE = re.compile(r"\[E\d+\]", re.IGNORECASE)


def load_manifest(path: str | Path = MANIFEST_PATH) -> dict[str, Any]:
    """Load the task manifest without reading project runtime data."""
    with Path(path).open(encoding="utf-8") as file:
        return json.load(file)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return schema errors; an empty list means the benchmark is usable."""
    errors: list[str] = []
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 14:
        return ["任务清单必须恰好包含 14 个任务"]

    task_ids = set()
    capabilities = set()
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            errors.append("存在缺失 id 的任务")
        elif task_id in task_ids:
            errors.append(f"任务 id 重复: {task_id}")
        task_ids.add(task_id)

        for field in ("capability", "title", "prompt", "fixture", "expected"):
            if not task.get(field):
                errors.append(f"{task_id}: 缺少 {field}")
        capabilities.add(task.get("capability"))
        expected = task.get("expected") or {}
        if not any(key in expected for key in (
            "answer_checks", "state_assertions", "tool_trace_contains", "min_evidence_references",
        )):
            errors.append(f"{task_id}: 缺少可验证的预期结果")
        for check in expected.get("answer_checks", []):
            if not check.get("label") or not check.get("any_of"):
                errors.append(f"{task_id}: answer_checks 格式不完整")
        minimum = expected.get("min_evidence_references")
        if minimum is not None and (not isinstance(minimum, int) or minimum < 1):
            errors.append(f"{task_id}: min_evidence_references 必须为正整数")

    if len(capabilities) < 12:
        errors.append("任务能力覆盖不足，至少需要 12 类")
    return errors


def _contains_any(text: str, candidates: list[str]) -> bool:
    normalized = text.casefold()
    return any(candidate.casefold() in normalized for candidate in candidates)


def _tool_names(tool_trace: list[Any]) -> set[str]:
    """Accept compact tool names or the structured trace emitted by ResearchAgent."""
    names = set()
    for event in tool_trace:
        if isinstance(event, str):
            names.add(event)
        elif isinstance(event, dict) and isinstance(event.get("tool"), str):
            names.add(event["tool"])
    return names


def _evidence_reference_count(answer: str) -> int:
    return len(set(_EVIDENCE_REFERENCE.findall(answer)))


def score_task(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Score deterministic checks and retain the rubric for human review."""
    expected = task["expected"]
    answer = str(result.get("answer", ""))
    trace = _tool_names(result.get("tool_trace", []))
    state = result.get("state", {}) or {}
    metrics = result.get("metrics", {}) or {}
    checks = []

    for check in expected.get("answer_checks", []):
        passed = _contains_any(answer, check["any_of"])
        checks.append({"kind": "answer", "label": check["label"], "passed": passed})
    for forbidden in expected.get("must_not_include", []):
        checks.append({
            "kind": "forbidden", "label": forbidden,
            "passed": forbidden.casefold() not in answer.casefold(),
        })
    for tool_name in expected.get("tool_trace_contains", []):
        checks.append({"kind": "tool", "label": tool_name, "passed": tool_name in trace})
    for key, value in expected.get("state_assertions", {}).items():
        checks.append({"kind": "state", "label": key, "passed": state.get(key) == value})
    for metric, upper_bound in expected.get("performance", {}).items():
        value = metrics.get(metric)
        checks.append({
            "kind": "performance", "label": metric,
            "passed": isinstance(value, (int, float)) and not isinstance(value, bool) and value <= upper_bound,
            "actual": value, "max": upper_bound,
        })
    minimum_evidence = expected.get("min_evidence_references")
    evidence_references = _evidence_reference_count(answer)
    if minimum_evidence is not None:
        checks.append({
            "kind": "evidence_reference", "label": "evidence_references",
            "passed": evidence_references >= minimum_evidence,
            "actual": evidence_references, "min": minimum_evidence,
        })

    return {
        "task_id": task["id"],
        "title": task["title"],
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "evidence_references": evidence_references,
        "manual_rubric": expected.get("manual_rubric", []),
    }


def _numeric_values(results: list[dict[str, Any]], name: str) -> list[float]:
    return [
        float(item.get("metrics", {}).get(name))
        for item in results
        if isinstance(item.get("metrics", {}).get(name), (int, float))
        and not isinstance(item.get("metrics", {}).get(name), bool)
    ]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _source_health(results: list[dict[str, Any]]) -> dict[str, int | float]:
    requests_total = 0
    failed = 0
    for result in results:
        source_stats = result.get("source_stats") or (result.get("state") or {}).get("source_stats") or {}
        if not isinstance(source_stats, dict):
            continue
        for keyword_stats in source_stats.values():
            if not isinstance(keyword_stats, dict):
                continue
            details = [keyword_stats] if "status" in keyword_stats else keyword_stats.values()
            for detail in details:
                if not isinstance(detail, dict) or "status" not in detail:
                    continue
                requests_total += 1
                failed += int(detail.get("status") != "ok")
    return {
        "source_requests": requests_total,
        "source_failures": failed,
        "source_failure_rate": round(failed / requests_total, 4) if requests_total else 0.0,
    }


def summarize_submission(scored: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only explicitly captured, non-sensitive execution metadata."""
    all_checks = [check for task in scored for check in task["checks"]]
    evidence_checks = [
        check for task in scored for check in task["checks"]
        if check["kind"] == "evidence_reference"
    ]
    durations = _numeric_values(results, "duration_ms")
    first_tokens = _numeric_values(results, "first_token_ms")
    model_calls = _numeric_values(results, "model_calls")
    passed = sum(item["passed"] for item in scored)
    summary = {
        "success_rate": round(passed / len(scored), 4) if scored else 0.0,
        "hard_check_pass_rate": round(
            sum(check["passed"] for check in all_checks) / len(all_checks), 4,
        ) if all_checks else 0.0,
        # This is traceability coverage, not a judgement of citation correctness;
        # correctness remains deliberately in the manual rubric.
        "citation_traceability_rate": round(
            sum(check["passed"] for check in evidence_checks) / len(evidence_checks), 4,
        ) if evidence_checks else None,
        "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else None,
        "p95_duration_ms": _percentile(durations, 0.95),
        "average_first_token_ms": round(sum(first_tokens) / len(first_tokens), 1) if first_tokens else None,
        "total_model_calls": int(sum(model_calls)),
        "average_model_calls": round(sum(model_calls) / len(model_calls), 2) if model_calls else None,
        **_source_health(results),
    }
    return summary


def score_submission(manifest: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Score submitted task results by task id; missing tasks are failures."""
    result_by_id = {item.get("task_id"): item for item in results}
    scored = [score_task(task, result_by_id.get(task["id"], {})) for task in manifest["tasks"]]
    passed = sum(item["passed"] for item in scored)
    return {
        "benchmark_version": manifest.get("version"),
        "passed": passed,
        "total": len(scored),
        "summary": summarize_submission(scored, results),
        "tasks": scored,
    }


def compare_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Report directional deltas without requiring a database or external service."""
    current_summary = current.get("summary", {})
    previous_summary = previous.get("summary", {})
    fields = (
        "success_rate", "hard_check_pass_rate", "citation_traceability_rate",
        "average_duration_ms", "p95_duration_ms", "average_first_token_ms",
        "total_model_calls", "source_failure_rate",
    )
    deltas = {}
    for field in fields:
        now, before = current_summary.get(field), previous_summary.get(field)
        if isinstance(now, (int, float)) and isinstance(before, (int, float)):
            deltas[field] = round(now - before, 4)
    return {
        "baseline_generated_at": previous.get("generated_at"),
        "baseline_version": previous.get("benchmark_version"),
        "deltas": deltas,
    }


def write_report(report: dict[str, Any], reports_dir: str | Path) -> Path:
    """Persist a dated report only when the caller explicitly requests it."""
    generated_at = datetime.now(timezone.utc).isoformat()
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("benchmark-%Y%m%dT%H%M%SZ.json")
    path = directory / filename
    path.write_text(
        json.dumps({"generated_at": generated_at, **report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="验证或评分科研 Agent 评测集")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--results", help="Agent 运行结果 JSON（list[task_id, answer, tool_trace, state]）")
    parser.add_argument("--strict", action="store_true", help="有未通过任务时返回非零状态")
    parser.add_argument("--write-report", action="store_true", help="将本次评分保存为带时间戳的 JSON 报告")
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--compare", help="上一份 benchmark 报告，用于输出指标变化")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    errors = validate_manifest(manifest)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    if not args.results:
        print(json.dumps({"valid": True, "tasks": len(manifest["tasks"])}, ensure_ascii=False))
        return 0

    with Path(args.results).open(encoding="utf-8") as file:
        results = json.load(file)
    report = score_submission(manifest, results)
    if args.compare:
        with Path(args.compare).open(encoding="utf-8") as file:
            report["comparison"] = compare_reports(report, json.load(file))
    if args.write_report:
        report["report_path"] = str(write_report(report, args.reports_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.strict and report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())

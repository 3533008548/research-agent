"""Versioned, API-free scorer for the research-agent benchmark."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "research_tasks.json"


def load_manifest(path: str | Path = MANIFEST_PATH) -> dict[str, Any]:
    """Load the task manifest without reading project runtime data."""
    with Path(path).open(encoding="utf-8") as file:
        return json.load(file)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return schema errors; an empty list means the benchmark is usable."""
    errors: list[str] = []
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 10:
        return ["任务清单必须恰好包含 10 个任务"]

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
            "answer_checks", "state_assertions", "tool_trace_contains",
        )):
            errors.append(f"{task_id}: 缺少可验证的预期结果")
        for check in expected.get("answer_checks", []):
            if not check.get("label") or not check.get("any_of"):
                errors.append(f"{task_id}: answer_checks 格式不完整")

    if len(capabilities) < 8:
        errors.append("任务能力覆盖不足，至少需要 8 类")
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
            "passed": isinstance(value, (int, float)) and value <= upper_bound,
            "actual": value, "max": upper_bound,
        })

    return {
        "task_id": task["id"],
        "title": task["title"],
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "manual_rubric": expected.get("manual_rubric", []),
    }


def score_submission(manifest: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Score submitted task results by task id; missing tasks are failures."""
    result_by_id = {item.get("task_id"): item for item in results}
    scored = []
    for task in manifest["tasks"]:
        result = result_by_id.get(task["id"], {})
        scored.append(score_task(task, result))
    passed = sum(item["passed"] for item in scored)
    return {
        "benchmark_version": manifest.get("version"),
        "passed": passed,
        "total": len(scored),
        "tasks": scored,
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
    if args.write_report:
        report["report_path"] = str(write_report(report, args.reports_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.strict and report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())

"""Versioned, reviewer-friendly real user acceptance evaluation.

This module deliberately separates what can be measured mechanically from
what requires a human judgement.  It uses the same isolated runtime rule as
``real_eval`` so that real calls never write test conversations into the web
application's user-facing session list.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.real_eval import (
    DEFAULT_REPORTS_DIR,
    redact_known_secrets,
    resolve_runtime,
)


MANIFEST_PATH = Path(__file__).resolve().with_name("user_acceptance_tasks.json")
_EVIDENCE_REFERENCE = re.compile(r"\[E\d+\]", re.IGNORECASE)
_SECRET_ENV_NAMES = ("DEEPSEEK_API_KEY", "OPENALEX_API_KEY", "GLM_API_KEY")


def load_manifest(path: str | Path = MANIFEST_PATH) -> dict[str, Any]:
    """Load the acceptance scenarios without touching application runtime data."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return structural errors; valid scenarios are safe to select and run."""
    errors: list[str] = []
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) < 10:
        return ["验收集至少需要 10 个场景"]

    seen_ids: set[str] = set()
    allowed_modes = {"chat", "research", "sequence", "isolated_sessions", "daily_search", "session_delete"}
    for scenario in scenarios:
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            errors.append("存在缺少 id 的验收场景")
            continue
        if scenario_id in seen_ids:
            errors.append(f"场景 ID 重复: {scenario_id}")
        seen_ids.add(scenario_id)
        for field in ("suite", "journey", "title", "mode", "automated", "manual_rubric"):
            if not scenario.get(field):
                errors.append(f"{scenario_id}: 缺少 {field}")
        mode = scenario.get("mode")
        if mode not in allowed_modes:
            errors.append(f"{scenario_id}: 不支持的 mode: {mode}")
        if mode == "sequence":
            turns = scenario.get("turns")
            if not isinstance(turns, list) or len(turns) < 2 or not all(isinstance(turn, str) and turn.strip() for turn in turns):
                errors.append(f"{scenario_id}: sequence 至少需要两轮非空 turns")
        elif mode == "isolated_sessions":
            if not scenario.get("setup_prompt") or not scenario.get("prompt"):
                errors.append(f"{scenario_id}: isolated_sessions 需要 setup_prompt 和 prompt")
        elif mode == "daily_search":
            if not scenario.get("keyword"):
                errors.append(f"{scenario_id}: daily_search 需要 keyword")
        elif mode != "session_delete" and not scenario.get("prompt"):
            errors.append(f"{scenario_id}: 缺少 prompt")
        rubric = scenario.get("manual_rubric") or []
        if not isinstance(rubric, list) or not rubric:
            errors.append(f"{scenario_id}: manual_rubric 必须为非空列表")
        for item in rubric:
            if not isinstance(item, dict) or not item.get("id") or not item.get("label") or not item.get("description"):
                errors.append(f"{scenario_id}: 存在不完整的人工评分项")
    return errors


def select_scenarios(
    scenario_ids: list[str] | None = None,
    *,
    suites: list[str] | None = None,
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Select explicit scenario IDs or named suites while preserving manifest order."""
    manifest = manifest or load_manifest()
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("验收集无效: " + "; ".join(errors))
    available = {str(item["id"]): item for item in manifest["scenarios"]}
    requested_ids = [value.strip().upper() for value in (scenario_ids or []) if value.strip()]
    requested_suites = [value.strip().lower() for value in (suites or []) if value.strip()]
    duplicate_ids = sorted({value for value in requested_ids if requested_ids.count(value) > 1})
    if duplicate_ids:
        raise ValueError(f"验收场景重复: {', '.join(duplicate_ids)}")
    unknown_ids = [value for value in requested_ids if value not in available]
    if unknown_ids:
        raise ValueError(f"未知验收场景: {', '.join(unknown_ids)}")

    requested = set(requested_ids)
    available_suites = {str(item.get("suite", "")).lower() for item in manifest["scenarios"]}
    unknown_suites = [value for value in requested_suites if value not in available_suites]
    if unknown_suites:
        raise ValueError(f"未知验收分组: {', '.join(unknown_suites)}")
    requested.update(
        str(item["id"])
        for item in manifest["scenarios"]
        if str(item.get("suite", "")).lower() in requested_suites
    )
    if not requested:
        raise ValueError("至少指定一个 --scenario 或 --suite，例如 --suite core")
    return [item for item in manifest["scenarios"] if item["id"] in requested]


def _trace_summary(trace: dict[str, Any] | None) -> dict[str, Any]:
    trace = trace or {}
    tool_trace = list(trace.get("tool_trace") or [])
    safe_tools = []
    for event in tool_trace:
        if isinstance(event, str):
            safe_tools.append({"tool": event})
        elif isinstance(event, dict):
            safe_tools.append({
                "tool": str(event.get("tool") or event.get("name") or ""),
                "duration_ms": event.get("duration_ms"),
                "status": event.get("status"),
            })
    usage = trace.get("usage_delta") or {}
    return {
        "outcome": trace.get("outcome"),
        "duration_ms": trace.get("duration_ms"),
        "first_token_ms": trace.get("first_token_ms"),
        "model_calls": usage.get("calls"),
        "tools": safe_tools,
        "error_type": trace.get("error_type"),
    }


def _tool_names(trace: dict[str, Any]) -> set[str]:
    names = set()
    for event in trace.get("tools") or []:
        if isinstance(event, dict) and event.get("tool"):
            names.add(str(event["tool"]))
    return names


def _contains_secret(answer: str, configured_secrets: tuple[str, ...]) -> bool:
    return any(secret and secret in answer for secret in configured_secrets)


def _check_automated(scenario: dict[str, Any], observed: dict[str, Any], secrets: tuple[str, ...]) -> list[dict[str, Any]]:
    rules = scenario.get("automated") or {}
    answer = str(observed.get("answer") or "")
    trace = observed.get("trace") or {}
    checks: list[dict[str, Any]] = []

    minimum = rules.get("min_answer_chars")
    if isinstance(minimum, int):
        checks.append({"label": "minimum answer length", "passed": len(answer.strip()) >= minimum, "actual": len(answer.strip()), "min": minimum})
    maximum = rules.get("max_duration_ms")
    duration = trace.get("duration_ms")
    if isinstance(maximum, (int, float)):
        checks.append({
            "label": "duration budget",
            "passed": isinstance(duration, (int, float)) and duration <= maximum,
            "actual": duration,
            "max": maximum,
        })
    min_refs = rules.get("min_evidence_references")
    if isinstance(min_refs, int):
        actual_refs = len(_EVIDENCE_REFERENCE.findall(answer))
        checks.append({"label": "evidence references", "passed": actual_refs >= min_refs, "actual": actual_refs, "min": min_refs})
    min_evidence = rules.get("min_evidence")
    if isinstance(min_evidence, int):
        evidence_count = int(observed.get("state", {}).get("evidence_count", 0))
        checks.append({"label": "persisted evidence", "passed": evidence_count >= min_evidence, "actual": evidence_count, "min": min_evidence})
    min_candidates = rules.get("min_candidates")
    if isinstance(min_candidates, int):
        candidate_count = int(observed.get("state", {}).get("candidate_count", 0))
        checks.append({"label": "daily candidates", "passed": candidate_count >= min_candidates, "actual": candidate_count, "min": min_candidates})
    required_tools = rules.get("required_trace_tools") or []
    actual_tools = _tool_names(trace)
    for tool in required_tools:
        checks.append({"label": f"tool: {tool}", "passed": tool in actual_tools})
    statuses = rules.get("allowed_statuses")
    if isinstance(statuses, list):
        status = observed.get("state", {}).get("status")
        checks.append({"label": "run status", "passed": status in statuses, "actual": status, "allowed": statuses})
    min_turns = rules.get("min_turns")
    if isinstance(min_turns, int):
        actual_turns = int(observed.get("state", {}).get("turn_count", 0))
        checks.append({"label": "conversation turns", "passed": actual_turns >= min_turns, "actual": actual_turns, "min": min_turns})
    for key, expected in (rules.get("state_assertions") or {}).items():
        actual = observed.get("state", {}).get(key)
        checks.append({"label": f"state: {key}", "passed": actual == expected, "actual": actual, "expected": expected})
    if rules.get("secret_free"):
        checks.append({"label": "configured secret absent", "passed": not _contains_secret(answer, secrets)})
    return checks


def _manual_review_template(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "description": item["description"],
            "critical": bool(item.get("critical")),
            "score": None,
            "notes": "",
        }
        for item in scenario.get("manual_rubric", [])
    ]


def _research_state(agent, session_id: str) -> dict[str, Any]:
    run = agent.sessions.get_latest_research_run(session_id) or {}
    evidence = run.get("evidence") or []
    return {"status": str(run.get("status") or "missing"), "evidence_count": len(evidence)}


def _daily_state(result: Any) -> dict[str, Any]:
    return {
        "status": str(getattr(result, "status", "missing")),
        "candidate_count": len(getattr(result, "candidates", []) or []),
        "run_id": str(getattr(result, "run_id", "")),
    }


def _format_daily_answer(result: Any) -> str:
    brief = str(getattr(result, "brief", "") or "")
    candidates = list(getattr(result, "candidates", []) or [])
    titles = [str(item.get("title") or "") for item in candidates[:3] if isinstance(item, dict)]
    if titles:
        return f"{brief}\n\n候选论文：" + "；".join(titles)
    return brief


def _run_scenario(agent, scheduler, scenario: dict[str, Any], *, research_scope: str) -> dict[str, Any]:
    """Execute one user journey and capture only the answer plus safe metadata."""
    mode = str(scenario["mode"])
    started = time.perf_counter()
    state: dict[str, Any] = {}
    answer = ""
    trace: dict[str, Any] = {}

    if mode == "chat":
        session = agent.create_session(f"验收 {scenario['id']}")
        answer = agent.step(str(scenario["prompt"]), session_id=str(session["thread_id"]))
        trace = _trace_summary(agent.get_last_trace(str(session["thread_id"])))
    elif mode == "research":
        session = agent.create_session(f"验收 {scenario['id']}")
        session_id = str(session["thread_id"])
        answer = agent.research(str(scenario["prompt"]), scope=research_scope, session_id=session_id)
        state.update(_research_state(agent, session_id))
        trace = _trace_summary(agent.get_last_trace(session_id))
    elif mode == "sequence":
        session = agent.create_session(f"验收 {scenario['id']}")
        session_id = str(session["thread_id"])
        responses = []
        for turn in scenario["turns"]:
            responses.append(agent.step(str(turn), session_id=session_id))
        answer = responses[-1]
        state["turn_count"] = len(responses)
        trace = _trace_summary(agent.get_last_trace(session_id))
    elif mode == "isolated_sessions":
        source = agent.create_session(f"验收 {scenario['id']} 来源")
        target = agent.create_session(f"验收 {scenario['id']} 目标")
        agent.step(str(scenario["setup_prompt"]), session_id=str(source["thread_id"]))
        answer = agent.step(str(scenario["prompt"]), session_id=str(target["thread_id"]))
        state["sessions_distinct"] = source["thread_id"] != target["thread_id"]
        trace = _trace_summary(agent.get_last_trace(str(target["thread_id"])))
    elif mode == "daily_search":
        from daily_orchestrator import DailyResearchOrchestrator

        orchestrator = DailyResearchOrchestrator(
            scheduler=scheduler,
            llm_client=agent.llm_client,
            model=agent.model,
            request_timeout_seconds=agent.cfg.daily_request_timeout_seconds,
            max_keyword_concurrency=agent.cfg.daily_keyword_concurrency,
            max_results_per_keyword=agent.cfg.daily_max_results_per_keyword,
            daily_sources=agent.cfg.daily_sources,
            openalex_api_key=agent.cfg.openalex_api_key,
        )
        result = orchestrator.run("search", keyword=str(scenario["keyword"]), paper_store=None)
        answer = _format_daily_answer(result)
        state.update(_daily_state(result))
        trace = {"outcome": state["status"], "duration_ms": round((time.perf_counter() - started) * 1000, 1), "tools": [{"tool": "daily_search"}]}
    elif mode == "session_delete":
        target = agent.create_session(f"验收 {scenario['id']} 待删除")
        retained = agent.create_session(f"验收 {scenario['id']} 保留")
        target_id = str(target["thread_id"])
        retained_id = str(retained["thread_id"])
        deleted = agent.delete_session(target_id)
        retained_exists = agent.sessions.get(retained_id) is not None
        state.update({"target_deleted": deleted and agent.sessions.get(target_id) is None, "retained_session_present": retained_exists})
        answer = "已删除目标会话；其他会话仍保留。" if deleted and retained_exists else "会话删除验证未完成。"
        trace = {"outcome": "success" if deleted else "failed", "duration_ms": round((time.perf_counter() - started) * 1000, 1), "tools": []}
    else:  # validate_manifest prevents this path.
        raise ValueError(f"不支持的验收场景模式: {mode}")

    trace.setdefault("duration_ms", round((time.perf_counter() - started) * 1000, 1))
    return {"answer": answer, "state": state, "trace": trace}


def _result_for_report(scenario: dict[str, Any], observed: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    automated = _check_automated(scenario, observed, secrets)
    return {
        "scenario_id": scenario["id"],
        "suite": scenario["suite"],
        "journey": scenario["journey"],
        "title": scenario["title"],
        "mode": scenario["mode"],
        "answer": observed["answer"],
        "trace": observed["trace"],
        "state": observed["state"],
        "automated_checks": automated,
        "automated_passed": all(item["passed"] for item in automated),
        "manual_review": _manual_review_template(scenario),
    }


def _failed_observation(exc: Exception) -> dict[str, Any]:
    """Preserve a diagnosable but non-sensitive failed journey result."""
    return {
        "answer": "",
        "state": {"status": "failed"},
        "trace": {
            "outcome": "failed",
            "duration_ms": 0.0,
            "tools": [],
            "error_type": type(exc).__name__,
        },
    }


def build_report_from_observations(
    scenarios: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    model: str = "fixture-model",
    isolated_temporary_runtime: bool = True,
    rag_enabled: bool = False,
    research_scope: str = "public",
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a reviewable report from supplied safe observations.

    This is intentionally public so deterministic tests and future offline
    fixtures can validate report semantics without invoking a real model.
    """
    if len(scenarios) != len(observations):
        raise ValueError("验收场景与观察结果数量不一致")
    results = [_result_for_report(scenario, observed, secrets) for scenario, observed in zip(scenarios, observations)]
    threshold = float(manifest.get("default_manual_threshold", 1.5))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "versioned-real-user-acceptance",
        "acceptance_version": manifest.get("version"),
        "selection": {"scenario_ids": [item["id"] for item in scenarios], "scenario_count": len(scenarios)},
        "execution": {
            "uses_real_model": True,
            "model": model,
            "isolated_temporary_runtime": isolated_temporary_runtime,
            "rag_enabled": rag_enabled,
            "research_scope": research_scope,
            "trace_payload_policy": "final answer plus sanitized trace; no prompt, runtime DB, candidates, or key",
        },
        "review_instructions": {
            "workflow": "先检查 automated_checks，再按 0/1/2 填写 manual_review.score 和 notes；关键项低于阈值时记录失败原因。",
            "score_scale": manifest.get("review_scale"),
            "threshold": threshold,
            "do_not_treat_as": "自动化通过不代表事实正确、引用充分或用户体验合格。",
        },
        "results": results,
        "summary": summarize_results(results, manual_threshold=threshold),
    }


def summarize_results(results: list[dict[str, Any]], *, manual_threshold: float) -> dict[str, Any]:
    """Summarize mechanical results; manual scores stay pending until imported."""
    check_rows = [check for result in results for check in result.get("automated_checks", [])]
    passed = sum(bool(result.get("automated_passed")) for result in results)
    durations = [
        float(result["trace"]["duration_ms"])
        for result in results
        if isinstance(result.get("trace", {}).get("duration_ms"), (int, float))
    ]
    manual_items = [item for result in results for item in result.get("manual_review", [])]
    scored_manual = [float(item["score"]) for item in manual_items if isinstance(item.get("score"), (int, float))]
    critical_failures = [
        item["id"] for item in manual_items
        if item.get("critical") and isinstance(item.get("score"), (int, float)) and float(item["score"]) < manual_threshold
    ]
    return {
        "automated_passed": passed,
        "automated_total": len(results),
        "automated_success_rate": round(passed / len(results), 4) if results else 0.0,
        "automated_check_pass_rate": round(sum(item["passed"] for item in check_rows) / len(check_rows), 4) if check_rows else None,
        "p95_duration_ms": _percentile(durations, 0.95),
        "manual_review": {
            "scale": "0=不满足, 1=部分满足, 2=满足",
            "threshold": manual_threshold,
            "scored_items": len(scored_manual),
            "total_items": len(manual_items),
            "average_score": round(sum(scored_manual) / len(scored_manual), 3) if scored_manual else None,
            "pending_items": len(manual_items) - len(scored_manual),
            "critical_below_threshold": critical_failures,
        },
    }


def validate_manual_review(report: dict[str, Any]) -> list[str]:
    """Validate completed human scores without judging their substantive content."""
    errors: list[str] = []
    if not isinstance(report.get("acceptance_version"), str):
        errors.append("报告缺少 acceptance_version")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        return errors + ["报告缺少 results"]
    for result in results:
        scenario_id = result.get("scenario_id", "未知场景") if isinstance(result, dict) else "未知场景"
        reviews = result.get("manual_review") if isinstance(result, dict) else None
        if not isinstance(reviews, list):
            errors.append(f"{scenario_id}: 缺少 manual_review")
            continue
        for item in reviews:
            if not isinstance(item, dict) or not item.get("id"):
                errors.append(f"{scenario_id}: 存在不完整的人工评分项")
                continue
            score = item.get("score")
            if score is not None and (isinstance(score, bool) or score not in {0, 1, 2}):
                errors.append(f"{scenario_id}/{item['id']}: score 必须为 0、1、2 或 null")
    return errors


def refresh_review_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Recalculate aggregate results after a reviewer fills manual scores."""
    errors = validate_manual_review(report)
    if errors:
        raise ValueError("人工评分报告无效: " + "; ".join(errors))
    updated = json.loads(json.dumps(report, ensure_ascii=False))
    threshold = float((updated.get("review_instructions") or {}).get("threshold", 1.5))
    updated["summary"] = summarize_results(list(updated["results"]), manual_threshold=threshold)
    updated["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    return updated


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _report_path(reports_dir: str | Path) -> Path:
    directory = Path(reports_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / datetime.now(timezone.utc).strftime("user-acceptance-%Y%m%dT%H%M%SZ.json")


def write_report(report: dict[str, Any], reports_dir: str | Path = DEFAULT_REPORTS_DIR) -> Path:
    """Write an already-redacted acceptance report to the ignored reports directory."""
    path = _report_path(reports_dir)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_acceptance(
    scenario_ids: list[str] | None = None,
    *,
    suites: list[str] | None = None,
    data_dir: str | None = None,
    keep_runtime: bool = False,
    allow_production_runtime: bool = False,
    with_rag: bool = False,
    research_scope: str = "public",
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
    compare_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run selected user journeys in an isolated runtime and write a review report."""
    manifest = load_manifest()
    selected = select_scenarios(scenario_ids, suites=suites, manifest=manifest)
    runtime_root, owns_runtime = resolve_runtime(
        data_dir,
        keep_runtime=keep_runtime,
        allow_production_runtime=allow_production_runtime,
    )
    if dry_run:
        if owns_runtime:
            shutil.rmtree(runtime_root, ignore_errors=True)
        return {
            "dry_run": True,
            "scenario_ids": [item["id"] for item in selected],
            "suites": sorted({item["suite"] for item in selected}),
            "runtime": "temporary" if owns_runtime else "operator-specified",
            "keep_runtime": keep_runtime,
            "rag_enabled": with_rag,
            "research_scope": research_scope,
        }

    prior_data_dir = os.environ.get("APP_DATA_DIR")
    agent = None
    scheduler = None
    try:
        from config import Config
        from research_agent import ResearchAgent
        from scheduler import Scheduler

        config = Config.load({"data_dir": str(runtime_root), "rag_enabled": with_rag})
        agent = ResearchAgent(config)
        scheduler = Scheduler(config.daily_db, request_timeout_seconds=config.daily_request_timeout_seconds)
        secrets = tuple(os.getenv(name, "") for name in _SECRET_ENV_NAMES)
        observations = []
        for scenario in selected:
            try:
                observed = _run_scenario(agent, scheduler, scenario, research_scope=research_scope)
            except Exception as exc:  # Continue to produce a reviewable failure report for the remaining journeys.
                observed = _failed_observation(exc)
            observations.append(observed)
        report = build_report_from_observations(
            selected,
            observations,
            manifest=manifest,
            model=agent.model,
            isolated_temporary_runtime=owns_runtime and not keep_runtime,
            rag_enabled=with_rag,
            research_scope=research_scope,
            secrets=secrets,
        )
        report = redact_known_secrets(report, secrets)
        if compare_path:
            previous = json.loads(Path(compare_path).read_text(encoding="utf-8"))
            report["comparison"] = compare_reports(report, previous)
        path = write_report(report, reports_dir)
        return {
            "report_path": str(path),
            "scenario_ids": report["selection"]["scenario_ids"],
            "automated_passed": report["summary"]["automated_passed"],
            "automated_total": report["summary"]["automated_total"],
            "manual_review_pending": report["summary"]["manual_review"]["pending_items"],
            "runtime_retained": keep_runtime or not owns_runtime,
            "runtime_root": str(runtime_root) if keep_runtime or not owns_runtime else None,
        }
    finally:
        if scheduler is not None:
            scheduler.close()
        if agent is not None:
            for store_name in ("sessions", "memory"):
                close = getattr(getattr(agent, store_name, None), "close", None)
                if callable(close):
                    close()
        if prior_data_dir is None:
            os.environ.pop("APP_DATA_DIR", None)
        else:
            os.environ["APP_DATA_DIR"] = prior_data_dir
        if owns_runtime and not keep_runtime:
            shutil.rmtree(runtime_root, ignore_errors=True)


def _scores_by_scenario(report: dict[str, Any]) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for result in report.get("results") or []:
        if not isinstance(result, dict) or not isinstance(result.get("scenario_id"), str):
            continue
        scores[result["scenario_id"]] = {
            str(item["id"]): float(item["score"])
            for item in result.get("manual_review") or []
            if isinstance(item, dict) and item.get("id") and isinstance(item.get("score"), (int, float))
        }
    return scores


def compare_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Compare only matching acceptance versions and non-sensitive aggregate scores."""
    if previous.get("acceptance_version") != current.get("acceptance_version"):
        raise ValueError("不能比较不同版本的真实用户验收报告")
    current_summary = current.get("summary") or {}
    previous_summary = previous.get("summary") or {}
    deltas: dict[str, float] = {}
    for field in ("automated_success_rate", "automated_check_pass_rate", "p95_duration_ms"):
        now, before = current_summary.get(field), previous_summary.get(field)
        if isinstance(now, (int, float)) and isinstance(before, (int, float)):
            deltas[field] = round(float(now) - float(before), 4)
    current_manual = (current_summary.get("manual_review") or {}).get("average_score")
    previous_manual = (previous_summary.get("manual_review") or {}).get("average_score")
    if isinstance(current_manual, (int, float)) and isinstance(previous_manual, (int, float)):
        deltas["manual_average_score"] = round(float(current_manual) - float(previous_manual), 4)
    current_scores = _scores_by_scenario(current)
    previous_scores = _scores_by_scenario(previous)
    manual_deltas = {
        scenario_id: {
            item_id: round(current_score - previous_scores[scenario_id][item_id], 4)
            for item_id, current_score in items.items()
            if item_id in previous_scores.get(scenario_id, {})
        }
        for scenario_id, items in current_scores.items()
        if scenario_id in previous_scores
    }
    return {
        "baseline_generated_at": previous.get("generated_at"),
        "baseline_version": previous.get("acceptance_version"),
        "deltas": deltas,
        "manual_scores_by_scenario": manual_deltas,
    }

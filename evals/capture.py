"""Adapt a real ResearchAgent turn into the benchmark result format."""

from __future__ import annotations

from typing import Any


def result_from_trace(
    task_id: str,
    answer: str,
    trace: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
    source_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a scorer-ready result without storing prompts, keys or tool payloads."""
    trace = trace or {}
    tool_trace = list(trace.get("tool_trace") or [])
    durations = [
        event.get("duration_ms") for event in tool_trace
        if isinstance(event, dict) and isinstance(event.get("duration_ms"), (int, float))
    ]
    metrics = {
        "duration_ms": trace.get("duration_ms"),
        "first_token_ms": trace.get("first_token_ms"),
        "max_tool_duration_ms": max(durations) if durations else 0,
        "model_calls": (trace.get("usage_delta") or {}).get("calls"),
    }
    return {
        "task_id": task_id,
        "answer": answer,
        "tool_trace": tool_trace,
        "state": state or {},
        "source_stats": source_stats or {},
        "metrics": {key: value for key, value in metrics.items() if value is not None},
    }


def result_from_daily_run(
    task_id: str,
    daily_result: Any,
    answer: str | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a DailyRunResult without retaining candidates, prompts or API data."""
    safe_state = {
        "run_status": str(getattr(daily_result, "status", "")),
        "candidate_count": len(getattr(daily_result, "candidates", []) or []),
        **(state or {}),
    }
    return {
        "task_id": task_id,
        "answer": answer if answer is not None else str(getattr(daily_result, "brief", "")),
        "tool_trace": ["daily_search"],
        "state": safe_state,
        "source_stats": getattr(daily_result, "source_stats", {}) or {},
        "metrics": {},
    }

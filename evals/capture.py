"""Adapt a real ResearchAgent turn into the benchmark result format."""

from __future__ import annotations

from typing import Any


def result_from_trace(
    task_id: str,
    answer: str,
    trace: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
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
    }
    return {
        "task_id": task_id,
        "answer": answer,
        "tool_trace": tool_trace,
        "state": state or {},
        "metrics": {key: value for key, value in metrics.items() if value is not None},
    }

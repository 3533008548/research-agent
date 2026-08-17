"""Shared, privacy-safe contracts for externally observable agent runs.

This module intentionally owns only the small vocabulary shared by chat,
deep-research and daily-discovery runs.  It is not a plugin framework or an
event bus: business-specific state continues to live in the existing
orchestrators and SQLite tables.
"""

from __future__ import annotations

from typing import Any


RUN_EVENT_SCHEMA_VERSION = 1
RUN_EVENT_PROTOCOL = "run-event/v1"

# The SSE contract is public.  Keep it small enough for Gradio, CLI clients
# and future clients to share without exposing internal graph events.
SSE_EVENT_TYPES = frozenset({"status", "token", "tool", "done", "error"})

# Token chunks are intentionally ephemeral Redis data.  Durable audit events
# exclude them so a long answer cannot turn SQLite into a stream transcript.
PERSISTED_EVENT_TYPES = frozenset({"status", "tool", "done", "error"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "partial_failed"})
TERMINAL_EVENT_STAGES = frozenset({"run", "completed", "cancelled", "failed", "delivery", "orchestrator"})
RUN_KINDS = frozenset({"chat", "research", "daily"})
RESEARCH_SCOPES = frozenset({"both", "local", "public"})


_SAFE_METRIC_KEYS = frozenset({
    "at_ms",
    "duration_ms",
    "queue_wait_ms",
    "attempt",
    "model_calls",
    "tool_count",
    "candidate_count",
    "source_failures",
    "event_count",
})
_SAFE_METADATA_KEYS = frozenset({"protocol", "run_kind", "model", "runner", "scope", "toolset"})


def safe_metrics(metrics: dict[str, Any] | None) -> dict[str, int | float]:
    """Keep only bounded aggregate measurements in a durable event."""
    safe: dict[str, int | float] = {}
    for key, value in (metrics or {}).items():
        if key not in _SAFE_METRIC_KEYS or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            safe[key] = round(value, 1) if isinstance(value, float) else value
    return safe


def safe_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Allow only a small execution fingerprint, never prompts or tool data."""
    safe: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if key not in _SAFE_METADATA_KEYS or not isinstance(value, str):
            continue
        compact = " ".join(value.split())[:120]
        if not compact:
            continue
        if key == "run_kind" and compact not in RUN_KINDS:
            continue
        if key == "scope" and compact not in RESEARCH_SCOPES:
            continue
        if key == "protocol" and compact != RUN_EVENT_PROTOCOL:
            continue
        if key in {"model", "runner", "toolset"} and not all(
            character.isascii() and (character.isalnum() or character in "._:/-")
            for character in compact
        ):
            continue
        safe[key] = compact
    return safe


def execution_metadata(
    run_kind: str,
    *,
    runner: str,
    model: str = "",
    scope: str = "",
    toolset: str = "",
) -> dict[str, str]:
    """Build the stable, low-cardinality fingerprint written at Run creation."""
    metadata = {
        "protocol": RUN_EVENT_PROTOCOL,
        "run_kind": run_kind,
        "runner": runner,
        "model": model,
        "scope": scope,
        "toolset": toolset,
    }
    return safe_metadata(metadata)


def infer_persisted_event_type(
    *,
    status: str,
    stage: str,
    error_type: str = "",
    event_type: str | None = None,
) -> str:
    """Map legacy timeline fields onto the fixed durable event vocabulary."""
    if event_type in PERSISTED_EVENT_TYPES:
        return event_type
    if error_type:
        return "error"
    if stage == "tool":
        return "tool"
    if status in TERMINAL_RUN_STATUSES and stage in TERMINAL_EVENT_STAGES:
        return "done"
    return "status"


def daily_event_summary(stage: str, status: str) -> str:
    """Return a fixed daily-run label without retaining keywords or papers."""
    labels = {
        "planner": "检索计划阶段",
        "scouts": "多来源检索阶段",
        "normalizer": "字段统一与去重阶段",
        "quality_gate": "质量门控阶段",
        "curator": "候选排序阶段",
        "critic": "质量复核阶段",
        "delivery": "结果投递阶段",
        "orchestrator": "任务编排阶段",
    }
    label = labels.get(str(stage), "每日检索状态")
    suffix = {
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "partial": "部分完成",
        "partial_failed": "部分失败",
        "skipped": "已跳过",
        "waiting": "等待中",
        "running": "进行中",
    }.get(str(status), "已更新")
    return f"{label}{suffix}"

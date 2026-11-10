"""Shared, privacy-safe contracts for externally observable agent runs.

This module intentionally owns only the small vocabulary shared by chat,
deep-research and daily-discovery runs.  It is not a plugin framework or an
event bus: business-specific state continues to live in the existing
orchestrators and SQLite tables.
"""

from __future__ import annotations

from typing import Any
import math
import re


RUN_EVENT_SCHEMA_VERSION = 1
RUN_EVENT_PROTOCOL = "run-event/v1"

# The SSE contract is public. Keep it small enough for browser, CLI and future
# clients to share without exposing internal graph events.
SSE_EVENT_TYPES = frozenset({"status", "token", "tool", "done", "error"})

# Wall-clock position and elapsed time are the two numbers a timeline needs.
# They are aggregate measurements, so they stay inside the existing safe
# numeric allow-list instead of widening the public vocabulary.
TIMING_METRIC_KEYS = ("at_ms", "duration_ms")

# Statuses that close a stage.  ``partial``/``partial_failed`` close it too:
# a stage that only half-succeeded still consumed measurable wall-clock time.
TERMINAL_STAGE_STATUSES = frozenset({
    "completed", "failed", "cancelled", "partial", "partial_failed", "skipped",
})

# Token chunks are intentionally ephemeral Redis data.  Durable audit events
# exclude them so a long answer cannot turn SQLite into a stream transcript.
PERSISTED_EVENT_TYPES = frozenset({"status", "tool", "done", "error"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "partial_failed"})
TERMINAL_EVENT_STAGES = frozenset({"run", "completed", "cancelled", "failed", "delivery", "orchestrator"})
RUN_KINDS = frozenset({"chat", "research", "daily"})
RESEARCH_SCOPES = frozenset({"both", "local", "public"})

LLM_ERROR_CODES = frozenset({
    "LLM_HTTP_ERROR", "LLM_DEADLINE_EXCEEDED", "LLM_STREAM_TIMEOUT",
    "LLM_CONNECTION_ERROR", "LLM_RESPONSE_TIMEOUT", "LLM_CONNECT_TIMEOUT", "LLM_QUEUE_TIMEOUT", "LLM_QUEUE_FULL",
    "LLM_CIRCUIT_OPEN", "LLM_STREAM_INTERRUPTED", "LLM_REQUEST_FAILED",
    "LLM_OUTPUT_TRUNCATED", "LLM_CONTENT_FILTERED", "LLM_STREAM_PROTOCOL_ERROR",
})
_ERROR_PHASES = frozenset({"queue", "connect", "response_headers", "stream_read", "retry", "unknown"})
_EXCEPTION_TYPES = frozenset({
    "LLMClientError", "LLMRequestTimeoutError", "LLMRequestFailedError",
    "LLMQueueFullError", "LLMCircuitOpenError", "HTTPError", "_RetryableHTTPError",
    "ReadTimeout", "ConnectTimeout", "Timeout", "ConnectionError",
    "ChunkedEncodingError", "ReadTimeoutError", "ConnectTimeoutError",
    "ProtocolError", "TimeoutError", "ConnectionResetError", "SSLError",
    "RequestException", "JSONDecodeError", "RuntimeError", "OSError",
})


def safe_error_details(source: object) -> dict[str, Any]:
    """Allow operational diagnostics, never exception text, URLs or response bodies."""
    if not isinstance(source, dict):
        return {}
    safe: dict[str, Any] = {}
    if isinstance(source.get("code"), str) and source["code"] in LLM_ERROR_CODES:
        safe["code"] = source["code"]
    if isinstance(source.get("phase"), str) and source["phase"] in _ERROR_PHASES:
        safe["phase"] = source["phase"]
    for key in ("exception_type", "cause_type"):
        if isinstance(source.get(key), str) and source[key] in _EXCEPTION_TYPES:
            safe[key] = source[key]
    status = source.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        safe["http_status"] = status
    for key in ("elapsed_ms", "deadline_ms", "queue_wait_ms", "attempts", "retries_used"):
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        maximum = 1000 if key in {"attempts", "retries_used"} else 604_800_000
        if 0 <= value <= maximum and math.isfinite(value):
            safe[key] = int(value) if key in {"attempts", "retries_used"} else round(value, 1)
    request_id = source.get("request_id")
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", request_id):
        if not any(word in request_id.lower() for word in ("bearer", "authorization", "api_key", "api-key", "sk-")):
            safe["request_id"] = request_id
    return safe


def latest_error_details(events: list[dict] | None) -> dict[str, Any]:
    for event in reversed(events or []):
        if not isinstance(event, dict):
            continue
        details = safe_error_details(event.get("error_details"))
        if details:
            return details
    return {}


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
    "file_count",
    "unknown_count",
    "validation_errors",
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


def is_timing_metric(key: str) -> bool:
    """Whether ``key`` carries wall-clock position rather than content."""
    return key in TIMING_METRIC_KEYS


def bounded_timing_metrics(source: dict[str, Any] | None) -> dict[str, float]:
    """Project only ``at_ms``/``duration_ms`` out of an internal event.

    Timeline rendering must never be able to reach prompts, tool arguments or
    paper text, so the projection is an explicit allow-list of two numeric
    measurement keys rather than a filter over whatever the caller passed.
    """
    bounded: dict[str, float] = {}
    for key in TIMING_METRIC_KEYS:
        value = (source or {}).get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        bounded[key] = round(float(value), 1)
    return bounded


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

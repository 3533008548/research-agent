"""Helpers for the fixed, de-identified Server-Sent Events contract."""

from __future__ import annotations

import json
from typing import Any

from run_contract import SSE_EVENT_TYPES


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "")[:limit]


def public_sse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Project internal events onto the documented public SSE vocabulary."""
    event_type = str(event.get("type") or "status")
    if event_type not in SSE_EVENT_TYPES:
        return {"type": "status", "status": "running"}
    if event_type == "token":
        # Token text and the final answer are intentionally delivered in full:
        # they are the caller's own response, not an operational trace field.
        return {"type": "token", "text": str(event.get("text") or "")}
    if event_type == "tool":
        payload: dict[str, Any] = {
            "type": "tool",
            "tool": _bounded_text(event.get("tool"), 80),
            "status": _bounded_text(event.get("status") or "running", 40),
        }
        if isinstance(event.get("duration_ms"), (int, float)) and not isinstance(event["duration_ms"], bool):
            payload["duration_ms"] = round(float(event["duration_ms"]), 1)
        return payload
    if event_type == "done":
        return {
            "type": "done",
            "status": _bounded_text(event.get("status") or "completed", 40),
            "answer": str(event.get("answer") or ""),
        }
    if event_type == "error":
        return {
            "type": "error",
            "error_type": _bounded_text(event.get("error_type") or "RunError", 120),
        }
    payload = {
        "type": "status",
        "status": _bounded_text(event.get("status") or "running", 40),
    }
    if event.get("stage"):
        payload["stage"] = _bounded_text(event["stage"], 80)
    if event.get("run_id"):
        payload["run_id"] = _bounded_text(event["run_id"], 120)
    return payload


def format_sse(event: dict[str, Any]) -> str:
    """Serialize one event while enforcing the public event vocabulary."""
    public_event = public_sse_event(event)
    data = json.dumps(public_event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {public_event['type']}\ndata: {data}\n\n"

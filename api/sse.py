"""Helpers for the fixed, de-identified Server-Sent Events contract."""

from __future__ import annotations

import json
from typing import Any

from run_contract import SSE_EVENT_TYPES, bounded_timing_metrics, safe_error_details


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
        payload.update(bounded_timing_metrics(event))
        for key, limit in (("call_id", 120), ("agent", 80), ("error_type", 120)):
            if event.get(key):
                payload[key] = _bounded_text(event[key], limit)
        return payload
    if event_type == "done":
        payload = {
            "type": "done",
            "status": _bounded_text(event.get("status") or "completed", 40),
            "answer": str(event.get("answer") or ""),
        }
        details = safe_error_details(event.get("error_details"))
        if details:
            payload["error_details"] = details
        return payload
    if event_type == "error":
        payload = {
            "type": "error",
            "error_type": _bounded_text(event.get("error_type") or "RunError", 120),
        }
        details = safe_error_details(event.get("error_details"))
        if details:
            payload["error_details"] = details
        return payload
    payload = {
        "type": "status",
        "status": _bounded_text(event.get("status") or "running", 40),
    }
    if event.get("stage"):
        payload["stage"] = _bounded_text(event["stage"], 80)
    if event.get("verification_round") in (1, 2):
        payload["verification_round"] = event["verification_round"]
    if event.get("error_type"):
        payload["error_type"] = _bounded_text(event["error_type"], 120)
    if event.get("run_id"):
        payload["run_id"] = _bounded_text(event["run_id"], 120)
    # Status events also carry the two bounded timing numbers so a live
    # timeline needs no second request to place a finished stage.
    payload.update(bounded_timing_metrics(event))
    details = safe_error_details(event.get("error_details"))
    if details:
        payload["error_details"] = details
    return payload


def format_sse(event: dict[str, Any]) -> str:
    """Serialize one event while enforcing the public event vocabulary."""
    public_event = public_sse_event(event)
    data = json.dumps(public_event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {public_event['type']}\ndata: {data}\n\n"

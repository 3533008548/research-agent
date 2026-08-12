"""Helpers for the fixed, de-identified Server-Sent Events contract."""

from __future__ import annotations

import json
from typing import Any


SSE_EVENT_TYPES = frozenset({"status", "token", "tool", "done", "error"})


def format_sse(event: dict[str, Any]) -> str:
    """Serialize one event while enforcing the public event vocabulary."""
    event_type = str(event.get("type") or "status")
    if event_type not in SSE_EVENT_TYPES:
        event_type = "status"
        event = {"type": "status", "status": "running"}
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"

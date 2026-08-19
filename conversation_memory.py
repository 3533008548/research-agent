"""Conversation-summary policy kept outside the LangGraph node implementation.

The graph decides *when* a successful turn may be compacted.  This module owns
the mechanics: frequency gating, bounded source construction, the low-priority
LLM request and durable storage.  Keeping those details here makes the graph's
verification path easier to read and the policy independently testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from cancellation import RequestCancelledError
from llm_client import RequestPolicy, RequestPriority


MIN_USER_MESSAGES = 11
MIN_CONTEXT_RATIO = 0.5
SUMMARY_COOLDOWN = timedelta(minutes=10)
RECENT_MESSAGE_LIMIT = 20
MESSAGE_CHAR_LIMIT = 300
SOURCE_CHAR_LIMIT = 3_000
SUMMARY_CHAR_LIMIT = 300


def context_usage_ratio(token_usage: dict[str, Any] | None) -> float:
    """Return a safe 0..1+ context usage ratio from optional token metrics."""
    if not token_usage:
        return 0.0
    try:
        used = max(0, int(token_usage.get("last_prompt", 0) or 0))
        limit = max(1, int(token_usage.get("context_limit", 131_072) or 131_072))
    except (TypeError, ValueError):
        return 0.0
    return used / limit


def _has_recent_summary(memory_store, thread_id: str, now: datetime) -> bool:
    last_summary_time = memory_store.get_last_summary_time(thread_id)
    if not last_summary_time:
        return False
    try:
        return datetime.fromisoformat(last_summary_time) + SUMMARY_COOLDOWN > now
    except (TypeError, ValueError):
        return False


def _summary_source(messages: list[dict[str, Any]]) -> str:
    recent = []
    for message in messages[-RECENT_MESSAGE_LIMIT:]:
        content = str(message.get("content") or "")[:MESSAGE_CHAR_LIMIT]
        recent.append(f"[{message.get('role', '')}] {content}")
    return "\n".join(recent)[:SOURCE_CHAR_LIMIT]


def maybe_store_conversation_summary(
    *,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    context_ratio: float,
    memory_store,
    llm_client,
    model: str,
    timeout_seconds: int,
    cancel_event=None,
    now: datetime | None = None,
) -> bool:
    """Persist one bounded summary when the conversation crosses its threshold.

    Returns whether a new summary was stored.  Optional summarization failures
    are intentionally non-fatal; user cancellation is still propagated.
    """
    thread_id = str(metadata.get("session_id") or "research-main")
    user_message_count = sum(message.get("role") == "user" for message in messages)
    if user_message_count < MIN_USER_MESSAGES and context_ratio <= MIN_CONTEXT_RATIO:
        return False
    if _has_recent_summary(memory_store, thread_id, now or datetime.now()):
        return False

    source = _summary_source(messages)
    if not source:
        return False
    prompt = f"Summarize this research conversation in 150 chars Chinese:\n{source}"
    try:
        budget = llm_client.new_request_budget(
            RequestPolicy(
                purpose="summary",
                priority=RequestPriority.SUMMARY,
                deadline_seconds=timeout_seconds,
                max_retries=0,
            ),
        )
        response = llm_client.post(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0.2,
            },
            stream=False,
            budget=budget,
            cancel_event=cancel_event,
        )
        if response.status_code != 200:
            return False
        summary = str(response.json()["choices"][0]["message"]["content"]).strip()
        if not summary:
            return False
        memory_store.add_summary(
            thread_id,
            str(metadata.get("topic") or ""),
            summary[:SUMMARY_CHAR_LIMIT],
        )
        return True
    except RequestCancelledError:
        raise
    except Exception:
        return False

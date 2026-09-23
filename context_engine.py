"""Hermes-inspired model-input compaction with checkpoint preservation.

LangGraph remains the durable transcript owner.  This module only prepares a
smaller *view* for an LLM call, so an interrupted compaction cannot erase chat
history.  It deliberately uses deterministic, clearly-labelled excerpts for
now; a future auxiliary summariser can replace ``archive_context`` without
changing the graph integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_THRESHOLD_RATIO = 0.50
DEFAULT_PROTECT_FIRST_MESSAGES = 2
DEFAULT_PROTECT_LAST_MESSAGES = 20
DEFAULT_TAIL_RATIO = 0.025
DEFAULT_MIN_TAIL_TOKENS = 10_000
DEFAULT_MAX_TAIL_TOKENS = 25_000
_ARCHIVE_EXCERPT_COUNT = 10
_ARCHIVE_EXCERPT_CHARS = 500
_ARCHIVE_MAX_CHARS = 6_000
_OLD_TOOL_RESULT_CHARS = 800
_OLD_MESSAGE_CHARS = 2_000


@dataclass(frozen=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    archive_context: str = ""
    original_tokens: int = 0
    prepared_tokens: int = 0
    compacted: bool = False


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """A bounded CJK-friendly fallback until provider usage is available."""
    chars = 0
    for message in messages:
        chars += len(str(message.get("content") or ""))
        # DeepSeek thinking-mode tool turns must keep this field verbatim.
        # Count it so compaction can remove whole old turns before a growing
        # chain of thought makes a later request slow or oversized.
        chars += len(str(message.get("reasoning_content") or ""))
        # Tool call arguments are part of a compatible chat payload too.
        chars += len(str(message.get("tool_calls") or ""))
    return max(0, chars // 3)


def prepare_model_context(
    messages: list[dict[str, Any]],
    *,
    context_limit: int,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    protect_first_messages: int = DEFAULT_PROTECT_FIRST_MESSAGES,
    protect_last_messages: int = DEFAULT_PROTECT_LAST_MESSAGES,
    tail_token_budget: int | None = None,
) -> PreparedContext:
    """Return a safe compact prompt view when estimated pressure is high.

    The model receives the first exchange, a labelled archive excerpt and the
    latest intact conversational tail.  Tool-call/result groups at a boundary
    are kept together.  The incoming list is never mutated.
    """

    original = [dict(message) for message in messages]
    original_tokens = estimate_message_tokens(original)
    try:
        resolved_limit = max(1, int(context_limit))
    except (TypeError, ValueError):
        resolved_limit = 131_072
    threshold = max(1, int(resolved_limit * min(0.95, max(0.05, threshold_ratio))))
    if original_tokens < threshold or len(original) <= 4:
        return PreparedContext(original, original_tokens=original_tokens, prepared_tokens=original_tokens)

    first_count = max(0, min(int(protect_first_messages), len(original)))
    head_end = _align_head_boundary(original, first_count)
    tail_budget = tail_token_budget
    if tail_budget is None:
        tail_budget = min(
            max(int(resolved_limit * DEFAULT_TAIL_RATIO), DEFAULT_MIN_TAIL_TOKENS),
            DEFAULT_MAX_TAIL_TOKENS,
        )
    tail_budget = max(1, int(tail_budget))
    tail_start = _tail_start(
        original,
        min_start=head_end,
        token_budget=tail_budget,
        min_messages=max(1, int(protect_last_messages)),
    )
    tail_start = _align_tail_boundary(original, tail_start, min_start=head_end)
    if tail_start <= head_end:
        # There is no removable middle.  Preserve the checkpoint-backed list
        # exactly instead of pretending that a compaction helped.
        return PreparedContext(original, original_tokens=original_tokens, prepared_tokens=original_tokens)

    head = [_slim_old_message(message) for message in original[:head_end]]
    tail = [_slim_old_message(message) for message in original[tail_start:]]
    archive = _archive_context(original[head_end:tail_start])
    prepared = head + tail
    return PreparedContext(
        prepared,
        archive_context=archive,
        original_tokens=original_tokens,
        prepared_tokens=estimate_message_tokens(prepared) + len(archive) // 3,
        compacted=True,
    )


def _tail_start(
    messages: list[dict[str, Any]], *, min_start: int, token_budget: int, min_messages: int,
) -> int:
    start = len(messages)
    used = 0
    protected = 0
    for index in range(len(messages) - 1, min_start - 1, -1):
        message_tokens = estimate_message_tokens([_slim_old_message(messages[index])])
        if protected >= min_messages and used + message_tokens > token_budget:
            break
        used += message_tokens
        protected += 1
        start = index
    return start


def _align_head_boundary(messages: list[dict[str, Any]], boundary: int) -> int:
    while boundary < len(messages) and messages[boundary].get("role") == "tool":
        boundary += 1
    return boundary


def _align_tail_boundary(messages: list[dict[str, Any]], boundary: int, *, min_start: int) -> int:
    # Never leave a tool result in the tail without the assistant call that
    # produced it.  The graph has already sanitized invalid pairs before this
    # engine runs, so walking back to the preceding non-tool message is enough.
    while boundary > min_start and messages[boundary].get("role") == "tool":
        boundary -= 1
    return boundary


def _slim_old_message(message: dict[str, Any]) -> dict[str, Any]:
    result = dict(message)
    content = str(result.get("content") or "")
    limit = _OLD_TOOL_RESULT_CHARS if result.get("role") == "tool" else _OLD_MESSAGE_CHARS
    if len(content) <= limit:
        return result
    if result.get("role") == "tool":
        result["content"] = (
            "[历史工具输出已从当前模型上下文截断；完整原文仍在会话 checkpoint 中]\n"
            + content[:limit]
        )
    else:
        result["content"] = content[:limit] + "\n[历史消息其余部分已省略]"
    return result


def _archive_context(middle: list[dict[str, Any]]) -> str:
    excerpts: list[str] = []
    for message in middle:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = " ".join(str(message.get("content") or "").split())
        if content:
            excerpts.append(f"[{role}] {content[:_ARCHIVE_EXCERPT_CHARS]}")
    excerpts = excerpts[-_ARCHIVE_EXCERPT_COUNT:]
    if not excerpts:
        return ""
    body = "\n".join(excerpts)[:_ARCHIVE_MAX_CHARS]
    return (
        "[早期对话已为本次模型调用压缩]\n"
        "以下是从被压缩区间提取的原文片段，不是新的事实或证据；"
        "完整消息仍保存在本地 checkpoint。\n"
        f"{body}"
    )

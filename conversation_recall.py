"""Selective replay of prior visible session messages."""

from __future__ import annotations

import re
from typing import Any


_RECALL_REFERENCE = re.compile(
    r"(?:之前|以前|刚才|上面|前面|此前|继续|接着|这个|这条|第[一二三四五六七八九十0-9])"
)


def should_recall_session_history(user_text: str) -> bool:
    """Avoid paying a prompt cost for ordinary standalone questions."""
    return bool(_RECALL_REFERENCE.search(str(user_text or "")))


def render_session_recall(messages: list[dict[str, Any]]) -> str:
    """Render retrieved messages as conversation context, never as evidence."""
    excerpts: list[str] = []
    for item in messages[:3]:
        role = str(item.get("role") or "")
        content = " ".join(str(item.get("content") or "").split())[:700]
        if role in {"user", "assistant"} and content:
            excerpts.append(f"[{role}] {content}")
    if not excerpts:
        return ""
    return (
        "[按需召回的早期会话片段]\n"
        "这些是此前对话内容，不能替代论文或外部证据；若与本轮用户指令冲突，以本轮为准。\n"
        + "\n".join(excerpts)
    )

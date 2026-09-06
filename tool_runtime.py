"""A small, shared execution boundary for model-facing tools.

The project deliberately keeps its existing tool functions and LangGraph
topology.  This module centralizes only cross-cutting concerns that otherwise
drift between tool-call sites: capability checks, cooperative cancellation,
bounded model-visible results and privacy-safe lifecycle events.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from cancellation import RequestCancelledError, raise_if_cancelled
from tool_catalog import get_tool_definition
from tools import execute_tool


class ToolDeadlineExceeded(RuntimeError):
    """A tool reached a caller-provided cooperative deadline."""


@dataclass(frozen=True)
class ToolExecutionContext:
    """Stable, non-content context for one model-facing tool invocation."""

    run_kind: str = "chat"
    run_id: str = ""
    session_id: str = ""
    research_scope: str = ""
    allowed_tool_names: frozenset[str] | None = None
    cancel_event: threading.Event | None = None
    deadline_monotonic: float | None = None


ToolExecutor = Callable[..., str]


class ToolRuntime:
    """Execute existing tools through one bounded, observable policy boundary."""

    def __init__(
        self,
        context: ToolExecutionContext,
        *,
        event_callback: Callable[..., None] | None = None,
        executor: ToolExecutor = execute_tool,
    ) -> None:
        self.context = context
        self._event_callback = event_callback
        self._executor = executor

    def _emit(self, event_type: str, **details: Any) -> None:
        if self._event_callback is None:
            return
        try:
            self._event_callback(event_type, **details)
        except Exception:
            # Instrumentation must never break a user-visible tool call.
            return

    def _ensure_active(self, stage: str) -> None:
        raise_if_cancelled(self.context.cancel_event, f"工具调用已在 {stage} 取消")
        if (
            self.context.deadline_monotonic is not None
            and time.monotonic() >= self.context.deadline_monotonic
        ):
            raise ToolDeadlineExceeded(f"工具调用已超过截止时间：{stage}")

    def _is_allowed(self, name: str) -> bool:
        allowed = self.context.allowed_tool_names
        return allowed is None or name in allowed

    @staticmethod
    def _truncate_result(result: object, limit: int) -> str:
        text = result if isinstance(result, str) else str(result)
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n\n...（截断至 {limit} 字符）"

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        paper_store=None,
        vision_model: str = "",
        profile_manager=None,
        memory_store=None,
        llm_client=None,
        model: str = "",
    ) -> str:
        """Run one existing tool without exposing its arguments or result to traces."""
        name = str(name or "")[:80]
        definition = get_tool_definition(name)
        if definition is None:
            self._emit("tool_failed", tool=name or "unknown", error_type="UnknownTool")
            return f"❌ 未知工具: {name or 'unknown'}"
        if not self._is_allowed(name):
            self._emit("tool_failed", tool=name, error_type="ToolAccessDenied")
            return f"Tool is not available in this {self.context.run_kind} role: {name}"

        self._ensure_active("before_tool")
        started_at = time.perf_counter()
        self._emit("tool_started", tool=name)
        try:
            result = self._executor(
                name,
                args,
                paper_store=paper_store,
                vision_model=vision_model,
                profile_manager=profile_manager,
                memory_store=memory_store,
                llm_client=llm_client,
                model=model,
                session_id=self.context.session_id,
                cancel_event=self.context.cancel_event,
            )
            self._ensure_active("after_tool")
        except RequestCancelledError:
            self._emit(
                "tool_failed",
                tool=name,
                duration_ms=round((time.perf_counter() - started_at) * 1_000, 1),
                error_type="RequestCancelledError",
            )
            raise
        except Exception as exc:
            self._emit(
                "tool_failed",
                tool=name,
                duration_ms=round((time.perf_counter() - started_at) * 1_000, 1),
                error_type=type(exc).__name__,
            )
            raise

        text = self._truncate_result(result, definition.result_limit)
        self._emit(
            "tool_finished",
            tool=name,
            duration_ms=round((time.perf_counter() - started_at) * 1_000, 1),
            rag_keyword_fallback=name == "query_papers" and "关键词候选" in text,
        )
        return text

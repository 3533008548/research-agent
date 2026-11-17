"""A small, shared execution boundary for model-facing tools.

The project deliberately keeps its existing tool functions and LangGraph
topology.  This module centralizes only cross-cutting concerns that otherwise
drift between tool-call sites: capability checks, cooperative cancellation,
bounded model-visible results and privacy-safe lifecycle events.
"""

from __future__ import annotations

import threading
import time
import uuid
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from cancellation import RequestCancelledError, raise_if_cancelled
from tool_catalog import get_tool_definition
from tools import execute_tool
from tool_results import (HEADER, RESULT_TOOLS, ToolResultStore, handle_result_tool,
                          page_view, source_reference)
from tool_errors import ToolFailure, failure_details, failure_from_exception


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
    result_db: str = ""
    result_session_id: str = ""


ToolExecutor = Callable[..., str]


class ToolRuntime:
    """Execute existing tools through one bounded, observable policy boundary."""

    def __init__(
        self,
        context: ToolExecutionContext,
        *,
        event_callback: Callable[..., None] | None = None,
        executor: ToolExecutor = execute_tool,
        result_db: str | None = None,
    ) -> None:
        self.context = context
        self._event_callback = event_callback
        self._executor = executor
        self._result_db = context.result_db or result_db
        self._result_session = context.result_session_id or context.session_id or 'anonymous-' + uuid.uuid4().hex
        self._result_store = None

    def result_store(self):
        if self._result_store is None:
            from runtime_paths import get_runtime_paths
            self._result_store = ToolResultStore(self._result_db or get_runtime_paths().checkpoint_db)
        return self._result_store

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
        return allowed is None or name in allowed or (name in RESULT_TOOLS and bool(allowed - RESULT_TOOLS))

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
        call_id = uuid.uuid4().hex
        definition = get_tool_definition(name)
        def rejected(code, error_type, phase='access'):
            failure = ToolFailure(code=code, phase=phase, retryable=False)
            self._emit('tool_failed', tool=name or 'unknown', call_id=call_id,
                       error_type=error_type, error_details=failure.error_details)
            return str(failure)
        if definition is None:
            return rejected('TOOL_UNKNOWN', 'UnknownTool')
        if not self._is_allowed(name):
            return rejected('TOOL_ACCESS_DENIED', 'ToolAccessDenied')

        if name == "read_pdf" and self.context.run_kind == "research":
            remote = str(args.get("url_or_path", "")).strip().lower().startswith(("https://", "http://"))
            scope = self.context.research_scope
            if (scope == "local" and remote) or (scope == "public" and not remote):
                return rejected('TOOL_SOURCE_SCOPE_DENIED', 'SourceScopeDenied')

        self._ensure_active("before_tool")
        started_at = time.perf_counter()
        self._emit("tool_started", tool=name, call_id=call_id)
        try:
            if name in RESULT_TOOLS:
                try:
                    text = handle_result_tool(name, args, store=self.result_store(),
                                              session_id=self._result_session,
                                              allowed_tools=self.context.allowed_tool_names,
                                              scope=self.context.research_scope)
                except (ValueError, OSError, sqlite3.Error):
                    text = ToolFailure(code='TOOL_RESULT_READ_FAILED', phase='result_storage', retryable=False)
                self._ensure_active('after_result_read')
                self._emit('tool_failed' if text.startswith('❌') else 'tool_finished', tool=name, call_id=call_id,
                           **({'error_type': 'ToolResultError', 'error_details': failure_details(text)} if text.startswith('❌') else {}),
                           duration_ms=round((time.perf_counter() - started_at) * 1000, 1))
                return str(text)
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
        except (RequestCancelledError, ToolDeadlineExceeded) as exc:
            self._emit(
                "tool_failed",
                tool=name,
                call_id=call_id,
                duration_ms=round((time.perf_counter() - started_at) * 1_000, 1),
                error_type=type(exc).__name__,
            )
            raise
        except Exception as exc:
            self._ensure_active('after_tool_error')
            result = failure_from_exception(exc)

        raw = result if isinstance(result, str) else str(result)
        diagnostics = getattr(result, 'error_details', {}) or failure_details(raw)
        if diagnostics and not isinstance(raw, ToolFailure):
            raw = ToolFailure(code=diagnostics.get('code', 'TOOL_REPORTED_FAILURE'),
                              phase=diagnostics.get('phase', 'unknown'), retryable=diagnostics.get('retryable'),
                              evidence_status=diagnostics.get('evidence_status', 'unknown'), details=diagnostics)
            diagnostics = raw.error_details
        if raw.lstrip().startswith('❌') and not diagnostics:
            raw = ToolFailure(code='TOOL_REPORTED_FAILURE', evidence_status='unknown', legacy_reason=raw)
            diagnostics = raw.error_details
        try:
            record = self.result_store().save(
                session_id=self._result_session, run_id=self.context.run_id,
                tool=name, scope=self.context.research_scope, source=source_reference(args), content=raw,
            )
            text = raw if diagnostics or (len(raw) <= 800 and not raw.startswith(HEADER)) else page_view(record, raw, limit=definition.result_limit, budget=definition.result_limit)
        except (OSError, sqlite3.Error):
            # The action already happened. Never retry a write tool just to recover its output.
            failure = ToolFailure(code='TOOL_RESULT_STORAGE_FAILED', phase='result_storage', evidence_status='unknown')
            self._emit('tool_failed', tool=name, call_id=call_id, error_type='ToolResultStorageError', error_details=failure.error_details)
            return str(failure)
        self._emit(
            "tool_failed" if raw.lstrip().startswith("❌") else "tool_finished",
            tool=name,
            call_id=call_id,
            **({"error_type": "ToolResultError", "error_details": diagnostics} if diagnostics else {}),
            duration_ms=round((time.perf_counter() - started_at) * 1_000, 1),
            rag_keyword_fallback=name == "query_papers" and "关键词候选" in text,
        )
        return str(text)

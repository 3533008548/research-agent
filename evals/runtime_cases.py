"""Real-orchestration, offline replay cases for reliability regressions.

Each case isolates its SQLite state in a temporary directory.  It exercises
the production session, research, daily-source, recovery, and cancellation
code, while substituting only the external boundaries defined in
``runtime_fakes``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from cancellation import raise_if_cancelled
from daily_orchestrator import DailyResearchOrchestrator
from research_orchestrator import ResearchOrchestrator
from scheduler import Scheduler
from session_store import SessionStore

from .runtime_fakes import FixtureResponse, FixtureSourceRouter, ScriptedResearchModels


_SENSITIVE_EVENT_FIELDS = {
    "answer", "api_key", "candidate", "candidates", "content", "doi", "evidence",
    "excerpt", "keyword", "message", "prompt", "query", "text", "title", "token", "url",
}


@dataclass(frozen=True)
class RuntimeCase:
    case_id: str
    title: str
    execute: Callable[["RuntimeContext"], "RuntimeCaseResult"]


@dataclass
class RuntimeContext:
    work_dir: Path
    fixtures_dir: Path
    seed: int
    events: list[dict[str, str]] = field(default_factory=list)

    def event(self, stage: str, status: str) -> None:
        """Store only stage/status; never persist prompts or provider payloads."""
        self.events.append({"stage": stage, "status": status})


@dataclass
class RuntimeCaseResult:
    case_id: str
    title: str
    assertions: list[dict[str, Any]]
    duration_ms: float
    events: list[dict[str, str]]
    metrics: dict[str, int | float] = field(default_factory=dict)
    source_stats: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None

    @property
    def passed(self) -> bool:
        return self.error_type is None and all(item["passed"] for item in self.assertions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "passed": self.passed,
            "assertions": _sanitize(self.assertions),
            "duration_ms": self.duration_ms,
            "metrics": _sanitize(self.metrics),
            "source_stats": _sanitize(self.source_stats),
            "events": _sanitize(self.events),
            "error_type": self.error_type,
        }


def _sanitize(value: Any) -> Any:
    """Defence in depth for reports: retain structure but remove user/data payloads."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.casefold() in _SENSITIVE_EVENT_FIELDS else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and len(value) > 240:
        return "<redacted>"
    return value


def _assert(assertions: list[dict[str, Any]], label: str, condition: bool) -> None:
    assertions.append({"label": label, "passed": bool(condition)})


def _completed(
    case: RuntimeCase,
    started: float,
    context: RuntimeContext,
    assertions: list[dict[str, Any]],
    *,
    metrics: dict[str, int | float] | None = None,
    source_stats: dict[str, Any] | None = None,
) -> RuntimeCaseResult:
    return RuntimeCaseResult(
        case_id=case.case_id,
        title=case.title,
        assertions=assertions,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        events=list(context.events),
        metrics=metrics or {},
        source_stats=source_stats or {},
    )


def _r01_session_isolation_and_delete(context: RuntimeContext) -> RuntimeCaseResult:
    case = CASES_BY_ID["R01"]
    started = time.perf_counter()
    assertions: list[dict[str, Any]] = []
    store = SessionStore(str(context.work_dir / "checkpoint.db"))
    try:
        context.event("session_a", "created")
        session_a = store.create("offline-a")
        session_b = store.create("offline-b")
        store.touch(session_a["thread_id"], "private-a")
        store.touch(session_b["thread_id"], "private-b")
        run_a = store.create_research_run(session_a["thread_id"], "offline query")
        store.update_research_run(
            run_a["run_id"], status="failed", evidence=[{"id": "", "source": "fixture"}],
        )
        context.event("session_a", "deleted")
        deleted = store.delete(session_a["thread_id"])
        _assert(assertions, "deleting one session reports success", deleted)
        _assert(assertions, "deleted session is absent", store.get(session_a["thread_id"]) is None)
        _assert(assertions, "deleted session research run is absent", store.get_research_run(run_a["run_id"]) is None)
        _assert(assertions, "other session remains isolated", store.get(session_b["thread_id"]) is not None)
        _assert(assertions, "other session preview is retained", store.get(session_b["thread_id"]).get("preview") == "private-b")
    finally:
        store.close()
    return _completed(case, started, context, assertions)


def _r02_tool_trace_sanitization(context: RuntimeContext) -> RuntimeCaseResult:
    case = CASES_BY_ID["R02"]
    started = time.perf_counter()
    assertions: list[dict[str, Any]] = []
    from config import Config
    from research_agent import ResearchAgent

    class _FixturePaperStore:
        def query_with_timeout(self, _query: str, *, top_k: int, section: str | None = None):
            return ([{
                "retrieval": "keyword", "title": "Fixture local paper", "section": section or "Methods",
                "keyword_score": 1.0, "text": "offline evidence",
            }][:top_k], None)

    prior_app_data_dir = os.environ.get("APP_DATA_DIR")
    agent = ResearchAgent(Config(deepseek_key="offline-key", rag_enabled=False, data_dir=str(context.work_dir)))
    try:
        # The graph and tool dispatcher are real.  Only its model transport is
        # scripted: first response requests query_papers, second returns text.
        agent._paper_store = _FixturePaperStore()
        scripted_payloads = iter([
            {
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "fixture-tool-call",
                    "function": {
                        "name": "query_papers",
                        "arguments": '{"query":"offline fixture","top_k":1}',
                    },
                }]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
            },
            {
                "choices": [{"message": {"content": "离线工具回放完成"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            },
        ])

        def fake_post(_payload: dict[str, Any], **_kwargs: Any) -> FixtureResponse:
            try:
                return FixtureResponse(payload=next(scripted_payloads))
            except StopIteration as exc:
                raise AssertionError("offline graph made an unexpected model request") from exc

        sentinel = "offline-private-input-must-not-leak"
        with patch.object(agent.llm_client, "post", side_effect=fake_post):
            answer = agent.step(sentinel)
        trace = agent.get_last_trace() or {}
        context.event("tool_dispatch", "completed")
        _assert(assertions, "tool returned an assistant response", answer == "离线工具回放完成")
        _assert(assertions, "trace records query_papers", trace.get("tool_trace", [{}])[0].get("tool") == "query_papers")
        _assert(assertions, "trace excludes input payload", sentinel not in str(trace))
        _assert(assertions, "trace exposes success outcome", trace.get("outcome") == "success")
        metrics = {
            "tool_events": len(trace.get("tool_trace") or []),
            "model_calls": int((trace.get("usage_delta") or {}).get("calls", 0)),
        }
    finally:
        agent.memory.close()
        agent.sessions.close()
        if prior_app_data_dir is None:
            os.environ.pop("APP_DATA_DIR", None)
        else:
            os.environ["APP_DATA_DIR"] = prior_app_data_dir
    return _completed(case, started, context, assertions, metrics=metrics)


def _r03_research_resume(context: RuntimeContext) -> RuntimeCaseResult:
    case = CASES_BY_ID["R03"]
    started = time.perf_counter()
    assertions: list[dict[str, Any]] = []
    store = SessionStore(str(context.work_dir / "checkpoint.db"))
    try:
        session = store.create("resume-fixture")
        previous = store.create_research_run(session["thread_id"], "offline research question")
        evidence = [{
            "id": "", "researcher": "local", "source_type": "query_papers",
            "source": "Fixture evidence", "excerpt": "stored offline evidence", "uncertainty": "fixture",
        }]
        store.update_research_run(
            previous["run_id"], status="failed", plan={"objective": "offline", "subtasks": []}, evidence=evidence,
        )
        models = ScriptedResearchModels()
        orchestrator = ResearchOrchestrator(
            session_store=store,
            api_key="offline-key",
            model="offline-model",
            paper_store=None,
            glm_api_key="",
            llm_client=None,
            verify_timeout_seconds=3,
        )

        def no_workers(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("resume with stored evidence must not restart workers")

        with patch.object(orchestrator, "_call_model", side_effect=models), \
             patch.object(orchestrator, "_run_workers", side_effect=no_workers):
            result = orchestrator.run("", thread_id=session["thread_id"], resume=True)
        context.event("research_resume", result.status)
        saved = store.get_research_run(previous["run_id"]) or {}
        _assert(assertions, "resume keeps the original run id", result.run_id == previous["run_id"])
        _assert(assertions, "resume completes", result.status == "completed")
        _assert(assertions, "resume skips planner and workers", models.stages == ["synthesis", "critic"])
        _assert(assertions, "renumbered evidence is persisted", saved.get("evidence", [{}])[0].get("id") == "E1")
        return _completed(
            case, started, context, assertions,
            metrics={"model_calls": int(result.usage.get("calls", 0))},
        )
    finally:
        store.close()


def _new_daily_orchestrator(store: Scheduler) -> DailyResearchOrchestrator:
    return DailyResearchOrchestrator(
        scheduler=store,
        llm_client=None,
        model="",
        max_keyword_concurrency=1,
        max_results_per_keyword=1,
        daily_sources=("openalex", "openaire", "dblp"),
        openalex_api_key="offline-key-not-reported",
    )


def _r04_daily_partial_failure_resume(context: RuntimeContext) -> RuntimeCaseResult:
    case = CASES_BY_ID["R04"]
    started = time.perf_counter()
    assertions: list[dict[str, Any]] = []
    store = Scheduler(str(context.work_dir / "daily.db"))
    try:
        store.add_keyword("offline replay")
        router = FixtureSourceRouter(context.fixtures_dir, failed_sources={"openaire"}, seed=context.seed)
        orchestrator = _new_daily_orchestrator(store)
        with patch("daily_orchestrator.requests.get", side_effect=router.get):
            first = orchestrator.run("daily")
            calls_after_first = dict(router.calls)
            resumed = orchestrator.run("daily", resume=True)
        context.event("daily_sources", first.status)
        context.event("daily_resume", resumed.status)
        saved_candidates = store.get_daily_candidates(first.run_id)
        _assert(assertions, "one source failure is retained as partial_failed", first.status == "partial_failed")
        _assert(assertions, "healthy sources still produce candidates", len(first.candidates) >= 2)
        _assert(assertions, "failed provider is observable", first.source_stats["offline replay"]["openaire"]["status"] == "failed")
        _assert(assertions, "resume reuses persisted candidates", dict(router.calls) == calls_after_first)
        _assert(assertions, "resume preserves the run id", resumed.run_id == first.run_id)
        _assert(assertions, "candidate persistence survives partial failure", len(saved_candidates) == len(first.candidates))
        return _completed(
            case, started, context, assertions,
            metrics={"source_transport_calls": sum(router.calls.values())},
            source_stats=first.source_stats,
        )
    finally:
        store.close()


def _r05_cancellation_propagation(context: RuntimeContext) -> RuntimeCaseResult:
    case = CASES_BY_ID["R05"]
    started = time.perf_counter()
    assertions: list[dict[str, Any]] = []
    import threading

    store = Scheduler(str(context.work_dir / "daily.db"))
    try:
        store.add_keyword("offline cancellation")
        router = FixtureSourceRouter(context.fixtures_dir, seed=context.seed)
        orchestrator = _new_daily_orchestrator(store)
        cancel_event = threading.Event()
        curator_calls = 0

        def cancel_at_curator(_candidates: list[dict[str, Any]], event: threading.Event | None):
            nonlocal curator_calls
            curator_calls += 1
            cancel_event.set()
            raise_if_cancelled(event, "offline cancellation")
            raise AssertionError("cancellation did not propagate to curator")

        with patch("daily_orchestrator.requests.get", side_effect=router.get), \
             patch.object(orchestrator, "_curate", side_effect=cancel_at_curator):
            result = orchestrator.run("daily", cancel_event=cancel_event)
        context.event("curator", "cancelled")
        persisted = store.get_daily_candidates(result.run_id)
        event_pairs = {(item["agent"], item["status"]) for item in store.get_daily_agent_events(result.run_id)}
        _assert(assertions, "curator observes cancellation", curator_calls == 1)
        _assert(assertions, "run ends as cancelled", result.status == "cancelled")
        _assert(assertions, "candidates are persisted before cancellation", len(persisted) == len(result.candidates) and bool(persisted))
        _assert(assertions, "delivery is not emitted after cancellation", not any(agent == "delivery" for agent, _ in event_pairs))
        _assert(assertions, "cancelled event is persisted", ("orchestrator", "cancelled") in event_pairs)
        return _completed(
            case, started, context, assertions,
            metrics={"source_transport_calls": sum(router.calls.values())},
            source_stats=result.source_stats,
        )
    finally:
        store.close()


CASES = (
    RuntimeCase("R01", "会话隔离与删除", _r01_session_isolation_and_delete),
    RuntimeCase("R02", "工具轨迹脱敏", _r02_tool_trace_sanitization),
    RuntimeCase("R03", "深度研究基于证据恢复", _r03_research_resume),
    RuntimeCase("R04", "每日来源局部失败后恢复", _r04_daily_partial_failure_resume),
    RuntimeCase("R05", "取消传播与候选保留", _r05_cancellation_propagation),
)
CASES_BY_ID = {case.case_id: case for case in CASES}


def select_cases(case_ids: list[str] | None = None) -> list[RuntimeCase]:
    if not case_ids:
        return list(CASES)
    requested = [case_id.upper() for case_id in case_ids]
    unknown = sorted(set(requested) - set(CASES_BY_ID))
    if unknown:
        raise ValueError(f"unknown runtime replay case(s): {', '.join(unknown)}")
    return [CASES_BY_ID[case_id] for case_id in requested]

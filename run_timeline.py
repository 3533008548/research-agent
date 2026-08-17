"""Safe, read-only projection of agent execution records for the web UI.

The session database owns ordinary chat and deep-research runs.  Daily search
continues to own its data in ``daily.db``.  This module is the only place that
combines their *operational* views, and deliberately never returns prompts,
answers, keywords, candidate papers, tool payloads or daily-event messages.
"""

from __future__ import annotations

from html import escape
from typing import Any


_STATUS_LABELS = {
    "running": "运行中",
    "waiting": "等待中",
    "streaming": "输出中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "partial": "部分完成",
    "partial_failed": "部分失败",
    "resumed": "已恢复",
}

_KIND_LABELS = {
    "chat": "单 Agent 对话",
    "research": "深度研究",
    "daily": "每日检索",
}

_DAILY_STAGE_LABELS = {
    "planner": "检索计划阶段",
    "scouts": "多来源检索阶段",
    "normalizer": "字段统一与去重阶段",
    "quality_gate": "质量门控阶段",
    "curator": "候选排序阶段",
    "critic": "质量复核阶段",
    "delivery": "结果投递阶段",
    "orchestrator": "任务编排阶段",
}


class RunTimelineService:
    """Expose a small and presentation-safe timeline for the Gradio UI."""

    def __init__(self, sessions, scheduler) -> None:
        self.sessions = sessions
        self.scheduler = scheduler

    @staticmethod
    def _safe_time(value: object) -> str:
        return str(value or "")[:19].replace("T", " ")

    @staticmethod
    def _safe_duration(value: object) -> str:
        if not isinstance(value, (int, float)):
            return ""
        milliseconds = max(0, float(value))
        if milliseconds < 1_000:
            return f"{milliseconds:.0f} ms"
        if milliseconds < 60_000:
            return f"{milliseconds / 1_000:.1f} s"
        return f"{milliseconds / 60_000:.1f} min"

    @staticmethod
    def _run_summary(kind: str, status: str) -> str:
        if kind == "research" and status in {"failed", "cancelled"}:
            return "可在对话页使用“继续上次研究”恢复已保存的证据。"
        if kind == "daily" and status in {"failed", "cancelled", "partial_failed"}:
            return "可输入 `/daily resume` 从已保存的候选继续。"
        if kind == "chat" and status == "failed":
            return "可使用 `/retry` 重新发送本轮请求。"
        return ""

    def _session_run(self, row: dict[str, Any], kind: str) -> dict[str, Any]:
        trace = row.get("trace") if isinstance(row.get("trace"), dict) else {}
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        return {
            "run_id": str(row.get("run_id") or ""),
            "kind": kind,
            "status": str(row.get("status") or "running"),
            "created_at": self._safe_time(row.get("created_at")),
            "updated_at": self._safe_time(row.get("updated_at")),
            "duration_ms": row.get("duration_ms", trace.get("duration_ms")),
            "metrics": metrics,
            "events": self.sessions.get_run_events(
                str(row.get("run_id") or ""), str(row.get("thread_id") or ""),
            ),
        }

    def session_runs(self, thread_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """Return only runs owned by one session, newest first."""
        if not thread_id or not self.sessions.get(thread_id):
            return []
        rows = [
            *(self._session_run(row, "chat") for row in self.sessions.list_chat_runs(thread_id, limit)),
            *(self._session_run(row, "research") for row in self.sessions.list_research_runs(thread_id, limit)),
        ]
        rows.sort(key=lambda item: (item["updated_at"], item["created_at"]), reverse=True)
        return rows[:limit]

    def latest_chat_run(self, thread_id: str) -> dict[str, Any] | None:
        runs = self.sessions.list_chat_runs(thread_id, limit=1) if thread_id else []
        return self._session_run(runs[0], "chat") if runs else None

    def _daily_run(self, row: dict[str, Any]) -> dict[str, Any]:
        run_id = str(row.get("run_id") or "")
        events = []
        for event in self.scheduler.get_daily_agent_events(run_id):
            stage = str(event.get("agent") or "orchestrator")
            events.append({
                "agent": stage,
                "stage": stage,
                "status": str(event.get("status") or "running"),
                "summary": _DAILY_STAGE_LABELS.get(stage, "每日检索状态已更新"),
                "metrics": {},
                "error_type": "",
                "created_at": self._safe_time(event.get("created_at")),
            })
        return {
            "run_id": run_id,
            "kind": "daily",
            "status": str(row.get("status") or "running"),
            "created_at": self._safe_time(row.get("created_at")),
            "updated_at": self._safe_time(row.get("updated_at")),
            "duration_ms": None,
            "metrics": {},
            "events": events,
        }

    def daily_runs(self, limit: int = 12) -> list[dict[str, Any]]:
        return [self._daily_run(row) for row in self.scheduler.list_daily_runs(limit)]

    def render_latest_chat(self, thread_id: str) -> str:
        run = self.latest_chat_run(thread_id)
        if not run:
            return (
                '<div class="agent-run-empty">本轮尚未开始。普通对话将展示：'
                '排队 → 模型 → 可选工具 → 可选核验 → 完成。</div>'
            )
        return self.render_runs([run], compact=True)

    def render_session_runs(self, thread_id: str) -> str:
        return self.render_runs(self.session_runs(thread_id))

    def render_daily_runs(self) -> str:
        return self.render_runs(self.daily_runs())

    def render_runs(self, runs: list[dict[str, Any]], *, compact: bool = False) -> str:
        if not runs:
            return '<div class="agent-run-empty">暂无可展示的运行记录。</div>'
        max_events = 6 if compact else 10
        cards = [self._render_run_card(run, max_events=max_events) for run in runs]
        return '<div class="agent-run-list">' + "".join(cards) + "</div>"

    def _render_run_card(self, run: dict[str, Any], *, max_events: int) -> str:
        kind = str(run.get("kind") or "chat")
        status = str(run.get("status") or "running")
        duration = self._safe_duration(run.get("duration_ms"))
        metadata = " · ".join(part for part in (
            self._safe_time(run.get("updated_at") or run.get("created_at")), duration,
        ) if part)
        header = (
            '<div class="agent-run-title">'
            f'<strong>{escape(_KIND_LABELS.get(kind, "Agent 运行"))}</strong>'
            f'<span class="agent-run-badge status-{escape(status)}">'
            f'{escape(_STATUS_LABELS.get(status, status))}</span>'
            '</div>'
            f'<div class="agent-run-meta">{escape(metadata)} · 运行 ID：'
            f'<code>{escape(str(run.get("run_id") or ""))}</code></div>'
        )
        events = list(run.get("events") or [])[-max_events:]
        event_html = "".join(self._render_event(event) for event in events)
        if not event_html:
            event_html = '<li class="agent-run-event">运行已创建，等待阶段事件。</li>'
        hint = self._run_summary(kind, status)
        hint_html = f'<div class="agent-run-hint">{escape(hint)}</div>' if hint else ""
        return (
            '<section class="agent-run-card">'
            + header
            + '<ol class="agent-run-events">' + event_html + '</ol>'
            + hint_html
            + '</section>'
        )

    def _render_event(self, event: dict[str, Any]) -> str:
        status = str(event.get("status") or "running")
        stage = str(event.get("stage") or "run")
        summary = str(event.get("summary") or "运行状态已更新")
        duration = self._safe_duration((event.get("metrics") or {}).get("duration_ms"))
        meta = f" · {duration}" if duration else ""
        return (
            '<li class="agent-run-event">'
            f'<span class="agent-event-dot status-{escape(status)}"></span>'
            f'<span>{escape(summary)}</span>'
            f'<small>{escape(stage)}{escape(meta)}</small>'
            '</li>'
        )

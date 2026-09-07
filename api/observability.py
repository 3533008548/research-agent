"""Small dependency-free Prometheus exposition for the durable run boundary.

The metrics intentionally contain only fixed labels and aggregate numbers.  A
scrape must never reveal a session id, prompt, model answer, paper title, tool
argument, or API credential.
"""

from __future__ import annotations

import time
from typing import Any

from api.redis_runs import QueueUnavailableError


RUN_KINDS = ("chat", "research", "daily")
RUN_STATUSES = ("queued", "running", "cancelling", "completed", "cancelled", "failed", "partial_failed")


def _line(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    rendered = ",".join(f'{key}="{label}"' for key, label in labels.items())
    return f"{name}{{{rendered}}} {value}"


def _run_status_counts(app) -> dict[str, dict[str, int]]:
    """Read only bounded status aggregates from the existing SQLite stores."""
    counts = {kind: {} for kind in RUN_KINDS}
    sessions = getattr(getattr(app.state, "agent", None), "sessions", None)
    if sessions and hasattr(sessions, "run_status_counts"):
        counts.update(sessions.run_status_counts())
    daily_manager = getattr(app.state, "daily_run_manager", None)
    scheduler = getattr(daily_manager, "scheduler", None)
    if scheduler and hasattr(scheduler, "daily_run_status_counts"):
        counts["daily"] = scheduler.daily_run_status_counts()
    return counts


def render_prometheus_metrics(app: Any) -> str:
    """Render an authenticated Prometheus text response without extra packages."""
    lines = [
        "# HELP research_agent_api_up Whether this FastAPI process can serve requests.",
        "# TYPE research_agent_api_up gauge",
        _line("research_agent_api_up", 1),
        "# HELP research_agent_api_uptime_seconds FastAPI process uptime in seconds.",
        "# TYPE research_agent_api_uptime_seconds gauge",
        _line("research_agent_api_uptime_seconds", round(max(0.0, time.monotonic() - app.state.api_started_at), 3)),
        "# HELP research_agent_run_records Persisted run records by kind and current status.",
        "# TYPE research_agent_run_records gauge",
    ]
    for kind, counts in _run_status_counts(app).items():
        for status in RUN_STATUSES:
            lines.append(_line(
                "research_agent_run_records", int(counts.get(status, 0)),
                {"kind": kind, "status": status},
            ))

    lines.extend([
        "# HELP research_agent_queue_configured Whether a durable Redis queue is configured for a run kind.",
        "# TYPE research_agent_queue_configured gauge",
        "# HELP research_agent_queue_messages Current Redis stream entries awaiting acknowledgement.",
        "# TYPE research_agent_queue_messages gauge",
        "# HELP research_agent_worker_up Live durable workers with a recent Redis heartbeat.",
        "# TYPE research_agent_worker_up gauge",
    ])

    redis_up = 1
    manager_names = {
        "chat": "chat_run_manager",
        "research": "research_run_manager",
        "daily": "daily_run_manager",
    }
    for kind, manager_name in manager_names.items():
        manager = getattr(app.state, manager_name, None)
        broker = getattr(manager, "broker", None)
        configured = int(broker is not None)
        queue_messages = 0
        workers = 0
        if broker is not None:
            try:
                queue_messages = max(0, int(broker.queue_depth()))
                workers = max(0, int(broker.live_worker_count()))
            except (AttributeError, QueueUnavailableError):
                redis_up = 0
        lines.append(_line("research_agent_queue_configured", configured, {"kind": kind}))
        lines.append(_line("research_agent_queue_messages", queue_messages, {"kind": kind}))
        lines.append(_line("research_agent_worker_up", workers, {"kind": kind}))

    lines.extend([
        "# HELP research_agent_redis_up Whether every configured durable queue responded to its scrape query.",
        "# TYPE research_agent_redis_up gauge",
        _line("research_agent_redis_up", redis_up),
    ])
    return "\n".join(lines) + "\n"

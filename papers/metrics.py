"""Retrieval latency instrumentation.

Prometheus had seven metrics before this module existed and *all* of them were
queue/process level: api_up, uptime, run_records, queue_configured,
queue_messages, redis_up, worker_up.  Nothing about retrieval.  So performance
was observable offline (``scripts/profile_retrieval.py``) but not monitored
online -- nobody could answer "which stage got slow" from a running system.

Process boundary (this is the subtle part)
------------------------------------------
Retrieval runs in the **worker** process, but ``/metrics`` is served by the
**web** process.  Accumulating in-process and rendering from the web process
would therefore report a permanent zero.  The worker's totals are pushed to
Redis as a single snapshot key with a TTL -- the same pattern the existing
worker heartbeat already uses (``api/redis_runs.py``), so the transport is
proven rather than new.

Privacy
-------
Only aggregate numbers and stage names are recorded.  Never a query string,
paper title, chunk id or paper id.  ``api/observability.py`` documents the same
rule for its metrics, and both must keep to it: a scrape is a legitimate
operational read, not a data leak.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

# The four stages of a hybrid retrieval.  The names are part of the metric's
# label contract, so renaming one changes the dashboard -- keep them stable.
STAGES = ("semantic", "lexical", "fusion", "rerank")

# Milliseconds is the useful unit for this system: a hybrid query is ~600 ms
# end-to-end, and reranking alone is ~90% of it.  Seconds would render as
# "0.000" for the fast stages.
_UNIT = "duration_ms"

_lock = threading.Lock()
_totals: dict[str, float] = {stage: 0.0 for stage in STAGES}
_counts: dict[str, int] = {stage: 0 for stage in STAGES}
_errors: dict[str, int] = {stage: 0 for stage in STAGES}
_queries = 0
_started_at = time.monotonic()


def reset() -> None:
    """Clear all accumulators.  Test-only: production never resets."""
    global _queries, _started_at
    with _lock:
        for stage in STAGES:
            _totals[stage] = 0.0
            _counts[stage] = 0
            _errors[stage] = 0
        _queries = 0
        _started_at = time.monotonic()


class StageTimer:
    """Context manager that times one stage and records the result.

    Records on both the success and the failure path -- a stage that raised is
    a slow stage, and dropping it would bias the average downward exactly when
    latency matters most.

    Usage::

        with StageTimer("rerank"):
            reranked = self._rerank(query_text, ranked[:limit])
    """

    __slots__ = ("_stage", "_started")

    def __init__(self, stage: str) -> None:
        if stage not in STAGES:
            # A typo'd stage name would otherwise create an unbounded metric
            # label, which is how dashboards get flooded with garbage series.
            raise ValueError(f"unknown retrieval stage: {stage!r}")
        self._stage = stage
        self._started = 0.0

    def __enter__(self) -> "StageTimer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _record(self._stage, (time.perf_counter() - self._started) * 1000.0,
                failed=exc_type is not None)
        return False


def _record(stage: str, duration_ms: float, *, failed: bool = False) -> None:
    """Add one observation.  Never blocks a query for long: the lock is held
    only for a couple of dict updates, and no caller runs inside it."""
    global _queries
    with _lock:
        _totals[stage] += duration_ms
        _counts[stage] += 1
        if failed:
            _errors[stage] += 1
        if stage == "semantic":
            # One hybrid query runs semantic first, exactly once; counting it
            # keeps the query total aligned with the semantic stage count.
            _queries += 1


def snapshot() -> dict[str, Any]:
    """Current aggregates, safe to serialise and ship across a process boundary."""
    with _lock:
        stages = {
            stage: {
                "total_ms": round(_totals[stage], 3),
                "count": _counts[stage],
                "errors": _errors[stage],
            }
            for stage in STAGES
        }
        return {
            "unit": _UNIT,
            "queries": _queries,
            "uptime_seconds": round(time.monotonic() - _started_at, 1),
            "stages": stages,
        }


def mean_ms(stage: str) -> float:
    """Average duration, or 0.0 when nothing has been recorded yet."""
    with _lock:
        count = _counts[stage]
        return round(_totals[stage] / count, 3) if count else 0.0


# ── Redis transport ───────────────────────────────────────────────────────────
#
# Key shape mirrors the heartbeat keys in api/redis_runs.py: a TTL'd scalar so
# a dead worker stops being reported instead of freezing stale numbers in the
# dashboard forever.

_METRICS_KEY = "research_agent:retrieval_metrics"
_METRICS_TTL_SECONDS = 60


def publish(redis_client: Any, *, ttl_seconds: int = _METRICS_TTL_SECONDS) -> bool:
    """Push this process's aggregates to Redis.  Returns True on success.

    Every failure is swallowed on purpose: instrumentation must never break the
    query that produced the numbers.  ``stderr`` gets nothing either -- a
    worker with a broken Redis already reports that through its own health
    checks, and a noisy metrics failure would drown out the real signal.
    """
    if redis_client is None:
        return False
    try:
        payload = json.dumps(snapshot(), separators=(",", ":"))
        redis_client.set(_METRICS_KEY, payload, ex=max(5, int(ttl_seconds)))
        return True
    except Exception:  # noqa: BLE001 - see docstring
        return False


def load_published(redis_client: Any) -> dict[str, Any] | None:
    """Read the published aggregates, or None when absent/unavailable."""
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(_METRICS_KEY)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def render_prometheus(published: dict[str, Any] | None) -> list[str]:
    """Render Prometheus exposition lines for retrieval stages.

    Emitted as a gauge with mean/total/count rather than a true histogram: the
    per-bucket latencies would need bucket edges, and a mean plus a count is
    enough to answer "did the reranker get slow", which is what this exists for.

    Returns an empty list when nothing has been recorded -- emitting HELP/TYPE
    headers with no samples makes Prometheus believe the series exists.
    """
    if not published:
        return []

    stages = published.get("stages") or {}
    series: list[tuple[str, str, float]] = []
    for stage in STAGES:
        data = stages.get(stage)
        if not isinstance(data, dict):
            continue
        total = float(data.get("total_ms", 0.0))
        count = int(data.get("count", 0))
        if not count:
            continue
        series.append((stage, "mean", round(total / count, 3)))
        series.append((stage, "total", round(total, 3)))
        series.append((stage, "count", float(count)))
        errors = int(data.get("errors", 0))
        if errors:
            series.append((stage, "errors", float(errors)))

    if not series:
        return []

    name = "research_agent_retrieval_stage_ms"
    lines = [
        f"# HELP {name} Retrieval stage latency in milliseconds.",
        f"# TYPE {name} gauge",
    ]
    lines.extend(
        f'{name}{{stage="{stage}",stat="{stat}"}} {value}' for stage, stat, value in series
    )

    queries = int(published.get("queries", 0))
    if queries:
        lines.append("research_agent_retrieval_queries_total " + str(queries))
        uptime = float(published.get("uptime_seconds", 0.0))
        if uptime > 0:
            lines.append("research_agent_retrieval_uptime_seconds " + str(round(uptime, 1)))

    return lines

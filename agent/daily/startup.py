"""Restore daily scheduling when a worker starts."""


def enqueue_enabled_daily_run(cfg, manager) -> str | None:
    """Restore the former UI-startup daily task from the durable worker.

    A resumable daily run always wins so a restart does not discard already
    collected candidates.  Otherwise a normal daily run lets ``Scheduler``
    preserve its existing once-per-day keyword semantics.
    """
    if not bool(getattr(cfg, "daily_search_enabled", False)):
        return None
    kind = "resume" if manager.scheduler.get_latest_resumable_run("daily") else "daily"
    run = manager.start(kind)
    return str(run.get("run_id") or "") or None

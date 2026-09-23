"""Process entrypoint for all durable FastAPI runs.

Every API instance only enqueues work. This single worker process owns the
model client and SQLite writes, while Redis carries shared queue/cancellation/
SSE state. Separate consumers let interactive chat run even when a background
task is already waiting on a source or model request.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import TimeoutError as FutureTimeout

from api.redis_runs import (
    RedisChatRunBroker,
    RedisChatRunWorker,
    RedisDailyRunBroker,
    RedisDailyRunManager,
    RedisDailyRunWorker,
    RedisExperimentRunBroker,
    RedisExperimentRunWorker,
    RedisResearchRunBroker,
    RedisResearchRunWorker,
)
from config import Config
from daily_orchestrator import DailyResearchOrchestrator
from experiment_projects import ExperimentProjectStore
from repository_preparation import RepositoryPreparationOrchestrator
from experiment_reproduction import ExperimentOrchestrator
from research_agent import ResearchAgent
from scheduler import Scheduler


def warm_rag_before_consuming(agent, *, timeout_seconds: float = 90.0) -> None:
    """Keep the first user run from paying the local encoder cold-start cost."""
    future = agent.start_rag_warmup()
    if future is None:
        return
    print("      🔥 正在预热本地嵌入模型…", flush=True)
    try:
        future.result(timeout=max(1.0, float(timeout_seconds)))
    except FutureTimeout:
        print("      ⚠️ 嵌入模型预热超过 90 秒，Worker 将继续启动并保留关键词回退。", flush=True)
    except Exception as exc:
        print(f"      ⚠️ 嵌入模型预热失败（{type(exc).__name__}），Worker 将继续启动并保留关键词回退。", flush=True)
    else:
        print("      ✅ 本地嵌入模型预热完成", flush=True)


def enqueue_enabled_daily_run(cfg, manager: RedisDailyRunManager) -> str | None:
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


def main() -> None:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for api_worker")
    cfg = Config.load()
    agent = ResearchAgent(cfg=cfg)
    warm_rag_before_consuming(agent)
    chat_broker = RedisChatRunBroker(redis_url)
    research_broker = RedisResearchRunBroker(redis_url)
    daily_broker = RedisDailyRunBroker(redis_url)
    experiment_broker = RedisExperimentRunBroker(redis_url)
    for broker in (chat_broker, research_broker, daily_broker, experiment_broker):
        broker.ping()
    chat_worker = RedisChatRunWorker(
        agent, chat_broker,
        claim_idle_ms=int(os.getenv("CHAT_RUN_CLAIM_IDLE_MS", "120000")),
    )
    research_worker = RedisResearchRunWorker(
        agent, research_broker,
        claim_idle_ms=int(os.getenv("RESEARCH_RUN_CLAIM_IDLE_MS", "120000")),
    )
    scheduler = Scheduler(cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds)
    daily_orchestrator = DailyResearchOrchestrator(
        scheduler=scheduler,
        llm_client=agent.llm_client,
        model=agent.model,
        request_timeout_seconds=cfg.daily_request_timeout_seconds,
        max_keyword_concurrency=cfg.daily_keyword_concurrency,
        max_results_per_keyword=cfg.daily_max_results_per_keyword,
        daily_sources=cfg.daily_sources,
        openalex_api_key=cfg.openalex_api_key,
        ieee_api_key=cfg.ieee_api_key,
    )
    daily_worker = RedisDailyRunWorker(
        daily_orchestrator, scheduler, daily_broker,
        claim_idle_ms=int(os.getenv("DAILY_RUN_CLAIM_IDLE_MS", "120000")),
        paper_store=agent.paper_store,
    )
    daily_manager = RedisDailyRunManager(scheduler, daily_broker)
    startup_daily_run_id = enqueue_enabled_daily_run(cfg, daily_manager)
    if startup_daily_run_id:
        print(f"      📰 已提交每日自动检索: {startup_daily_run_id}")
    experiment_worker = RedisExperimentRunWorker(
        ExperimentOrchestrator(
            paper_store=agent.paper_store,
            project_store=ExperimentProjectStore(cfg.runtime_paths),
            llm_client=agent.llm_client,
            model=agent.model,
            paths=cfg.runtime_paths,
        ),
        agent.sessions,
        experiment_broker,
        repository_preparer=RepositoryPreparationOrchestrator(
            project_store=ExperimentProjectStore(cfg.runtime_paths),
        ),
        claim_idle_ms=int(os.getenv("EXPERIMENT_RUN_CLAIM_IDLE_MS", "120000")),
    )

    # Independent consumers keep a long source fetch or background synthesis
    # from blocking chat dispatch. All three share ``agent.llm_client``; its
    # existing interactive-reserved permits therefore arbitrate model capacity
    # in one place. API replicas remain safe because they only enqueue.
    workers = (chat_worker, research_worker, daily_worker, experiment_worker)
    threads = [
        threading.Thread(
            target=worker.run_forever,
            name=f"api-{name}-consumer",
            daemon=False,
        )
        for name, worker in zip(("chat", "research", "daily", "experiment"), workers, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()

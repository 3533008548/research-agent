"""Process entrypoint for all durable FastAPI runs.

Every API instance only enqueues work. This single worker process owns the
model client and SQLite writes, while Redis carries shared queue/cancellation/
SSE state. Separate consumers let interactive chat run even when a background
task is already waiting on a source or model request.
"""

from __future__ import annotations

import os
import threading

from api.redis_runs import (
    RedisChatRunBroker,
    RedisChatRunWorker,
    RedisDailyRunBroker,
    RedisDailyRunWorker,
    RedisResearchRunBroker,
    RedisResearchRunWorker,
)
from config import Config
from daily_orchestrator import DailyResearchOrchestrator
from research_agent import ResearchAgent
from scheduler import Scheduler


def main() -> None:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for api_worker")
    cfg = Config.load()
    agent = ResearchAgent(cfg=cfg)
    chat_broker = RedisChatRunBroker(redis_url)
    research_broker = RedisResearchRunBroker(redis_url)
    daily_broker = RedisDailyRunBroker(redis_url)
    for broker in (chat_broker, research_broker, daily_broker):
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

    # Independent consumers keep a long source fetch or background synthesis
    # from blocking chat dispatch. All three share ``agent.llm_client``; its
    # existing interactive-reserved permits therefore arbitrate model capacity
    # in one place. API replicas remain safe because they only enqueue.
    workers = (chat_worker, research_worker, daily_worker)
    threads = [
        threading.Thread(
            target=worker.run_forever,
            name=f"api-{name}-consumer",
            daemon=False,
        )
        for name, worker in zip(("chat", "research", "daily"), workers, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()

"""Process entrypoint for Redis-backed FastAPI chat runs.

Only this process consumes the durable chat queue.  Keep one worker while the
project's user data lives in SQLite; Redis makes cancellation and SSE delivery
cross-process without pretending that SQLite is a multi-writer job database.
"""

from __future__ import annotations

import os

from api.redis_runs import RedisChatRunBroker, RedisChatRunWorker
from config import Config
from research_agent import ResearchAgent


def main() -> None:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for api_worker")
    cfg = Config.load()
    agent = ResearchAgent(cfg=cfg)
    broker = RedisChatRunBroker(redis_url)
    broker.ping()
    worker = RedisChatRunWorker(
        agent,
        broker,
        claim_idle_ms=int(os.getenv("CHAT_RUN_CLAIM_IDLE_MS", "120000")),
    )
    worker.run_forever()


if __name__ == "__main__":
    main()

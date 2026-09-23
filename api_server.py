"""ASGI entrypoint: FastAPI service and the bundled React workbench."""

from __future__ import annotations

import os
from pathlib import Path

from api.app import create_app, mount_react_frontend
from api.redis_runs import (
    RedisChatRunBroker,
    RedisChatRunManager,
    RedisDailyRunBroker,
    RedisDailyRunManager,
    RedisExperimentRunBroker,
    RedisExperimentRunManager,
    RedisResearchRunBroker,
    RedisResearchRunManager,
)
from config import Config
from experiment_projects import ExperimentProjectStore
from research_agent import ResearchAgent
from scheduler import Scheduler


def create_server():
    cfg = Config.load()
    agent = ResearchAgent(cfg=cfg)
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        chat_broker = RedisChatRunBroker(redis_url)
        research_broker = RedisResearchRunBroker(redis_url)
        daily_broker = RedisDailyRunBroker(redis_url)
        experiment_broker = RedisExperimentRunBroker(redis_url)
        for broker in (chat_broker, research_broker, daily_broker, experiment_broker):
            broker.ping()
        manager = RedisChatRunManager(agent, chat_broker)
        research_manager = RedisResearchRunManager(agent, research_broker)
        daily_manager = RedisDailyRunManager(
            Scheduler(cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds), daily_broker,
        )
        experiment_manager = RedisExperimentRunManager(
            agent, ExperimentProjectStore(cfg.runtime_paths), experiment_broker,
        )
    else:
        manager = None
        research_manager = None
        daily_manager = None
        experiment_manager = None
    app = create_app(
        agent=agent,
        chat_run_manager=manager,
        research_run_manager=research_manager,
        daily_run_manager=daily_manager,
        experiment_run_manager=experiment_manager,
        api_key=os.getenv("API_AUTH_TOKEN"),
        require_api_key=os.getenv("API_AUTH_REQUIRED", "").strip().lower() in {"1", "true", "yes"},
    )
    mount_react_frontend(app, Path(__file__).resolve().parent / "frontend" / "dist")
    return app


app = create_server()

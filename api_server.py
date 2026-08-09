"""Single-process ASGI entrypoint: FastAPI API plus the existing Gradio UI."""

from __future__ import annotations

import os

import gradio as gr

from api.app import create_app
from api.redis_runs import (
    RedisChatRunBroker,
    RedisChatRunManager,
    RedisDailyRunBroker,
    RedisDailyRunManager,
    RedisResearchRunBroker,
    RedisResearchRunManager,
)
from config import Config
from research_agent import ResearchAgent
from scheduler import Scheduler
from web_ui import UI_CSS, UI_JS, UI_THEME, build_ui


def create_server():
    cfg = Config.load()
    agent = ResearchAgent(cfg=cfg)
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        chat_broker = RedisChatRunBroker(redis_url)
        research_broker = RedisResearchRunBroker(redis_url)
        daily_broker = RedisDailyRunBroker(redis_url)
        for broker in (chat_broker, research_broker, daily_broker):
            broker.ping()
        manager = RedisChatRunManager(agent, chat_broker)
        research_manager = RedisResearchRunManager(agent, research_broker)
        daily_manager = RedisDailyRunManager(
            Scheduler(cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds), daily_broker,
        )
    else:
        manager = None
        research_manager = None
        daily_manager = None
    app = create_app(
        agent=agent,
        chat_run_manager=manager,
        research_run_manager=research_manager,
        daily_run_manager=daily_manager,
        api_key=os.getenv("API_AUTH_TOKEN"),
        require_api_key=os.getenv("API_AUTH_REQUIRED", "").strip().lower() in {"1", "true", "yes"},
    )
    ui = build_ui(cfg=cfg, agent=agent, launch=False)
    return gr.mount_gradio_app(
        app,
        ui,
        path="/",
        theme=UI_THEME,
        css=UI_CSS,
        js=UI_JS,
    )


app = create_server()

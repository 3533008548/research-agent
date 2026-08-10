"""Application factory for the versioned FastAPI service boundary."""

from __future__ import annotations

import time

from fastapi import FastAPI

from api.auth import APIKeyAuthenticator
from api.routes import router
from api.run_manager import ChatRunManager


def create_app(
    *,
    agent,
    title: str = "Research Agent API",
    chat_run_manager=None,
    research_run_manager=None,
    daily_run_manager=None,
    api_key: str | None = None,
    require_api_key: bool = False,
) -> FastAPI:
    """Create an ASGI app around an already configured shared agent instance."""
    app = FastAPI(title=title, version="v1")
    app.state.agent = agent
    app.state.api_started_at = time.monotonic()
    app.state.chat_run_manager = chat_run_manager or ChatRunManager(agent)
    # ``None`` means that this deployment has not configured the durable Redis
    # worker boundary; the canonical route then returns a clear 503 for the
    # unavailable run kind instead of silently starting local background work.
    app.state.research_run_manager = research_run_manager
    app.state.daily_run_manager = daily_run_manager
    app.state.api_authenticator = APIKeyAuthenticator(api_key, required=require_api_key)
    app.include_router(router)
    return app

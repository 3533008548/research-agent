"""Application factory for the versioned FastAPI service boundary."""

from __future__ import annotations

from fastapi import FastAPI

from api.auth import APIKeyAuthenticator
from api.routes import router
from api.run_manager import ChatRunManager


def create_app(
    *,
    agent,
    title: str = "Research Agent API",
    chat_run_manager=None,
    api_key: str | None = None,
    require_api_key: bool = False,
) -> FastAPI:
    """Create an ASGI app around an already configured shared agent instance."""
    app = FastAPI(title=title, version="v1")
    app.state.agent = agent
    app.state.chat_run_manager = chat_run_manager or ChatRunManager(agent)
    app.state.api_authenticator = APIKeyAuthenticator(api_key, required=require_api_key)
    app.include_router(router)
    return app

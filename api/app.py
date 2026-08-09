"""Application factory for the versioned FastAPI service boundary."""

from __future__ import annotations

from fastapi import FastAPI

from api.routes import router
from api.run_manager import ChatRunManager


def create_app(*, agent, title: str = "Research Agent API") -> FastAPI:
    """Create an ASGI app around an already configured shared agent instance."""
    app = FastAPI(title=title, version="v1")
    app.state.agent = agent
    app.state.chat_run_manager = ChatRunManager(agent)
    app.include_router(router)
    return app

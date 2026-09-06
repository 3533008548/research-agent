"""Application factory for the versioned FastAPI service boundary."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.auth import APIKeyAuthenticator
from api.routes import router
from api.run_manager import ChatRunManager
from badcase_store import BadcaseStore
from research_documents import ResearchDocumentStore
from scheduler import Scheduler


def _default_badcase_store(agent) -> BadcaseStore:
    """Keep feedback beside runtime data without requiring Config on test doubles."""
    paths = getattr(getattr(agent, "cfg", None), "runtime_paths", None)
    if paths is not None:
        return BadcaseStore(str(paths.badcases_db))
    session_db = getattr(getattr(agent, "sessions", None), "db_path", "")
    if session_db:
        return BadcaseStore(str(Path(session_db).parent / "badcases.db"))
    from runtime_paths import get_runtime_paths
    return BadcaseStore(str(get_runtime_paths().badcases_db))


def create_app(
    *,
    agent,
    title: str = "Research Agent API",
    chat_run_manager=None,
    research_run_manager=None,
    daily_run_manager=None,
    badcase_store=None,
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
    app.state.badcase_store = badcase_store or _default_badcase_store(agent)
    app.state.api_authenticator = APIKeyAuthenticator(api_key, required=require_api_key)
    # The React workbench needs the same local, user-owned data that the
    # legacy Gradio panels use.  Test doubles and API-only embeddings may not
    # expose Config, so leave these optional instead of manufacturing a second
    # runtime directory for them.
    cfg = getattr(agent, "cfg", None)
    paths = getattr(cfg, "runtime_paths", None)
    app.state.workspace_paths = paths
    app.state.research_document_store = ResearchDocumentStore(paths) if paths else None
    app.state.workspace_scheduler = (
        getattr(daily_run_manager, "scheduler", None)
        if daily_run_manager is not None
        else (Scheduler(cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds) if cfg else None)
    )
    app.include_router(router)
    return app


def mount_react_frontend(app: FastAPI, dist_dir: str | Path, *, mount_path: str = "/app") -> bool:
    """Serve a built React client beside the legacy Gradio route when present.

    Source-only checkouts intentionally keep working: the Python API can start
    before Node has produced ``frontend/dist``.  The container build creates
    the directory, while local React development normally uses Vite's proxy.
    """
    dist = Path(dist_dir)
    index = dist / "index.html"
    if not index.is_file():
        return False

    normalized_path = "/" + mount_path.strip("/")
    assets = dist / "assets"
    if assets.is_dir():
        app.mount(
            f"{normalized_path}/assets",
            StaticFiles(directory=assets),
            name="react-assets",
        )

    @app.get(normalized_path, include_in_schema=False)
    @app.get(f"{normalized_path}/{{client_path:path}}", include_in_schema=False)
    def react_client(client_path: str = "") -> FileResponse:
        # A client-side route should receive the SPA shell.  Static assets are
        # matched by the earlier mount and therefore never reach this handler.
        return FileResponse(index)

    return True

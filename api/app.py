"""Application factory for the versioned FastAPI service boundary."""

from __future__ import annotations

import time
import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.auth import APIKeyAuthenticator
from api.routes import router
from api.run_manager import ChatRunManager
from badcase_store import BadcaseStore
from paper_relations import PaperRelationStore
from scheduler import Scheduler
from workspace_uploads import WorkspaceUploadStore
from library_store import LibraryStore
from api.library_routes import router as library_router


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
    # The React workbench needs the same local, user-owned data as the agent.
    # Test doubles and API-only embeddings may not expose Config, so leave
    # these optional instead of manufacturing a second runtime directory.
    cfg = getattr(agent, "cfg", None)
    paths = getattr(cfg, "runtime_paths", None)
    app.state.workspace_paths = paths
    app.state.library_store = LibraryStore(paths) if paths else None
    app.state.workspace_uploads = WorkspaceUploadStore(paths) if paths else None
    app.state.paper_relation_store = PaperRelationStore(paths) if paths else None
    app.state.workspace_scheduler = (
        getattr(daily_run_manager, "scheduler", None)
        if daily_run_manager is not None
        else (Scheduler(cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds) if cfg else None)
    )
    app.include_router(router)
    app.include_router(library_router)
    return app


def mount_react_frontend(app: FastAPI, dist_dir: str | Path, *, mount_path: str = "") -> bool:
    """Serve the built React client, including client-side routes.

    Source-only checkouts intentionally keep working: the Python API can start
    before Node has produced ``frontend/dist``.  The container build creates
    the directory, while local React development normally uses Vite's proxy.
    """
    dist = Path(dist_dir)
    # Windows registry MIME mappings can label ES-module workers text/plain.
    mimetypes.add_type('text/javascript', '.mjs')
    mimetypes.add_type('application/wasm', '.wasm')
    index = dist / "index.html"
    if not index.is_file():
        return False

    suffix = mount_path.strip("/")
    normalized_path = f"/{suffix}" if suffix else ""
    assets = dist / "assets"
    if (dist / "pdfjs").is_dir():
        app.mount(f"{normalized_path}/pdfjs", StaticFiles(directory=dist / "pdfjs"), name="pdf-assets")
    if assets.is_dir():
        app.mount(
            f"{normalized_path}/assets" or "/assets",
            StaticFiles(directory=assets),
            name="react-assets",
        )

    @app.api_route(
        "/api/{api_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def missing_api_route(api_path: str) -> None:
        # Keep removed/unknown API endpoints out of the SPA fallback.
        raise HTTPException(status_code=404, detail="API route not found")

    @app.get(normalized_path or "/", include_in_schema=False)
    @app.get(f"{normalized_path}/{{client_path:path}}" if normalized_path else "/{client_path:path}", include_in_schema=False)
    def react_client(client_path: str = "") -> FileResponse:
        if client_path == "api" or client_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        # A client-side route should receive the SPA shell.  Static assets are
        # matched by the earlier mount and therefore never reach this handler.
        return FileResponse(index)

    return True

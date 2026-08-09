"""Versioned HTTP routes; all agent work stays behind the service boundary."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from api.schemas import (
    CancelRunResponse,
    ChatRunCreateRequest,
    ChatRunResponse,
    ChatRunStartResponse,
    SessionCreateRequest,
    SessionResponse,
)


router = APIRouter(prefix="/api/v1")


def _session_payload(session: dict) -> dict:
    return {
        "thread_id": session["thread_id"],
        "title": session["title"],
        "preview": session.get("preview", ""),
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
    }


def _run_payload(run: dict, events: list[dict] | None = None) -> dict:
    return {
        "run_id": run["run_id"],
        "session_id": run["thread_id"],
        "status": run["status"],
        "model": run.get("model", ""),
        "answer": run.get("answer", ""),
        "duration_ms": run.get("duration_ms"),
        "metrics": run.get("metrics", {}),
        "error_type": run.get("error_type", ""),
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "completed_at": run.get("completed_at"),
        "events": events or [],
    }


def _manager(request: Request):
    return request.app.state.chat_run_manager


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "model": str(request.app.state.agent.model)}


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(request: Request) -> list[dict]:
    return [_session_payload(item) for item in request.app.state.agent.list_sessions()]


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionCreateRequest, request: Request) -> dict:
    return _session_payload(request.app.state.agent.create_session(payload.title))


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, request: Request) -> Response:
    _manager(request).cancel_for_session(session_id)
    if not request.app.state.agent.delete_session(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/runs", response_model=list[ChatRunResponse])
def list_session_runs(session_id: str, request: Request, limit: int = 20) -> list[dict]:
    if not request.app.state.agent.sessions.get(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    manager = _manager(request)
    return [_run_payload(item) for item in manager.list_for_session(session_id, limit=limit)]


@router.post(
    "/sessions/{session_id}/chat-runs",
    response_model=ChatRunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_chat_run(session_id: str, payload: ChatRunCreateRequest, request: Request) -> dict:
    if not payload.message.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="message is blank")
    manager = _manager(request)
    try:
        run = manager.start(session_id, payload.message)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found") from None
    return {
        "run_id": run["run_id"],
        "session_id": session_id,
        "status": run["status"],
        "stream_url": f"/api/v1/runs/{run['run_id']}/events",
    }


@router.get("/runs/{run_id}", response_model=ChatRunResponse)
def get_run(run_id: str, request: Request) -> dict:
    manager = _manager(request)
    run = manager.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    events = request.app.state.agent.sessions.get_run_events(run_id, run["thread_id"])
    return _run_payload(run, events)


@router.get("/runs/{run_id}/events")
def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    manager = _manager(request)
    if not manager.get(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return StreamingResponse(
        manager.stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel", response_model=CancelRunResponse)
def cancel_run(run_id: str, request: Request) -> dict:
    run = _manager(request).cancel(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is not active in this API process",
        )
    return {"run_id": run_id, "status": run["status"]}

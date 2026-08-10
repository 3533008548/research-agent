"""Versioned HTTP routes; all agent work stays behind the service boundary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, StreamingResponse

from api.auth import require_api_key
from api.observability import render_prometheus_metrics
from api.redis_runs import QueueUnavailableError
from api.schemas import (
    CancelRunResponse,
    ChatCreateRequest,
    ChatRunCreateRequest,
    ChatRunResponse,
    ChatRunStartResponse,
    RunCreateRequest,
    RunResponse,
    RunStartResponse,
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


def _chat_payload(run: dict, events: list[dict] | None = None) -> dict:
    return {
        "run_id": run["run_id"],
        "session_id": run["thread_id"],
        "kind": "chat",
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


def _research_payload(run: dict, events: list[dict] | None = None) -> dict:
    trace = run.get("trace") or {}
    usage = trace.get("usage") if isinstance(trace, dict) else {}
    safe_metrics = {
        key: value for key, value in (usage or {}).items()
        if key in {"prompt", "completion", "total", "calls"}
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    duration = trace.get("duration_ms") if isinstance(trace, dict) else None
    return {
        "run_id": run["run_id"], "session_id": run["thread_id"], "kind": "research",
        "status": run["status"], "model": "", "answer": run.get("final_answer", ""),
        "duration_ms": duration if isinstance(duration, (int, float)) else None,
        "metrics": safe_metrics, "error_type": str(trace.get("error_type") or "")[:120]
        if isinstance(trace, dict) else "",
        "created_at": run["created_at"], "updated_at": run["updated_at"],
        "completed_at": None, "events": events or [],
    }


def _daily_payload(run: dict) -> dict:
    result = run.get("result") or {}
    selected = result.get("selected") if isinstance(result, dict) else []
    candidates = run.get("candidates") or []
    error_text = str(run.get("error_text") or "")
    return {
        "run_id": run["run_id"], "session_id": None, "kind": "daily",
        "status": run["status"], "model": "", "answer": "", "duration_ms": None,
        "metrics": {
            "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
            "selected_count": len(selected) if isinstance(selected, list) else 0,
        },
        "error_type": error_text.split(":", 1)[0][:120] if error_text else "",
        "created_at": run["created_at"], "updated_at": run["updated_at"],
        "completed_at": None, "events": [],
    }


def _manager(request: Request):
    return request.app.state.chat_run_manager


def _manager_for_run(request: Request, run_id: str):
    if run_id.startswith("chat-"):
        return request.app.state.chat_run_manager
    if run_id.startswith("research-"):
        return request.app.state.research_run_manager
    if run_id.startswith("daily-"):
        return request.app.state.daily_run_manager
    return None


def _run_payload(request: Request, run: dict, events: list[dict] | None = None) -> dict:
    run_id = str(run.get("run_id") or "")
    if run_id.startswith("research-"):
        return _research_payload(run, events)
    if run_id.startswith("daily-"):
        return _daily_payload(run)
    return _chat_payload(run, events)


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "model": str(request.app.state.agent.model)}


@router.get("/metrics", include_in_schema=False, dependencies=[Depends(require_api_key)])
def prometheus_metrics(request: Request) -> PlainTextResponse:
    """Expose aggregate operational metrics without any user or model payload."""
    return PlainTextResponse(
        render_prometheus_metrics(request.app),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/sessions", response_model=list[SessionResponse], dependencies=[Depends(require_api_key)])
def list_sessions(request: Request) -> list[dict]:
    return [_session_payload(item) for item in request.app.state.agent.list_sessions()]


@router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def create_session(payload: SessionCreateRequest, request: Request) -> dict:
    return _session_payload(request.app.state.agent.create_session(payload.title))


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
)
def delete_session(session_id: str, request: Request) -> Response:
    _manager(request).cancel_for_session(session_id)
    research_manager = request.app.state.research_run_manager
    if research_manager:
        research_manager.cancel_for_session(session_id)
    if not request.app.state.agent.delete_session(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{session_id}/runs",
    response_model=list[RunResponse],
    dependencies=[Depends(require_api_key)],
)
def list_session_runs(session_id: str, request: Request, limit: int = 20) -> list[dict]:
    if not request.app.state.agent.sessions.get(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    chat_runs = _manager(request).list_for_session(session_id, limit=limit)
    research_manager = request.app.state.research_run_manager
    research_runs = research_manager.list_for_session(session_id, limit=limit) if research_manager else []
    all_runs = [*chat_runs, *research_runs]
    all_runs.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("created_at") or "")), reverse=True)
    return [_run_payload(request, item) for item in all_runs[:max(1, min(limit, 100))]]


@router.post(
    "/sessions/{session_id}/chat-runs",
    response_model=ChatRunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def create_chat_run(session_id: str, payload: ChatRunCreateRequest, request: Request) -> dict:
    if not payload.message.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="message is blank")
    manager = _manager(request)
    try:
        run = manager.start(session_id, payload.message)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found") from None
    except QueueUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="chat queue unavailable") from None
    return {
        "run_id": run["run_id"],
        "session_id": session_id,
        "status": run["status"],
        "stream_url": f"/api/v1/runs/{run['run_id']}/events",
    }


@router.post(
    "/chat",
    response_model=RunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def create_chat(payload: ChatCreateRequest, request: Request) -> dict:
    """Small compatibility-friendly chat facade; ``/runs`` remains canonical."""
    return create_run(
        RunCreateRequest(kind="chat", session_id=payload.session_id, message=payload.message), request,
    )


@router.post(
    "/runs",
    response_model=RunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def create_run(payload: RunCreateRequest, request: Request) -> dict:
    """Create any externally runnable task through the one public run contract."""
    try:
        if payload.kind == "chat":
            if not payload.session_id or not (payload.message or "").strip():
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="chat requires session_id and message")
            run = _manager(request).start(payload.session_id, payload.message)
            session_id = payload.session_id
        elif payload.kind == "research":
            if not payload.session_id or not (payload.query or "").strip():
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="research requires session_id and query")
            manager = request.app.state.research_run_manager
            if manager is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="research queue unavailable")
            run = manager.start(payload.session_id, payload.query, payload.scope)
            session_id = payload.session_id
        else:
            manager = request.app.state.daily_run_manager
            if manager is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="daily queue unavailable")
            run = manager.start(payload.daily_kind or "daily", payload.keyword)
            session_id = None
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except QueueUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="run queue unavailable") from None
    return {
        "run_id": run["run_id"], "kind": payload.kind, "session_id": session_id,
        "status": run["status"], "stream_url": f"/api/v1/runs/{run['run_id']}/events",
    }


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
    dependencies=[Depends(require_api_key)],
)
def get_run(run_id: str, request: Request) -> dict:
    manager = _manager_for_run(request, run_id)
    if manager is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    run = manager.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    events = []
    if run_id.startswith(("chat-", "research-")):
        events = request.app.state.agent.sessions.get_run_events(run_id, run["thread_id"])
    return _run_payload(request, run, events)


@router.get("/runs/{run_id}/events", dependencies=[Depends(require_api_key)])
def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    manager = _manager_for_run(request, run_id)
    if manager is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if not manager.get(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return StreamingResponse(
        manager.stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=CancelRunResponse,
    dependencies=[Depends(require_api_key)],
)
def cancel_run(run_id: str, request: Request) -> dict:
    try:
        manager = _manager_for_run(request, run_id)
        run = manager.cancel(run_id) if manager else None
    except QueueUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="run queue unavailable") from None
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run not found",
        )
    return {"run_id": run_id, "status": run["status"]}

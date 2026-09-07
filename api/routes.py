"""Versioned HTTP routes; all agent work stays behind the service boundary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from api.auth import require_api_key
from api.observability import render_prometheus_metrics
from api.redis_runs import QueueUnavailableError
from api.schemas import (
    BadcaseCandidateResponse,
    BadcaseCreateRequest,
    CancelRunResponse,
    ChatCreateRequest,
    ChatRunCreateRequest,
    ChatRunResponse,
    ChatRunStartResponse,
    DailyDigestResponse,
    DailyKeywordCreateRequest,
    DailyKeywordResponse,
    DailyPaperStatusUpdateRequest,
    DailyRunDetailResponse,
    PaperResponse,
    ResearchDocumentDetailResponse,
    ResearchDocumentResponse,
    RunCreateRequest,
    RunResponse,
    RunStartResponse,
    SessionCreateRequest,
    SessionMessageResponse,
    SessionResponse,
    SessionUsageResponse,
    WorkspaceSettingsResponse,
    WorkspaceSettingsUpdateRequest,
    WorkspaceUploadResponse,
)
from workspace_uploads import UploadValidationError


router = APIRouter(prefix="/api/v1")


def _session_payload(session: dict) -> dict:
    return {
        "thread_id": session["thread_id"],
        "title": session["title"],
        "preview": session.get("preview", ""),
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
    }


def _run_response(
    run: dict,
    *,
    kind: str,
    session_id: str | None,
    model: str = "",
    answer: str = "",
    duration_ms: int | float | None = None,
    metrics: dict | None = None,
    error_type: str = "",
    events: list[dict] | None = None,
    completed_at: str | None = None,
) -> dict:
    """Build the stable public representation shared by every run kind."""
    return {
        "run_id": run["run_id"],
        "session_id": session_id,
        "kind": kind,
        "status": run["status"],
        "model": model,
        "answer": answer,
        "duration_ms": duration_ms,
        "metrics": metrics or {},
        "error_type": error_type,
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "completed_at": completed_at,
        "events": events or [],
    }


def _chat_payload(run: dict, events: list[dict] | None = None) -> dict:
    return _run_response(
        run,
        kind="chat",
        session_id=run["thread_id"],
        model=run.get("model", ""),
        answer=run.get("answer", ""),
        duration_ms=run.get("duration_ms"),
        metrics=run.get("metrics", {}),
        error_type=run.get("error_type", ""),
        events=events,
        completed_at=run.get("completed_at"),
    )


def _research_payload(run: dict, events: list[dict] | None = None) -> dict:
    trace = run.get("trace") or {}
    usage = trace.get("usage") if isinstance(trace, dict) else {}
    safe_metrics = {
        key: value for key, value in (usage or {}).items()
        if key in {"prompt", "completion", "total", "calls"}
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    duration = trace.get("duration_ms") if isinstance(trace, dict) else None
    return _run_response(
        run,
        kind="research",
        session_id=run["thread_id"],
        answer=run.get("final_answer", ""),
        duration_ms=duration if isinstance(duration, (int, float)) else None,
        metrics=safe_metrics,
        error_type=str(trace.get("error_type") or "")[:120] if isinstance(trace, dict) else "",
        events=events,
    )


def _daily_payload(run: dict) -> dict:
    result = run.get("result") or {}
    selected = result.get("selected") if isinstance(result, dict) else []
    candidates = run.get("candidates") or []
    error_text = str(run.get("error_text") or "")
    return _run_response(
        run,
        kind="daily",
        session_id=None,
        metrics={
            "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
            "selected_count": len(selected) if isinstance(selected, list) else 0,
        },
        error_type=error_text.split(":", 1)[0][:120] if error_text else "",
    )


def _daily_run_detail_payload(scheduler, run: dict) -> dict:
    """Project a persisted daily result into a workbench document page."""
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    critique = run.get("critique") if isinstance(run.get("critique"), dict) else {}
    papers = []
    for candidate in scheduler.get_daily_candidates(str(run["run_id"])):
        title = str(candidate.get("title") or "").strip()
        if not title:
            continue
        curation = candidate.get("curation") if isinstance(candidate.get("curation"), dict) else {}
        raw_sources = candidate.get("sources") or candidate.get("source") or ""
        source = ", ".join(str(item) for item in raw_sources) if isinstance(raw_sources, list) else str(raw_sources)
        raw_tags = curation.get("tags") if isinstance(curation.get("tags"), list) else []
        year = candidate.get("year")
        citations = candidate.get("citation_count")
        papers.append({
            "title": title,
            "url": str(candidate.get("url") or ""),
            "source": source,
            "year": int(year) if isinstance(year, int) and not isinstance(year, bool) else None,
            "citation_count": int(citations) if isinstance(citations, int) and not isinstance(citations, bool) else None,
            "reason": str(curation.get("reason") or "")[:320],
            "tags": [str(tag)[:60] for tag in raw_tags[:8] if str(tag).strip()],
            "selected": bool(candidate.get("selected")),
        })
    warnings = critique.get("warnings") if isinstance(critique.get("warnings"), list) else []
    return {
        "run_id": run["run_id"],
        "kind": run.get("kind", "daily"),
        "status": run.get("status", "queued"),
        "created_at": run.get("created_at", ""),
        "updated_at": run.get("updated_at", ""),
        "brief": str(result.get("brief") or "")[:600],
        "warnings": [str(warning)[:160] for warning in warnings[:4] if str(warning).strip()],
        "papers": papers,
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


def _workspace_paths(request: Request):
    paths = getattr(request.app.state, "workspace_paths", None)
    if paths is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="workspace features unavailable")
    return paths


def _workspace_scheduler(request: Request):
    scheduler = getattr(request.app.state, "workspace_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="daily keyword service unavailable")
    return scheduler


def _research_documents(request: Request):
    store = getattr(request.app.state, "research_document_store", None)
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="research document service unavailable")
    return store


def _workspace_uploads(request: Request):
    uploads = getattr(request.app.state, "workspace_uploads", None)
    if uploads is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="workspace uploads unavailable")
    return uploads


def _document_payload(document: dict) -> dict:
    return {
        "document_id": document["document_id"],
        "title": document["title"],
        "summary": document.get("summary", ""),
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "revision": document["revision"],
    }


def _workspace_settings_payload(request: Request) -> dict:
    paths = _workspace_paths(request)
    cfg = request.app.state.agent.cfg
    stored = paths.read_settings()
    model = str(stored.get("model") or cfg.model).strip() or cfg.model
    return {
        "model": model,
        "rag_enabled": stored.get("rag_enabled") if isinstance(stored.get("rag_enabled"), bool) else cfg.rag_enabled,
        "pdf_max_pages": stored.get("pdf_max_pages") if isinstance(stored.get("pdf_max_pages"), int) and not isinstance(stored.get("pdf_max_pages"), bool) else cfg.pdf_max_pages,
        "daily_search_enabled": stored.get("daily_search_enabled") if isinstance(stored.get("daily_search_enabled"), bool) else cfg.daily_search_enabled,
        "restart_required": True,
    }


@router.get(
    "/workspace/research-documents",
    response_model=list[ResearchDocumentResponse],
    dependencies=[Depends(require_api_key)],
)
def list_research_documents(request: Request) -> list[dict]:
    """List document metadata without injecting document bodies into chat state."""
    return [_document_payload(document) for document in _research_documents(request).list(limit=50)]


@router.post(
    "/workspace/uploads",
    response_model=WorkspaceUploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def upload_workspace_file(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Store one local PDF/image and return an opaque id for a later chat run."""
    try:
        upload = _workspace_uploads(request).save(file.filename or "", file.file)
    except UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    finally:
        await file.close()
    return upload.public_payload()


@router.get(
    "/workspace/research-documents/{document_id}",
    response_model=ResearchDocumentDetailResponse,
    dependencies=[Depends(require_api_key)],
)
def get_research_document(document_id: str, request: Request) -> dict:
    document = _research_documents(request).read(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="research document not found")
    return {
        **_document_payload(document),
        "content": document.get("content", ""),
        "download_url": f"/api/v1/workspace/research-documents/{document_id}/download",
    }


@router.get(
    "/workspace/research-documents/{document_id}/download",
    dependencies=[Depends(require_api_key)],
)
def download_research_document(document_id: str, request: Request) -> FileResponse:
    document = _research_documents(request).read(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="research document not found")
    path = _workspace_paths(request).safe_child(
        _workspace_paths(request).research_documents_dir / document_id,
        str(document.get("docx_file") or "document.docx"),
    )
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document download not found")
    return FileResponse(path, filename=f"{document['title']}.docx")


@router.get(
    "/workspace/papers",
    response_model=list[PaperResponse],
    dependencies=[Depends(require_api_key)],
)
def list_workspace_papers(request: Request) -> list[dict]:
    paper_store = getattr(request.app.state.agent, "paper_store", None)
    if paper_store is None:
        return []
    return list(paper_store.list_papers())


@router.get(
    "/workspace/daily-keywords",
    response_model=list[DailyKeywordResponse],
    dependencies=[Depends(require_api_key)],
)
def list_daily_keywords(request: Request) -> list[dict]:
    return [
        {
            "keyword": item["keyword"],
            "active": bool(item.get("active", True)),
            "added_at": item["added_at"],
            "search_status": item.get("search_status", "idle"),
        }
        for item in _workspace_scheduler(request).list_keywords()
    ]


@router.post(
    "/workspace/daily-keywords",
    response_model=DailyKeywordResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def create_daily_keyword(payload: DailyKeywordCreateRequest, request: Request) -> dict:
    scheduler = _workspace_scheduler(request)
    keyword = payload.keyword.strip()
    error = scheduler.validate_keyword(keyword)
    if error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error)
    if any(item["keyword"] == keyword for item in scheduler.list_keywords()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="关键词已存在")
    scheduler.add_keyword(keyword)
    return next(
        {
            "keyword": item["keyword"],
            "active": bool(item.get("active", True)),
            "added_at": item["added_at"],
            "search_status": item.get("search_status", "idle"),
        }
        for item in scheduler.list_keywords()
        if item["keyword"] == keyword
    )


@router.delete(
    "/workspace/daily-keywords/{keyword}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
)
def delete_daily_keyword(keyword: str, request: Request) -> Response:
    scheduler = _workspace_scheduler(request)
    if not any(item["keyword"] == keyword for item in scheduler.list_keywords()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关键词不存在")
    scheduler.remove_keyword(keyword)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/workspace/daily-digest",
    response_model=DailyDigestResponse,
    dependencies=[Depends(require_api_key)],
)
def get_daily_digest(request: Request) -> dict:
    scheduler = _workspace_scheduler(request)
    return {"progress": scheduler.get_progress(), "papers": scheduler.get_today_papers()}


@router.put(
    "/workspace/daily-papers/status",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
)
def update_daily_paper_status(
    payload: DailyPaperStatusUpdateRequest,
    request: Request,
) -> Response:
    scheduler = _workspace_scheduler(request)
    if not any(
        item["keyword"] == payload.keyword and item["title"] == payload.title
        for item in scheduler.get_today_papers()
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="今日检索结果中未找到该论文")
    scheduler.set_daily_paper_status(payload.keyword, payload.title, payload.status)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/workspace/daily-runs",
    response_model=list[RunResponse],
    dependencies=[Depends(require_api_key)],
)
def list_daily_runs(request: Request, limit: int = 20) -> list[dict]:
    scheduler = _workspace_scheduler(request)
    return [_daily_payload(run) for run in scheduler.list_daily_runs(limit=limit)]


@router.get(
    "/workspace/daily-runs/{run_id}",
    response_model=DailyRunDetailResponse,
    dependencies=[Depends(require_api_key)],
)
def get_daily_run_detail(run_id: str, request: Request) -> dict:
    scheduler = _workspace_scheduler(request)
    run = scheduler.get_daily_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="daily run not found")
    return _daily_run_detail_payload(scheduler, run)


@router.get(
    "/workspace/settings",
    response_model=WorkspaceSettingsResponse,
    dependencies=[Depends(require_api_key)],
)
def get_workspace_settings(request: Request) -> dict:
    return _workspace_settings_payload(request)


@router.put(
    "/workspace/settings",
    response_model=WorkspaceSettingsResponse,
    dependencies=[Depends(require_api_key)],
)
def update_workspace_settings(payload: WorkspaceSettingsUpdateRequest, request: Request) -> dict:
    updates = payload.model_dump(exclude_none=True)
    if "model" in updates:
        updates["model"] = updates["model"].strip()
        if not updates["model"]:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="模型不能为空")
    _workspace_paths(request).update_settings(updates)
    return _workspace_settings_payload(request)


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
    request.app.state.badcase_store.delete_for_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[SessionMessageResponse],
    dependencies=[Depends(require_api_key)],
)
def list_session_messages(session_id: str, request: Request) -> list[dict]:
    """Restore only the user-visible turns needed by a stateless web client."""
    if not request.app.state.agent.sessions.get(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    return request.app.state.agent.get_history(session_id)


@router.get(
    "/sessions/{session_id}/usage",
    response_model=SessionUsageResponse,
    dependencies=[Depends(require_api_key)],
)
def get_session_usage(session_id: str, request: Request) -> dict:
    """Return persisted counters without reloading conversation content."""
    if not request.app.state.agent.sessions.get(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    usage = request.app.state.agent.sessions.get_usage(session_id)
    return {
        key: max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        for key, value in {
            "prompt": usage.get("prompt"),
            "completion": usage.get("completion"),
            "total": usage.get("total"),
            "calls": usage.get("calls"),
            "context_limit": usage.get("context_limit"),
        }.items()
    }


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
            if not payload.session_id:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="chat requires session_id")
            message = str(payload.message or "").strip()
            if payload.upload_id:
                try:
                    message = _workspace_uploads(request).agent_message(payload.upload_id, message)
                except KeyError:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="upload not found") from None
            if not message:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="chat requires message or upload_id")
            run = _manager(request).start(payload.session_id, message)
            session_id = payload.session_id
        elif payload.kind == "research":
            if not payload.session_id or (not payload.resume and not (payload.query or "").strip()):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="research requires session_id and query")
            manager = request.app.state.research_run_manager
            if manager is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="research queue unavailable")
            run = manager.start(
                payload.session_id, payload.query or "", payload.scope, resume=payload.resume,
            )
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


def _badcase_payload(candidate: dict) -> dict:
    """Return feedback metadata only; the content-free snapshot remains local."""
    return {
        key: candidate.get(key)
        for key in (
            "candidate_id", "run_id", "category", "source", "status", "fingerprint",
            "occurrence_count", "created_at", "updated_at",
        )
    }


@router.get(
    "/workspace/badcases",
    response_model=list[BadcaseCandidateResponse],
    dependencies=[Depends(require_api_key)],
)
def list_badcases(request: Request, limit: int = 50) -> list[dict]:
    """List content-free feedback candidates for the local review workflow."""
    return [
        _badcase_payload(candidate)
        for candidate in request.app.state.badcase_store.list(limit=max(1, min(limit, 100)))
    ]


@router.post(
    "/runs/{run_id}/badcases",
    response_model=BadcaseCandidateResponse,
    dependencies=[Depends(require_api_key)],
)
def create_badcase(
    run_id: str,
    payload: BadcaseCreateRequest,
    request: Request,
    response: Response,
) -> dict:
    """Mark an existing run for review without copying its input or answer."""
    manager = _manager_for_run(request, run_id)
    if manager is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    run = manager.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    events = []
    if run_id.startswith(("chat-", "research-")):
        events = request.app.state.agent.sessions.get_run_events(run_id, run["thread_id"])
    candidate, created = request.app.state.badcase_store.create_candidate(
        _run_payload(request, run, events),
        category=payload.category,
        note=payload.note,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _badcase_payload(candidate)


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

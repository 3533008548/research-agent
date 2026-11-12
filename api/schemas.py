"""Small, explicit request and response contracts for the public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)


class SessionResponse(BaseModel):
    thread_id: str
    title: str
    preview: str = ""
    created_at: str
    updated_at: str


class SessionMessageResponse(BaseModel):
    """A user-visible message in one session's restored conversation."""

    role: Literal["user", "assistant"]
    content: str


class SessionUsageResponse(BaseModel):
    """Persisted token counters for the active conversation only."""

    prompt: int = 0
    completion: int = 0
    total: int = 0
    calls: int = 0
    context_limit: int = 0


class ProfileResponse(BaseModel):
    """The user-owned profile kept outside individual chat transcripts."""

    content: str = ""


class PaperResponse(BaseModel):
    paper_id: str
    title: str
    chunks: int
    indexed_at: str = ""
    relation_count: int = 0


class PaperRelationEvidenceResponse(BaseModel):
    paper_id: str
    title: str = ""
    page: int | None = None
    chunk_index: int | None = None
    note: str = ""


class PaperRelationResponse(BaseModel):
    relation_id: str
    source_paper_id: str
    source_title: str = ""
    target_paper_id: str
    target_title: str = ""
    relation_type: Literal[
        "method_similar", "method_improves", "experiment_comparable", "result_conflicts", "explicit_citation",
    ]
    note: str
    evidence: list[PaperRelationEvidenceResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class WorkspaceUploadResponse(BaseModel):
    upload_id: str
    filename: str
    kind: Literal["pdf", "image"]
    size_bytes: int
    duplicate: bool = False


class DailyKeywordResponse(BaseModel):
    keyword: str
    active: bool
    added_at: str
    search_status: str = "idle"


class DailyKeywordCreateRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=500)


class DailyPaperResponse(BaseModel):
    keyword: str
    title: str
    url: str = ""
    source: str = ""
    status: Literal["new", "want_read", "read", "skipped"] = "new"
    searched_at: str = ""


class DailyDigestResponse(BaseModel):
    progress: str = ""
    papers: list[DailyPaperResponse] = Field(default_factory=list)


class DailyRunPaperResponse(BaseModel):
    title: str
    url: str = ""
    source: str = ""
    year: int | None = None
    citation_count: int | None = None
    reason: str = ""
    tags: list[str] = Field(default_factory=list)
    selected: bool = False


class DailyRunDetailResponse(BaseModel):
    run_id: str
    kind: str
    status: str
    created_at: str
    updated_at: str
    brief: str = ""
    warnings: list[str] = Field(default_factory=list)
    papers: list[DailyRunPaperResponse] = Field(default_factory=list)
    # Stage events projected onto the shared timeline shape, so reloading a
    # finished daily run restores the same view the live stream drew.
    events: list[dict[str, Any]] = Field(default_factory=list)


class DailyPaperStatusUpdateRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1_000)
    status: Literal["want_read", "read", "skipped"]


class WorkspaceSettingsResponse(BaseModel):
    model: str
    rag_enabled: bool
    pdf_max_pages: int
    daily_search_enabled: bool
    restart_required: bool = True


class WorkspaceSettingsUpdateRequest(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=120)
    rag_enabled: bool | None = None
    pdf_max_pages: int | None = Field(default=None, ge=5, le=100)
    daily_search_enabled: bool | None = None


class ChatRunCreateRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)


class ChatCreateRequest(ChatRunCreateRequest):
    session_id: str = Field(min_length=1, max_length=120)


class ChatRunStartResponse(BaseModel):
    run_id: str
    session_id: str
    status: str
    stream_url: str


class ChatRunResponse(BaseModel):
    run_id: str
    session_id: str
    kind: str = "chat"
    status: str
    model: str = ""
    answer: str = ""
    duration_ms: float | None = None
    elapsed_ms: float | None = None
    metrics: dict[str, int | float] = Field(default_factory=dict)
    error_type: str = ""
    error_details: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    completed_at: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class RunCreateRequest(BaseModel):
    """Canonical create contract for chat, research and daily work."""

    kind: Literal["chat", "research", "daily"]
    session_id: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=12_000)
    upload_id: str | None = Field(default=None, max_length=80)
    query: str | None = Field(default=None, max_length=12_000)
    scope: Literal["both", "local", "public"] = "both"
    resume: bool = False
    daily_kind: Literal["daily", "retry", "search", "resume"] | None = None
    keyword: str | None = Field(default=None, max_length=500)


class RunStartResponse(BaseModel):
    run_id: str
    kind: str
    session_id: str | None = None
    project_id: str | None = None
    status: str
    stream_url: str


class RunSteerCreateRequest(BaseModel):
    """A user message that should guide an already-running task at its next node."""

    message: str = Field(min_length=1, max_length=12_000)


class ResearchControlRequest(BaseModel):
    action: Literal["pause", "stop", "resume", "steer", "material"]
    message: str = Field(default="", max_length=12_000)
    event_id: str | None = Field(default=None, max_length=120)


class RunSteerResponse(BaseModel):
    steer_id: int
    run_id: str
    status: Literal["pending", "consumed"]
    message: str
    consumed_stage: str = ""
    created_at: str
    consumed_at: str | None = None


class RunResponse(BaseModel):
    run_id: str
    kind: str
    session_id: str | None = None
    project_id: str | None = None
    status: str
    model: str = ""
    answer: str = ""
    duration_ms: float | None = None
    elapsed_ms: float | None = None
    metrics: dict[str, int | float] = Field(default_factory=dict)
    error_type: str = ""
    error_details: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    completed_at: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    steers: list[RunSteerResponse] = Field(default_factory=list)
    research: dict[str, Any] | None = None

class CancelRunResponse(BaseModel):
    run_id: str
    status: str


class BadcaseCreateRequest(BaseModel):
    """User feedback without copying the associated prompt or answer."""

    category: Literal[
        "retrieval_miss", "citation_quality", "answer_quality", "tool_failure",
        "performance", "safety", "other",
    ]
    note: str = Field(default="", max_length=500)


class BadcaseCandidateResponse(BaseModel):
    candidate_id: str
    run_id: str
    category: str
    source: str
    status: str
    fingerprint: str
    occurrence_count: int
    created_at: str
    updated_at: str

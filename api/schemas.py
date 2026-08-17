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
    metrics: dict[str, int | float] = Field(default_factory=dict)
    error_type: str = ""
    created_at: str
    updated_at: str
    completed_at: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class RunCreateRequest(BaseModel):
    """Canonical create contract for chat, research and daily discovery work."""

    kind: Literal["chat", "research", "daily"]
    session_id: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=12_000)
    query: str | None = Field(default=None, max_length=12_000)
    scope: Literal["both", "local", "public"] = "both"
    daily_kind: Literal["daily", "retry", "search", "resume"] | None = None
    keyword: str | None = Field(default=None, max_length=500)


class RunStartResponse(BaseModel):
    run_id: str
    kind: str
    session_id: str | None = None
    status: str
    stream_url: str


class RunResponse(BaseModel):
    run_id: str
    kind: str
    session_id: str | None = None
    status: str
    model: str = ""
    answer: str = ""
    duration_ms: float | None = None
    metrics: dict[str, int | float] = Field(default_factory=dict)
    error_type: str = ""
    created_at: str
    updated_at: str
    completed_at: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)

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

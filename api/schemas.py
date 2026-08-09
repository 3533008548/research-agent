"""Small, explicit request and response contracts for the public API."""

from __future__ import annotations

from typing import Any

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

class CancelRunResponse(BaseModel):
    run_id: str
    status: str

"""Small API-key guard for the externally callable FastAPI surface."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


class APIKeyAuthenticator:
    """Validate one deployment-scoped key without exposing it in logs or docs."""

    def __init__(self, api_key: str | None, *, required: bool = False) -> None:
        self._api_key = (api_key or "").strip()
        self.required = bool(required)
        if self.required and not self._api_key:
            raise ValueError("API_AUTH_REQUIRED=1 requires API_AUTH_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def verify(self, candidate: str | None) -> bool:
        if not self.enabled:
            return not self.required
        return bool(candidate) and hmac.compare_digest(candidate, self._api_key)


def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Accept ``X-API-Key`` or a bearer token for every protected API route."""
    candidate = x_api_key
    if not candidate and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    authenticator: APIKeyAuthenticator = request.app.state.api_authenticator
    if not authenticator.verify(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

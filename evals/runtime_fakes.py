"""Offline adapters used by the runtime replay suite.

The production orchestration code still calls its normal parsing and
persistence paths.  These helpers only replace transport and model boundaries
so a replay never sends a network request or reads a user runtime directory.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests


class FixtureResponse:
    """A small ``requests.Response``-compatible object for source fixtures."""

    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("fixture response has no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"offline fixture returned HTTP {self.status_code}")

    def close(self) -> None:
        return None


class FixtureSourceRouter:
    """Route known provider URLs to raw, versioned response shapes on disk."""

    _FILENAMES = {
        "openalex": "openalex_works.json",
        "openaire": "openaire_graph_v3.json",
        "dblp": "dblp_publications.json",
    }

    def __init__(
        self,
        fixtures_dir: str | Path,
        *,
        failed_sources: set[str] | None = None,
        seed: int = 42,
    ) -> None:
        self.fixtures_dir = Path(fixtures_dir)
        self.failed_sources = set(failed_sources or set())
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._random = random.Random(seed)

    def get(self, url: str, **_kwargs: Any) -> FixtureResponse:
        source = self._source_for_url(url)
        with self._lock:
            self.calls[source] += 1
            # Small deterministic jitter exercises the same concurrent source
            # completion path without making an offline replay noticeably slow.
            delay_ms = self._random.randint(0, 3)
        if delay_ms:
            time.sleep(delay_ms / 1000)
        if source in self.failed_sources:
            return FixtureResponse(status_code=429)
        filename = self._FILENAMES[source]
        payload = json.loads((self.fixtures_dir / filename).read_text(encoding="utf-8"))
        return FixtureResponse(payload=payload)

    @staticmethod
    def _source_for_url(url: str) -> str:
        if "api.openalex.org" in url:
            return "openalex"
        if "api.openaire.eu" in url:
            return "openaire"
        if "dblp.org" in url:
            return "dblp"
        raise AssertionError(f"unexpected external URL in offline replay: {url}")


class ScriptedResearchModels:
    """Deterministic responses for the two model stages used after resume."""

    def __init__(self) -> None:
        self.stages: list[str] = []

    def __call__(self, *, stage: str, usage: dict[str, int | float], **_kwargs: Any) -> str:
        self.stages.append(stage)
        usage["calls"] = int(usage.get("calls", 0)) + 1
        usage["prompt"] = int(usage.get("prompt", 0)) + 5
        usage["completion"] = int(usage.get("completion", 0)) + 4
        usage["total"] = int(usage.get("total", 0)) + 9
        if stage == "synthesis":
            return "离线恢复回放：已有证据支持该结论。[E1]"
        if stage == "critic":
            return '{"verdict":"pass","issues":[]}'
        raise AssertionError(f"unexpected model stage during resume: {stage}")

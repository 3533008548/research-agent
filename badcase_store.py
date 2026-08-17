"""Local, privacy-preserving storage for Badcase candidates.

A candidate is not a benchmark fixture.  It records only a de-identified run
snapshot and a reviewer category.  A developer must explicitly turn it into a
synthetic, reproducible evaluation case before it reaches version control.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BADCASE_SCHEMA_VERSION = 1
BADCASE_CATEGORIES = frozenset({
    "retrieval_miss",
    "citation_quality",
    "answer_quality",
    "tool_failure",
    "performance",
    "safety",
    "other",
})
BADCASE_STATUSES = frozenset({"triage", "promoted", "dismissed"})
_SAFE_METRIC_KEYS = frozenset({
    "at_ms", "duration_ms", "first_token_ms", "prompt", "completion",
    "total", "calls", "candidate_count", "selected_count", "model_calls",
})
_SAFE_METADATA_KEYS = frozenset({
    "protocol", "run_kind", "model", "runner", "scope", "toolset",
})


def _safe_machine_label(value: object, *, limit: int = 120) -> str:
    """Keep identifiers, but reject prose that could contain user content."""
    compact = str(value or "").strip()[:limit]
    if not compact:
        return ""
    return compact if all(
        character.isascii() and (character.isalnum() or character in "._:/-")
        for character in compact
    ) else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_metrics(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item for key, item in value.items()
        if key in _SAFE_METRIC_KEYS
        and isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(item)
    }


def _safe_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: safe for key, item in value.items()
        if key in _SAFE_METADATA_KEYS
        and isinstance(item, (str, int, float))
        and (safe := _safe_machine_label(item))
    }


def safe_run_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    """Project a public run object onto a content-free Badcase snapshot."""
    run_id = str(run.get("run_id") or "").strip()[:120]
    if not run_id:
        raise ValueError("Badcase 候选必须关联 run_id")
    timeline = []
    for event in list(run.get("events") or [])[-30:]:
        if not isinstance(event, dict):
            continue
        timeline.append({
            "event_type": _safe_machine_label(event.get("event_type"), limit=40) or "status",
            "stage": _safe_machine_label(event.get("stage"), limit=80) or "run",
            "status": _safe_machine_label(event.get("status"), limit=40) or "running",
            "error_type": _safe_machine_label(event.get("error_type")),
            "metrics": _safe_metrics(event.get("metrics")),
            "metadata": _safe_metadata(event.get("metadata")),
        })
    duration = run.get("duration_ms")
    return {
        "schema_version": BADCASE_SCHEMA_VERSION,
        "run": {
            "run_id": run_id,
            "kind": _safe_machine_label(run.get("kind"), limit=40) or "chat",
            "session_id": _safe_machine_label(run.get("session_id")),
            "status": _safe_machine_label(run.get("status"), limit=40) or "running",
            "model": _safe_machine_label(run.get("model")),
            "duration_ms": (
                duration
                if isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                else None
            ),
            "error_type": _safe_machine_label(run.get("error_type")),
        },
        "metrics": _safe_metrics(run.get("metrics")),
        "timeline": timeline,
    }


def _fingerprint(category: str, snapshot: dict[str, Any]) -> str:
    run = snapshot["run"]
    signature = {
        "category": category,
        "kind": run["kind"],
        "model": run["model"],
        "status": run["status"],
        "error_type": run["error_type"],
        "timeline": [
            (event["event_type"], event["stage"], event["status"], event["error_type"])
            for event in snapshot["timeline"]
        ],
    }
    encoded = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


class BadcaseStore:
    """Thread-safe local candidate pool; it never stores model or tool content."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS badcase_candidates (
                candidate_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user_feedback',
                status TEXT NOT NULL DEFAULT 'triage',
                fingerprint TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, category)
            );
            CREATE INDEX IF NOT EXISTS idx_badcase_candidates_status_updated
                ON badcase_candidates(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_badcase_candidates_fingerprint
                ON badcase_candidates(fingerprint, updated_at DESC);
            """
        )
        # The first public version already contains ``source``. Keep this
        # narrow migration for pre-release local databases created by a
        # development checkout, without asking users to discard feedback.
        if not self._table_has_column("badcase_candidates", "source"):
            self._conn.execute(
                "ALTER TABLE badcase_candidates ADD COLUMN source TEXT NOT NULL DEFAULT 'user_feedback'"
            )
        self._conn.commit()

    def _table_has_column(self, table: str, column: str) -> bool:
        return any(
            row[1] == column
            for row in self._conn.execute(f"PRAGMA table_info([{table}])")
        )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        try:
            result["snapshot"] = json.loads(result.pop("snapshot_json"))
        except (KeyError, TypeError, json.JSONDecodeError):
            result["snapshot"] = {}
        return result

    def create_candidate(
        self,
        run: dict[str, Any],
        *,
        category: str,
        note: str = "",
        source: str = "user_feedback",
    ) -> tuple[dict[str, Any], bool]:
        """Create an idempotent candidate for one run/category pair."""
        if category not in BADCASE_CATEGORIES:
            raise ValueError("不支持的 Badcase 分类")
        if source not in {"user_feedback", "automatic_signal", "manual_review"}:
            raise ValueError("不支持的 Badcase 来源")
        snapshot = safe_run_snapshot(run)
        safe_note = str(note or "").strip()[:500]
        run_info = snapshot["run"]
        now = _now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM badcase_candidates WHERE run_id=? AND category=?",
                (run_info["run_id"], category),
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE badcase_candidates SET occurrence_count=occurrence_count+1, updated_at=? "
                    "WHERE candidate_id=?",
                    (now, existing["candidate_id"]),
                )
                refreshed = self._conn.execute(
                    "SELECT * FROM badcase_candidates WHERE candidate_id=?", (existing["candidate_id"],),
                ).fetchone()
                return self._row(refreshed) or {}, False

            candidate_id = f"bc-{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO badcase_candidates("
                "candidate_id, run_id, session_id, category, source, status, fingerprint, note, "
                "occurrence_count, snapshot_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'triage', ?, ?, 1, ?, ?, ?)",
                (
                    candidate_id,
                    run_info["run_id"],
                    run_info["session_id"],
                    category,
                    source,
                    _fingerprint(category, snapshot),
                    safe_note,
                    json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM badcase_candidates WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
            return self._row(row) or {}, True

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM badcase_candidates WHERE candidate_id=?", (str(candidate_id or ""),),
            ).fetchone()
            return self._row(row)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status is not None and status not in BADCASE_STATUSES:
            raise ValueError("不支持的 Badcase 状态")
        with self._lock:
            params: tuple[Any, ...] = (max(1, min(int(limit), 200)),)
            query = "SELECT * FROM badcase_candidates"
            if status:
                query += " WHERE status=?"
                params = (status, *params)
            query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
            rows = self._conn.execute(query, params).fetchall()
            return [self._row(row) or {} for row in rows]

    def set_status(self, candidate_id: str, status: str) -> dict[str, Any] | None:
        if status not in BADCASE_STATUSES:
            raise ValueError("不支持的 Badcase 状态")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE badcase_candidates SET status=?, updated_at=? WHERE candidate_id=?",
                (status, _now(), str(candidate_id or "")),
            )
        return self.get(candidate_id)

    def delete_for_session(self, session_id: str) -> int:
        """Honor session deletion even though candidates live in a separate DB."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM badcase_candidates WHERE session_id=?", (str(session_id or ""),),
            )
            return max(0, int(cursor.rowcount))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __del__(self) -> None:
        """Avoid leaving an incidental SQLite handle open in short-lived clients/tests."""
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass


def promotion_template(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a non-executable draft; a human must fill synthetic fixtures."""
    stored_snapshot = candidate.get("snapshot")
    if not isinstance(stored_snapshot, dict):
        stored_snapshot = {}
    safe_snapshot = safe_run_snapshot({
        **(stored_snapshot.get("run") if isinstance(stored_snapshot.get("run"), dict) else {}),
        "metrics": stored_snapshot.get("metrics"),
        "events": stored_snapshot.get("timeline"),
    })
    return {
        "schema": "badcase-fixture-draft/v1",
        "source_candidate": {
            "candidate_id": candidate.get("candidate_id", ""),
            "category": candidate.get("category", ""),
            "fingerprint": candidate.get("fingerprint", ""),
            "safe_snapshot": safe_snapshot,
        },
        "to_fill_manually": {
            "redacted_input": "",
            "synthetic_corpus": [],
            "expected_behavior": "",
            "automated_assertions": [],
        },
        "promotion_rules": [
            "不得复制真实用户问题、模型回答、PDF 正文或工具原始结果。",
            "使用合成语料写出可稳定复现的断言。",
            "修复后需同时通过该样例和完整发布门禁。",
        ],
    }

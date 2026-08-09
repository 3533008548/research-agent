"""多会话元数据与 LangGraph checkpoint 的本地 SQLite 存储。

会话目录和 LangGraph 的 checkpoint 使用同一个数据库文件：这样删除会话时可以在
一笔事务中清掉元数据、Token 统计和对应的 checkpoint 行，避免出现“列表已删除但
历史还在”的半完成状态。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any


LEGACY_THREAD_ID = "research-main"


def _synchronized(method):
    """序列化同一 SQLite 连接上的目录读写；RLock 支持内部方法互相调用。"""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class SessionStore:
    """管理本地会话目录与运行元数据。

    完整对话仍由 LangGraph checkpoint 保存；``chat_runs.answer_text`` 仅镜像一轮
    最终可见回答，使 API 客户端在断开 SSE 后仍能查询该运行的结果。
    """

    def __init__(self, checkpoint_db: str):
        self.db_path = checkpoint_db
        Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                thread_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                preview TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_session_usage (
                thread_id TEXT PRIMARY KEY,
                usage_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES agent_sessions(thread_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated_at
                ON agent_sessions(updated_at DESC);
            CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                query TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL DEFAULT '{}',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                critique_json TEXT NOT NULL DEFAULT '{}',
                final_answer TEXT NOT NULL DEFAULT '',
                trace_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES agent_sessions(thread_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_research_runs_thread_updated
                ON research_runs(thread_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS chat_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                status TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                answer_text TEXT NOT NULL DEFAULT '',
                duration_ms REAL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error_type TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(thread_id) REFERENCES agent_sessions(thread_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_runs_thread_updated
                ON chat_runs(thread_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS agent_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                run_kind TEXT NOT NULL,
                agent TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error_type TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES agent_sessions(thread_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_agent_run_events_run
                ON agent_run_events(run_id, id);
            CREATE INDEX IF NOT EXISTS idx_agent_run_events_thread_created
                ON agent_run_events(thread_id, created_at DESC);
            """
        )
        # Existing runtime databases predate the API run-result endpoint.
        # SQLite ADD COLUMN is atomic here and preserves all prior chat rows.
        if not self._table_has_column("chat_runs", "answer_text"):
            self._conn.execute(
                "ALTER TABLE chat_runs ADD COLUMN answer_text TEXT NOT NULL DEFAULT ''"
            )
        self._conn.commit()

    @staticmethod
    def _now() -> str:
        # Second-level timestamps made sessions/runs created in one UI callback
        # sort nondeterministically. Keep enough precision for stable ordering.
        return datetime.now().isoformat(timespec="microseconds")

    @staticmethod
    def _first_message_title(text: str) -> str:
        """首条用户消息的前 20 个字符作为会话标题。"""
        compact = " ".join((text or "").split())
        return compact[:20] or "新会话"

    @_synchronized
    def ensure_legacy_session(self) -> bool:
        """把旧版固定 ``research-main`` checkpoint 注册为“历史会话”。"""
        row = self._conn.execute(
            "SELECT 1 FROM agent_sessions WHERE thread_id=?", (LEGACY_THREAD_ID,)
        ).fetchone()
        if row:
            return False
        if not self._has_checkpoint_for(LEGACY_THREAD_ID):
            return False
        now = self._now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_sessions(thread_id, title, preview, created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?)",
                (LEGACY_THREAD_ID, "历史会话", now, now),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO agent_session_usage(thread_id, usage_json, updated_at) "
                "VALUES (?, '{}', ?)",
                (LEGACY_THREAD_ID, now),
            )
        return True

    def _has_checkpoint_for(self, thread_id: str) -> bool:
        if not self._table_has_column("checkpoints", "thread_id"):
            return False
        return bool(
            self._conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id=? LIMIT 1", (thread_id,)
            ).fetchone()
        )

    def _table_has_column(self, table: str, column: str) -> bool:
        tables = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not tables:
            return False
        return any(row[1] == column for row in self._conn.execute(f"PRAGMA table_info([{table}])"))

    @_synchronized
    def create(self, title: str | None = None) -> dict[str, Any]:
        thread_id = f"session-{uuid.uuid4().hex[:12]}"
        now = self._now()
        session = {
            "thread_id": thread_id,
            "title": (title or "新会话").strip()[:20] or "新会话",
            "preview": "",
            "created_at": now,
            "updated_at": now,
        }
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_sessions(thread_id, title, preview, created_at, updated_at) "
                "VALUES (:thread_id, :title, :preview, :created_at, :updated_at)",
                session,
            )
            self._conn.execute(
                "INSERT INTO agent_session_usage(thread_id, usage_json, updated_at) VALUES (?, '{}', ?)",
                (thread_id, now),
            )
        return session

    @_synchronized
    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT thread_id, title, preview, created_at, updated_at "
            "FROM agent_sessions ORDER BY updated_at DESC, created_at DESC, rowid DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def get(self, thread_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT thread_id, title, preview, created_at, updated_at "
            "FROM agent_sessions WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def touch(self, thread_id: str, user_message: str | None = None) -> dict[str, Any]:
        session = self.get(thread_id)
        if not session:
            raise KeyError(f"会话不存在: {thread_id}")
        now = self._now()
        values: dict[str, str] = {"updated_at": now, "thread_id": thread_id}
        sets = ["updated_at=:updated_at"]
        if user_message is not None:
            preview = " ".join(user_message.split())[:80]
            values["preview"] = preview
            sets.append("preview=:preview")
            if session["title"] == "新会话" and preview:
                values["title"] = self._first_message_title(user_message)
                sets.append("title=:title")
        with self._conn:
            self._conn.execute(
                f"UPDATE agent_sessions SET {', '.join(sets)} WHERE thread_id=:thread_id",
                values,
            )
        return self.get(thread_id) or session

    @_synchronized
    def get_usage(self, thread_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT usage_json FROM agent_session_usage WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if not row:
            if not self.get(thread_id):
                raise KeyError(f"会话不存在: {thread_id}")
            return {}
        try:
            value = json.loads(row["usage_json"])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    @_synchronized
    def save_usage(self, thread_id: str, usage: dict[str, Any]) -> None:
        if not self.get(thread_id):
            raise KeyError(f"会话不存在: {thread_id}")
        now = self._now()
        encoded = json.dumps(usage, ensure_ascii=False, separators=(",", ":"))
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_session_usage(thread_id, usage_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET usage_json=excluded.usage_json, "
                "updated_at=excluded.updated_at",
                (thread_id, encoded, now),
            )

    @staticmethod
    def _decode_json(value: str, fallback: Any) -> Any:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return fallback
        return decoded

    @classmethod
    def _research_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["plan"] = cls._decode_json(item.pop("plan_json", "{}"), {})
        item["evidence"] = cls._decode_json(item.pop("evidence_json", "[]"), [])
        item["critique"] = cls._decode_json(item.pop("critique_json", "{}"), {})
        item["trace"] = cls._decode_json(item.pop("trace_json", "{}"), {})
        return item

    @classmethod
    def _chat_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metrics"] = cls._decode_json(item.pop("metrics_json", "{}"), {})
        item["answer"] = item.pop("answer_text", "")
        return item

    @staticmethod
    def _safe_event_metrics(metrics: dict[str, Any] | None) -> dict[str, int | float]:
        """Keep only bounded aggregate metrics; traces must never contain payloads."""
        allowed = {
            "at_ms", "duration_ms", "queue_wait_ms", "attempt", "model_calls",
            "tool_count", "candidate_count", "source_failures", "event_count",
        }
        safe: dict[str, int | float] = {}
        for key, value in (metrics or {}).items():
            if key not in allowed or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                safe[key] = round(value, 1) if isinstance(value, float) else value
        return safe

    @_synchronized
    def create_research_run(self, thread_id: str, query: str) -> dict[str, Any]:
        if not self.get(thread_id):
            raise KeyError(f"会话不存在: {thread_id}")
        now = self._now()
        run_id = f"research-{uuid.uuid4().hex[:12]}"
        with self._conn:
            self._conn.execute(
                "INSERT INTO research_runs "
                "(run_id, thread_id, query, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, thread_id, query, now, now),
            )
        return self.get_research_run(run_id) or {}

    @_synchronized
    def list_research_runs(self, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM research_runs WHERE thread_id=? "
            "ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT ?",
            (thread_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._research_row(row) for row in rows]

    @_synchronized
    def create_chat_run(self, thread_id: str, model: str) -> dict[str, Any]:
        if not self.get(thread_id):
            raise KeyError(f"会话不存在: {thread_id}")
        now = self._now()
        run_id = f"chat-{uuid.uuid4().hex[:12]}"
        with self._conn:
            self._conn.execute(
                "INSERT INTO chat_runs "
                "(run_id, thread_id, status, model, created_at, updated_at) "
                "VALUES (?, ?, 'running', ?, ?, ?)",
                (run_id, thread_id, str(model or "")[:120], now, now),
            )
            self._prune_chat_runs(thread_id)
        return self.get_chat_run(run_id) or {}

    @_synchronized
    def get_chat_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM chat_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._chat_row(row) if row else None

    @_synchronized
    def list_chat_runs(self, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM chat_runs WHERE thread_id=? "
            "ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT ?",
            (thread_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._chat_row(row) for row in rows]

    @_synchronized
    def update_chat_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        answer: str | None = None,
        duration_ms: float | None = None,
        metrics: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get_chat_run(run_id)
        if not existing:
            return None
        now = self._now()
        values: dict[str, Any] = {"run_id": run_id, "updated_at": now}
        sets = ["updated_at=:updated_at"]
        if status is not None:
            values["status"] = str(status)[:40]
            sets.append("status=:status")
            if status in {"completed", "failed", "cancelled"}:
                values["completed_at"] = now
                sets.append("completed_at=:completed_at")
        if answer is not None:
            values["answer"] = str(answer)
            sets.append("answer_text=:answer")
        if duration_ms is not None:
            values["duration_ms"] = round(float(duration_ms), 1)
            sets.append("duration_ms=:duration_ms")
        if metrics is not None:
            values["metrics_json"] = json.dumps(
                self._safe_event_metrics(metrics), ensure_ascii=False, separators=(",", ":"),
            )
            sets.append("metrics_json=:metrics_json")
        if error_type is not None:
            values["error_type"] = str(error_type)[:120]
            sets.append("error_type=:error_type")
        with self._conn:
            self._conn.execute(
                f"UPDATE chat_runs SET {', '.join(sets)} WHERE run_id=:run_id", values,
            )
        return self.get_chat_run(run_id)

    def _prune_chat_runs(self, thread_id: str, keep: int = 100) -> None:
        stale = self._conn.execute(
            "SELECT run_id FROM chat_runs WHERE thread_id=? "
            "ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (thread_id, keep),
        ).fetchall()
        run_ids = [str(row["run_id"]) for row in stale]
        if not run_ids:
            return
        placeholders = ",".join("?" for _ in run_ids)
        self._conn.execute(
            f"DELETE FROM agent_run_events WHERE thread_id=? AND run_id IN ({placeholders})",
            [thread_id, *run_ids],
        )
        self._conn.execute(f"DELETE FROM chat_runs WHERE run_id IN ({placeholders})", run_ids)

    @_synchronized
    def add_run_event(
        self,
        thread_id: str,
        run_id: str,
        run_kind: str,
        agent: str,
        stage: str,
        status: str,
        *,
        summary: str = "",
        metrics: dict[str, Any] | None = None,
        error_type: str = "",
    ) -> None:
        if not self.get(thread_id):
            return
        if run_kind not in {"chat", "research"}:
            raise ValueError(f"未知运行类型: {run_kind}")
        compact_summary = " ".join(str(summary or "").split())[:240]
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_run_events "
                "(run_id, thread_id, run_kind, agent, stage, status, summary, metrics_json, error_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, thread_id, run_kind,
                    str(agent or "agent")[:80], str(stage or "run")[:80], str(status or "running")[:40],
                    compact_summary,
                    json.dumps(self._safe_event_metrics(metrics), ensure_ascii=False, separators=(",", ":")),
                    str(error_type or "")[:120], self._now(),
                ),
            )

    @_synchronized
    def get_run_events(self, run_id: str, thread_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT run_kind, agent, stage, status, summary, metrics_json, error_type, created_at "
            "FROM agent_run_events WHERE run_id=?"
        )
        values: list[str] = [run_id]
        if thread_id:
            query += " AND thread_id=?"
            values.append(thread_id)
        query += " ORDER BY id"
        rows = self._conn.execute(query, values).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["metrics"] = self._decode_json(item.pop("metrics_json", "{}"), {})
            events.append(item)
        return events

    @_synchronized
    def get_research_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM research_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return self._research_row(row) if row else None

    @_synchronized
    def get_latest_research_run(self, thread_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM research_runs WHERE thread_id=? "
            "ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return self._research_row(row) if row else None

    @_synchronized
    def update_research_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        plan: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        critique: dict[str, Any] | None = None,
        final_answer: str | None = None,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get_research_run(run_id)
        if not existing:
            return None
        values: dict[str, Any] = {"run_id": run_id, "updated_at": self._now()}
        sets = ["updated_at=:updated_at"]
        updates = {
            "status": status,
            "plan_json": json.dumps(plan, ensure_ascii=False, separators=(",", ":")) if plan is not None else None,
            "evidence_json": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) if evidence is not None else None,
            "critique_json": json.dumps(critique, ensure_ascii=False, separators=(",", ":")) if critique is not None else None,
            "final_answer": final_answer,
            "trace_json": json.dumps(trace, ensure_ascii=False, separators=(",", ":")) if trace is not None else None,
        }
        for column, value in updates.items():
            if value is not None:
                values[column] = value
                sets.append(f"{column}=:{column}")
        with self._conn:
            self._conn.execute(
                f"UPDATE research_runs SET {', '.join(sets)} WHERE run_id=:run_id",
                values,
            )
        return self.get_research_run(run_id)

    @_synchronized
    def delete(self, thread_id: str) -> bool:
        """硬删除一个会话及全部 LangGraph checkpoint 关联行。"""
        if not self.get(thread_id):
            return False
        session_tables = {"agent_sessions", "agent_session_usage"}
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        checkpoint_tables = [
            row["name"]
            for row in rows
            if row["name"] not in session_tables
            and self._table_has_column(row["name"], "thread_id")
        ]
        with self._conn:
            for table in checkpoint_tables:
                # table 名来自 sqlite_master 且已由 PRAGMA 验证；方括号避免关键字冲突。
                self._conn.execute(f"DELETE FROM [{table}] WHERE thread_id=?", (thread_id,))
            self._conn.execute("DELETE FROM agent_session_usage WHERE thread_id=?", (thread_id,))
            self._conn.execute("DELETE FROM agent_sessions WHERE thread_id=?", (thread_id,))
        return True

    @_synchronized
    def cleanup_orphaned_checkpoints(self) -> dict[str, int]:
        """删除没有会话目录记录的 checkpoint 行。

        这类行来自早期版本的删除缺陷，当前 UI 无法访问它们。迁移完
        ``research-main`` 后调用，可避免已删除对话长期残留在数据库中。
        """
        session_tables = {"agent_sessions", "agent_session_usage"}
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        checkpoint_tables = [
            row["name"]
            for row in rows
            if row["name"] not in session_tables
            and self._table_has_column(row["name"], "thread_id")
        ]
        deleted: dict[str, int] = {}
        with self._conn:
            for table in checkpoint_tables:
                cursor = self._conn.execute(
                    f"DELETE FROM [{table}] "
                    "WHERE thread_id NOT IN (SELECT thread_id FROM agent_sessions)"
                )
                deleted[table] = cursor.rowcount
        return deleted

    @_synchronized
    def close(self) -> None:
        self._conn.close()

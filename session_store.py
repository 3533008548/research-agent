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
    """管理本地会话目录；不保存聊天正文，正文由 LangGraph checkpoint 保存。"""

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
            """
        )
        self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

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
            "FROM agent_sessions ORDER BY updated_at DESC, created_at DESC"
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

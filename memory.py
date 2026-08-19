"""
🧠 记忆模块 — 对话摘要 + 三元组索引 (SQLite)

结构: APP_DATA_DIR/primary/db/memory.db
  triples   (id, paper_title, relation, value, created_at)
  summaries (id, thread_id, topic, summary, created_at)
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from runtime_paths import get_runtime_paths


class MemoryStore:
    """轻量记忆存储 — 三元组 + 话题摘要"""

    MAX_SUMMARIES_PER_THREAD = 3

    def __init__(self, db_path: str | None = None):
        db_path = db_path or str(get_runtime_paths().memory_db)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS triples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_title TEXT NOT NULL,
                relation TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_triple_unique
                ON triples(paper_title, relation, value);
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                topic TEXT DEFAULT '',
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS image_descriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_path TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # ── 三元组 ──

    def add_triple(self, paper_title: str, relation: str, value: str) -> bool:
        """添加三元组，已存在则跳过。返回是否新增。"""
        now = datetime.now().isoformat()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO triples (paper_title, relation, value, created_at) VALUES (?, ?, ?, ?)",
                    (paper_title, relation, value, now),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # 重复，跳过

    def search_triples(self, query: str, limit: int = 10) -> list[dict]:
        """模糊搜索三元组"""
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 50))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM triples WHERE paper_title LIKE ? OR relation LIKE ? OR value LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_triples_by_paper(self, paper_title: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM triples WHERE paper_title LIKE ? ORDER BY created_at DESC",
                (f"%{paper_title}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 摘要 ──

    def add_summary(self, thread_id: str, topic: str, summary: str):
        now = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries (thread_id, topic, summary, created_at) VALUES (?, ?, ?, ?)",
                (thread_id, topic, summary, now),
            )
            rows = self._conn.execute(
                "SELECT id FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (thread_id, self.MAX_SUMMARIES_PER_THREAD),
            ).fetchall()
            for r in rows:
                self._conn.execute("DELETE FROM summaries WHERE id=?", (r["id"],))
            self._conn.commit()

    def get_last_summary_time(self, thread_id: str) -> Optional[str]:
        """获取最近一次摘要的时间戳（用于频率控制）"""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return row["created_at"] if row else None

    def get_recent_summary(self, thread_id: str, topic: str = "") -> Optional[str]:
        """获取最近一条摘要，可选按话题过滤"""
        if topic:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT summary FROM summaries WHERE thread_id=? AND topic=? ORDER BY created_at DESC LIMIT 1",
                    (thread_id, topic),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT summary FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
                    (thread_id,),
                ).fetchall()
        return rows[0]["summary"] if rows else None

    def get_all_summaries(self, thread_id: str, topic: str = "") -> list[dict]:
        if topic:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM summaries WHERE thread_id=? AND topic=? ORDER BY created_at DESC LIMIT 3",
                    (thread_id, topic),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT 3",
                    (thread_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def search_summaries(
        self,
        query: str,
        thread_id: str,
        limit: int = 3,
    ) -> list[dict]:
        """Search only the current session's summaries.

        Conversation summaries are session-scoped by design.  Requiring a
        ``thread_id`` here prevents a model-facing memory search from leaking
        another conversation's context into the active session.
        """
        query = str(query or "").strip()
        thread_id = str(thread_id or "").strip()
        if not query or not thread_id:
            return []
        limit = max(1, min(int(limit), self.MAX_SUMMARIES_PER_THREAD))
        pattern = f"%{query}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM summaries "
                "WHERE thread_id=? AND (topic LIKE ? OR summary LIKE ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (thread_id, pattern, pattern, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_summaries(self, thread_id: str) -> int:
        """删除一个会话产生的摘要；论文三元组和图片缓存属于全局知识，不受影响。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM summaries WHERE thread_id=?", (thread_id,)
            )
            self._conn.commit()
        return cursor.rowcount

    # ── 图片描述缓存 ──

    def get_image_description(self, image_path: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT description FROM image_descriptions WHERE image_path=?",
                (image_path,),
            ).fetchone()
        return row["description"] if row else None

    def save_image_description(self, image_path: str, description: str):
        now = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO image_descriptions (image_path, description, created_at) VALUES (?, ?, ?)",
                (image_path, description, now),
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

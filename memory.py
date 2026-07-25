"""
🧠 记忆模块 — 对话摘要 + 三元组索引 (SQLite)

结构: memory.db
  triples   (id, paper_title, relation, value, created_at)
  summaries (id, thread_id, topic, summary, created_at)
"""

import sqlite3
from datetime import datetime
from typing import Optional


class MemoryStore:
    """轻量记忆存储 — 三元组 + 话题摘要"""

    def __init__(self, db_path: str = "memory.db"):
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
        """)
        self._conn.commit()

    # ── 三元组 ──

    def add_triple(self, paper_title: str, relation: str, value: str) -> bool:
        """添加三元组，已存在则跳过。返回是否新增。"""
        now = datetime.now().isoformat()
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
        rows = self._conn.execute(
            "SELECT * FROM triples WHERE paper_title LIKE ? OR relation LIKE ? OR value LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_triples_by_paper(self, paper_title: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM triples WHERE paper_title LIKE ? ORDER BY created_at DESC",
            (f"%{paper_title}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 摘要 ──

    def add_summary(self, thread_id: str, topic: str, summary: str):
        now = datetime.now().isoformat()
        self._conn.execute(
            "INSERT INTO summaries (thread_id, topic, summary, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, topic, summary, now),
        )
        # 保留最近 3 条同 thread 的
        rows = self._conn.execute(
            "SELECT id FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 3",
            (thread_id,),
        ).fetchall()
        for r in rows:
            self._conn.execute("DELETE FROM summaries WHERE id=?", (r["id"],))
        self._conn.commit()

    def get_recent_summary(self, thread_id: str, topic: str = "") -> Optional[str]:
        """获取最近一条摘要，可选按话题过滤"""
        if topic:
            rows = self._conn.execute(
                "SELECT summary FROM summaries WHERE thread_id=? AND topic=? ORDER BY created_at DESC LIMIT 1",
                (thread_id, topic),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT summary FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchall()
        return rows[0]["summary"] if rows else None

    def get_all_summaries(self, thread_id: str, topic: str = "") -> list[dict]:
        if topic:
            rows = self._conn.execute(
                "SELECT * FROM summaries WHERE thread_id=? AND topic=? ORDER BY created_at DESC LIMIT 3",
                (thread_id, topic),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM summaries WHERE thread_id=? ORDER BY created_at DESC LIMIT 3",
                (thread_id,),
            ).fetchall()
        return [dict(r) for r in rows]

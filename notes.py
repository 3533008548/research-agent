"""
📝 笔记模块 — SQLite 持久化科研笔记

数据模型:
  topics (id INTEGER PRIMARY KEY, name TEXT UNIQUE, created_at TEXT)
  notes  (id INTEGER PRIMARY KEY, topic_id INTEGER, content TEXT, created_at TEXT)

用法:
  store = NoteStore()
  store.set_topic("TSN调度")
  store.add("这篇论文的奖励函数使用...")
  notes = store.list_notes()
  store.delete(3)
  store.list_topics()
"""

import sqlite3
import os
from datetime import datetime
from pathlib import Path

from runtime_paths import get_runtime_paths


class NoteStore:
    """科研笔记存储 — 按研究方向分话题"""

    def __init__(self, db_path: str | None = None):
        db_path = db_path or str(get_runtime_paths().notes_db)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                paper_title TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (topic_id) REFERENCES topics(id)
            );
        """)
        # 兼容旧表：无 paper_title 列时自动添加
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(notes)")]
        if "paper_title" not in cols:
            self._conn.execute("ALTER TABLE notes ADD COLUMN paper_title TEXT DEFAULT ''")
        self._conn.commit()

    # ── 话题管理 ──

    def list_topics(self) -> list[dict]:
        """列出所有话题"""
        rows = self._conn.execute(
            "SELECT id, name, created_at, "
            "(SELECT COUNT(*) FROM notes WHERE topic_id=topics.id) AS count "
            "FROM topics ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_topic(self, name_or_id) -> dict | None:
        """按名称或 ID 获取话题"""
        if isinstance(name_or_id, int) or name_or_id.isdigit():
            row = self._conn.execute(
                "SELECT * FROM topics WHERE id=?", (int(name_or_id),)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM topics WHERE name=?", (name_or_id,)
            ).fetchone()
            if not row:
                # 模糊匹配
                row = self._conn.execute(
                    "SELECT * FROM topics WHERE name LIKE ? LIMIT 1",
                    (f"%{name_or_id}%",)
                ).fetchone()
        return dict(row) if row else None

    def ensure_topic(self, name: str) -> dict:
        """获取或创建话题"""
        row = self._conn.execute(
            "SELECT * FROM topics WHERE name=?", (name,)
        ).fetchone()
        if row:
            return dict(row)
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO topics (name, created_at) VALUES (?, ?)",
            (name, now),
        )
        self._conn.commit()
        return {"id": cur.lastrowid, "name": name, "created_at": now}

    # ── 笔记 CRUD ──

    def add(self, topic_name: str, content: str) -> int:
        """添加一条笔记，返回笔记 ID"""
        topic = self.ensure_topic(topic_name)
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO notes (topic_id, content, created_at) VALUES (?, ?, ?)",
            (topic["id"], content.strip(), now),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_notes(self, topic_name: str = None) -> list[dict]:
        """列出笔记（可选限定话题）"""
        if topic_name:
            topic = self.get_topic(topic_name)
            if not topic:
                return []
            rows = self._conn.execute(
                "SELECT n.id, n.content, n.created_at, t.name AS topic "
                "FROM notes n JOIN topics t ON n.topic_id=t.id "
                "WHERE n.topic_id=? ORDER BY n.created_at ASC",
                (topic["id"],),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT n.id, n.content, n.created_at, t.name AS topic "
                "FROM notes n JOIN topics t ON n.topic_id=t.id "
                "ORDER BY n.created_at DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, note_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT n.id, n.content, n.created_at, t.name AS topic "
            "FROM notes n JOIN topics t ON n.topic_id=t.id "
            "WHERE n.id=?", (note_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete(self, note_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def set_paper(self, note_id: int, paper_title: str) -> bool:
        """将笔记关联到一篇论文"""
        cur = self._conn.execute(
            "UPDATE notes SET paper_title=? WHERE id=?",
            (paper_title, note_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_topic(self, topic_name: str) -> int:
        """删除话题及其所有笔记，返回删除的笔记数"""
        topic = self.get_topic(topic_name)
        if not topic:
            return 0
        count = self._conn.execute(
            "DELETE FROM notes WHERE topic_id=?", (topic["id"],)
        ).rowcount
        self._conn.execute("DELETE FROM topics WHERE id=?", (topic["id"],))
        self._conn.commit()
        return count

    def search(self, query: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT n.id, n.content, n.created_at, t.name AS topic "
            "FROM notes n JOIN topics t ON n.topic_id=t.id "
            "WHERE n.content LIKE ? OR t.name LIKE ? "
            "ORDER BY n.created_at DESC LIMIT 20",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        return [dict(r) for r in rows]

"""Preview and safely remove historical real-evaluation sessions."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_paths import RuntimePaths
from session_store import SessionStore


_EVAL_TITLE = re.compile(r"^真实评测\s+T\d{2}$")
_CORRUPTED_EVAL_TITLE = re.compile(r"^\?+\s+T\d{2}\s+\?+$")


def is_eval_session(session: dict) -> bool:
    """Match only the known title shapes created by the historical eval run."""
    title = " ".join(str(session.get("title") or "").split())
    return bool(_EVAL_TITLE.fullmatch(title) or _CORRUPTED_EVAL_TITLE.fullmatch(title))


def _backup_sqlite(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"checkpoint-before-eval-cleanup-{timestamp}.db"
    source_conn = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return destination


def _summary(store: SessionStore, session: dict) -> dict:
    thread_id = str(session["thread_id"])
    return {
        "thread_id": thread_id,
        "title": session["title"],
        "created_at": session["created_at"],
        "chat_runs": len(store.list_chat_runs(thread_id, limit=100)),
        "research_runs": len(store.list_research_runs(thread_id, limit=100)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="预览或清理误写入主运行时的真实评测会话")
    parser.add_argument("--data-dir", default=None, help="运行时目录（默认 APP_DATA_DIR 或 runtime）")
    parser.add_argument("--session-id", action="append", default=[], help="额外清理的精确会话 ID；可重复")
    parser.add_argument("--backup-dir", default="backups", help="执行清理前 checkpoint 备份目录")
    parser.add_argument("--apply", action="store_true", help="确认删除；默认仅预览")
    args = parser.parse_args()

    paths = RuntimePaths.from_root(args.data_dir)
    store = SessionStore(str(paths.checkpoint_db))
    memory_store = None
    try:
        requested = {item.strip() for item in args.session_id if item.strip()}
        matches = [
            session for session in store.list()
            if is_eval_session(session) or session["thread_id"] in requested
        ]
        payload: dict[str, object] = {
            "mode": "apply" if args.apply else "preview",
            "matches": [_summary(store, session) for session in matches],
        }
        if not args.apply:
            payload["next_step"] = "确认后执行: python scripts/cleanup_eval_sessions.py --apply"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if not matches:
            payload["deleted"] = []
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        backup = _backup_sqlite(paths.checkpoint_db, Path(args.backup_dir).expanduser().resolve())
        deleted = []
        # Keep the same cross-store semantics as ResearchAgent.delete_session:
        # SessionStore removes checkpoints/runs/events, while MemoryStore owns
        # optional per-session summaries in the separate memory database.
        from memory import MemoryStore

        memory_store = MemoryStore(str(paths.memory_db))
        for session in matches:
            thread_id = str(session["thread_id"])
            deleted_now = store.delete(thread_id)
            if deleted_now:
                memory_store.delete_summaries(thread_id)
            deleted.append({"thread_id": thread_id, "deleted": deleted_now})
        payload["backup"] = str(backup)
        payload["deleted"] = deleted
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        if memory_store is not None:
            memory_store.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

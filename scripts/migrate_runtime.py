"""将旧版工作目录数据复制到版本化 runtime 目录。

默认只预览；传入 --apply 才会写入。脚本从不删除旧数据。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_paths import RuntimePaths


def _copy_sqlite(source: Path, destination: Path, apply: bool, overwrite: bool) -> str:
    if not source.exists():
        return "skip（源文件不存在）"
    if destination.exists() and not overwrite:
        return "skip（目标已存在，使用 --overwrite 才会替换）"
    if not apply:
        return "preview（将执行 SQLite 在线备份）"

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    source_conn = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return "copied"


def _copy_file(source: Path, destination: Path, apply: bool, overwrite: bool) -> str:
    if not source.exists():
        return "skip（源文件不存在）"
    if destination.exists() and not overwrite:
        return "skip（目标已存在，使用 --overwrite 才会替换）"
    if not apply:
        return "preview（将复制文件）"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return "copied"


def _copy_tree(
    source: Path,
    destination: Path,
    apply: bool,
    overwrite: bool,
    ignore=None,
) -> str:
    if not source.exists():
        return "skip（源目录不存在）"
    if any(destination.iterdir()) and not overwrite:
        return "skip（目标目录非空，使用 --overwrite 才会合并）"
    if not apply:
        return "preview（将复制目录内容）"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
    return "copied"


def main() -> int:
    parser = argparse.ArgumentParser(description="复制旧版项目数据到统一 runtime 目录")
    parser.add_argument("--source", default=".", help="旧版项目数据所在目录（默认当前目录）")
    parser.add_argument("--data-dir", default=None, help="目标运行时目录（默认 APP_DATA_DIR 或 runtime）")
    parser.add_argument("--apply", action="store_true", help="执行复制；默认仅预览")
    parser.add_argument("--overwrite", action="store_true", help="显式允许替换目标同名数据")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    paths = RuntimePaths.from_root(args.data_dir)
    paths.ensure_initialized()
    actions = [
        ("checkpoint.db", lambda: _copy_sqlite(source / "checkpoint.db", paths.checkpoint_db, args.apply, args.overwrite)),
        ("memory.db", lambda: _copy_sqlite(source / "memory.db", paths.memory_db, args.apply, args.overwrite)),
        ("daily.db", lambda: _copy_sqlite(source / "daily.db", paths.daily_db, args.apply, args.overwrite)),
        ("badcases.db", lambda: _copy_sqlite(source / "badcases.db", paths.badcases_db, args.apply, args.overwrite)),
        ("profile.md", lambda: _copy_file(source / "profile.md", paths.profile_path, args.apply, args.overwrite)),
        ("chroma_data", lambda: _copy_tree(source / "chroma_data", paths.chroma_dir, args.apply, args.overwrite)),
        ("data/papers", lambda: _copy_tree(
            source / "data" / "papers", paths.papers_dir, args.apply, args.overwrite,
            ignore=shutil.ignore_patterns("images"),
        )),
        ("data/papers/images", lambda: _copy_tree(source / "data" / "papers" / "images", paths.images_dir, args.apply, args.overwrite)),
    ]

    results = {}
    for label, action in actions:
        try:
            results[label] = action()
        except Exception as exc:
            results[label] = f"error（{type(exc).__name__}: {exc}）"
        print(f"{label}: {results[label]}")

    if args.apply:
        log_file = paths.meta_dir / "migrations.json"
        history = []
        if log_file.exists():
            try:
                history = json.loads(log_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                history = []
        history.append({
            "source": str(source),
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        })
        log_file.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n✅ 迁移完成；旧数据仍保留。运行时目录: {paths.root}")
    else:
        print("\n这是预览。确认后执行: python scripts/migrate_runtime.py --apply")
    return 0 if not any(v.startswith("error") for v in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

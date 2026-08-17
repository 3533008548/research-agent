"""导出运行时原始数据的可移植 ZIP 备份。"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
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


def _backup_sqlite(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="备份运行时原始数据")
    parser.add_argument("--data-dir", default=None, help="运行时目录（默认 APP_DATA_DIR 或 runtime）")
    parser.add_argument("--output", default="backups", help="ZIP 输出目录")
    parser.add_argument("--include-derived", action="store_true", help="同时备份可重建的 Chroma 和图片缓存")
    args = parser.parse_args()

    paths = RuntimePaths.from_root(args.data_dir)
    paths.ensure_initialized()
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_base = output / f"research-agent-runtime-{timestamp}"

    with tempfile.TemporaryDirectory(prefix="research-agent-backup-") as temp:
        staging = Path(temp) / "runtime"
        for source, relative in (
            (paths.checkpoint_db, "primary/db/checkpoint.db"),
            (paths.memory_db, "primary/db/memory.db"),
            (paths.notes_db, "primary/db/notes.db"),
            (paths.daily_db, "primary/db/daily.db"),
            (paths.badcases_db, "primary/db/badcases.db"),
        ):
            _backup_sqlite(source, staging / relative)
        for source, relative in (
            (paths.profile_path, "primary/profile.md"),
            (paths.settings_file, "primary/settings.json"),
            (paths.state_file, "meta/state.json"),
        ):
            if source.exists():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if paths.papers_dir.exists():
            shutil.copytree(paths.papers_dir, staging / "primary/papers")
        if args.include_derived:
            for source, relative in (
                (paths.chroma_dir, "derived/chroma"),
                (paths.images_dir, "derived/images"),
            ):
                if source.exists():
                    shutil.copytree(source, staging / relative)
        archive = shutil.make_archive(str(archive_base), "zip", temp, "runtime")

    print(f"✅ 已创建备份: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

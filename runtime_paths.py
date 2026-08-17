"""运行时数据边界：将所有可变状态收敛到一个可挂载目录。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


STATE_SCHEMA_VERSION = 1
_settings_lock = RLock()


class RuntimeStateError(RuntimeError):
    """运行时目录的状态版本不受当前程序支持。"""


@dataclass(frozen=True)
class RuntimePaths:
    """用户数据目录的唯一入口。

    ``primary`` 保存不可丢失的用户数据；``derived`` 保存可由原始数据重建的
    向量索引和 PDF 图片。Docker 中只需把 ``root`` 挂载到容器即可。
    """

    root: Path

    @classmethod
    def from_root(cls, root: str | os.PathLike[str] | None = None) -> "RuntimePaths":
        selected = root or os.getenv("APP_DATA_DIR") or "runtime"
        return cls(Path(selected).expanduser().resolve())

    @property
    def primary_dir(self) -> Path:
        return self.root / "primary"

    @property
    def database_dir(self) -> Path:
        return self.primary_dir / "db"

    @property
    def papers_dir(self) -> Path:
        return self.primary_dir / "papers"

    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    @property
    def chroma_dir(self) -> Path:
        return self.derived_dir / "chroma"

    @property
    def images_dir(self) -> Path:
        return self.derived_dir / "images"

    @property
    def meta_dir(self) -> Path:
        return self.root / "meta"

    @property
    def state_file(self) -> Path:
        return self.meta_dir / "state.json"

    @property
    def settings_file(self) -> Path:
        return self.primary_dir / "settings.json"

    @property
    def checkpoint_db(self) -> Path:
        return self.database_dir / "checkpoint.db"

    @property
    def memory_db(self) -> Path:
        return self.database_dir / "memory.db"

    @property
    def notes_db(self) -> Path:
        return self.database_dir / "notes.db"

    @property
    def daily_db(self) -> Path:
        return self.database_dir / "daily.db"

    @property
    def badcases_db(self) -> Path:
        """本地 Badcase 候选池；仅保存脱敏运行快照与人工分类。"""
        return self.database_dir / "badcases.db"

    @property
    def profile_path(self) -> Path:
        return self.primary_dir / "profile.md"

    def ensure_initialized(self) -> None:
        """创建缺失目录并校验状态版本；不迁移、不删除任何旧数据。"""
        for directory in (
            self.database_dir,
            self.papers_dir,
            self.chroma_dir,
            self.images_dir,
            self.meta_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        if not self.state_file.exists():
            self._atomic_write_json(
                self.state_file,
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return

        state = self._read_json(self.state_file)
        version = state.get("schema_version")
        if not isinstance(version, int) or version > STATE_SCHEMA_VERSION:
            raise RuntimeStateError(
                f"运行时数据版本 {version!r} 不受当前程序支持；请升级程序后再启动。"
            )

    def read_settings(self) -> dict[str, Any]:
        self.ensure_initialized()
        with _settings_lock:
            return self._read_json(self.settings_file) if self.settings_file.exists() else {}

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """原子保存用户可修改设置，避免 Web UI 覆盖静态 config.yaml。"""
        self.ensure_initialized()
        with _settings_lock:
            current = self._read_json(self.settings_file) if self.settings_file.exists() else {}
            current.update(updates)
            self._atomic_write_json(self.settings_file, current)
            return current

    @staticmethod
    def safe_child(directory: Path, filename: str) -> Path:
        """返回受控目录内的文件名路径，拒绝上传名中的路径穿越。"""
        base = directory.resolve()
        candidate = (base / Path(filename).name).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("文件路径必须位于运行时数据目录内") from exc
        return candidate

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeStateError(f"无法读取运行时文件: {path}") from exc
        if not isinstance(value, dict):
            raise RuntimeStateError(f"运行时文件必须是 JSON 对象: {path}")
        return value

    @staticmethod
    def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)


def get_runtime_paths() -> RuntimePaths:
    """供无 Config 注入的工具模块获取当前运行时路径。"""
    paths = RuntimePaths.from_root()
    paths.ensure_initialized()
    return paths

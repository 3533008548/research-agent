"""One local file-import boundary shared by the web clients.

The browser never receives the server-side path.  It only gets an opaque
upload id; the API turns that id into the existing ``read_pdf`` or
``describe_image`` agent instruction when a chat run is created.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from runtime_paths import RuntimePaths


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_UPLOAD_KINDS: dict[str, tuple[Literal["pdf", "image"], str]] = {
    ".pdf": ("pdf", "read_pdf"),
    ".png": ("image", "describe_image"),
    ".jpg": ("image", "describe_image"),
    ".jpeg": ("image", "describe_image"),
}


class UploadValidationError(ValueError):
    """The selected file cannot enter the local research workspace."""


@dataclass(frozen=True)
class WorkspaceUpload:
    """A stored upload and the non-secret handle given to the browser."""

    upload_id: str
    filename: str
    kind: Literal["pdf", "image"]
    size_bytes: int
    duplicate: bool
    path: Path
    tool_name: str

    def public_payload(self) -> dict[str, str | int | bool]:
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "duplicate": self.duplicate,
        }


class WorkspaceUploadStore:
    """Store a local upload once and resolve it when its run starts."""

    def __init__(self, paths: RuntimePaths, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self.paths = paths
        self.max_bytes = max(1, int(max_bytes))
        self._uploads: dict[str, WorkspaceUpload] = {}
        self._lock = threading.RLock()

    def save(self, filename: str, source: BinaryIO) -> WorkspaceUpload:
        """Copy an allowed file into runtime storage without overwriting a file."""
        safe_name = Path(str(filename or "").replace("\\", "/")).name
        extension = Path(safe_name).suffix.lower()
        kind_and_tool = _UPLOAD_KINDS.get(extension)
        if not safe_name or not kind_and_tool:
            raise UploadValidationError("仅支持 PDF、PNG、JPG 或 JPEG 文件")

        self.paths.ensure_initialized()
        kind, tool_name = kind_and_tool
        directory = self.paths.papers_dir if kind == "pdf" else self.paths.images_dir
        temp_path = self.paths.safe_child(directory, f".upload-{uuid.uuid4().hex}{extension}")
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with temp_path.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > self.max_bytes:
                        raise UploadValidationError(
                            f"文件过大（上限 {self.max_bytes // 1024 // 1024} MB）",
                        )
                    digest.update(chunk)
                    target.write(chunk)

            existing = self._same_content_file(
                directory, extension, digest.hexdigest(), exclude=temp_path,
            )
            duplicate = existing is not None
            if existing is not None:
                temp_path.unlink(missing_ok=True)
                destination = existing
            else:
                destination = self._available_path(directory, safe_name)
                os.replace(temp_path, destination)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        upload = WorkspaceUpload(
            upload_id=f"upload-{uuid.uuid4().hex[:16]}",
            filename=destination.name,
            kind=kind,
            size_bytes=size_bytes,
            duplicate=duplicate,
            path=destination,
            tool_name=tool_name,
        )
        with self._lock:
            self._uploads[upload.upload_id] = upload
        return upload

    def get(self, upload_id: str) -> WorkspaceUpload | None:
        with self._lock:
            return self._uploads.get(str(upload_id or ""))

    def agent_message(self, upload_id: str, note: str = "") -> str:
        upload = self.get(upload_id)
        if upload is None:
            raise KeyError(upload_id)
        command = f"{upload.tool_name} {upload.path}"
        return command if not note.strip() else f"{command}\n{note.strip()}"

    @staticmethod
    def _available_path(directory: Path, filename: str) -> Path:
        candidate = RuntimePaths.safe_child(directory, filename)
        if not candidate.exists():
            return candidate
        for number in range(1, 10_000):
            renamed = RuntimePaths.safe_child(
                directory, f"{candidate.stem}_{number}{candidate.suffix}",
            )
            if not renamed.exists():
                return renamed
        raise UploadValidationError("同名文件过多，无法生成新名称")

    @staticmethod
    def _same_content_file(
        directory: Path,
        extension: str,
        expected_digest: str,
        *,
        exclude: Path | None = None,
    ) -> Path | None:
        for candidate in directory.iterdir():
            if candidate == exclude or not candidate.is_file() or candidate.suffix.lower() != extension:
                continue
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            except OSError:
                continue
            if digest.hexdigest() == expected_digest:
                return candidate
        return None

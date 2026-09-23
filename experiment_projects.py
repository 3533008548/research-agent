"""Durable, user-owned storage for evidence-backed paper reproduction projects.

Experiment files intentionally live outside ``SessionStore``.  A conversation
can be deleted without deleting its generated implementation, while every
project still records the paper and run that created its latest revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_paths import RuntimePaths


_PROJECT_ID = re.compile(r"experiment-[a-f0-9]{12}\Z")
_ALLOWED_FILE_NAMES = {"README.md", "requirements.txt"}
_ALLOWED_SUFFIXES = {".py", ".md", ".json", ".txt", ".yaml", ".yml"}
_MAX_FILES = 16
_MAX_FILE_BYTES = 100_000


class ExperimentProjectStore:
    """Read and write small reproducible-code projects under the runtime root."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.paths.ensure_initialized()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _project_dir(self, project_id: str) -> Path:
        if not _PROJECT_ID.fullmatch(str(project_id or "")):
            raise ValueError("invalid experiment project id")
        return self.paths.experiment_projects_dir / project_id

    @staticmethod
    def _read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback or {}
        return value if isinstance(value, dict) else (fallback or {})

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(value, encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _safe_relative_path(value: str) -> Path:
        candidate = Path(str(value or ""))
        if (
            not str(value or "").strip()
            or candidate.is_absolute()
            or ".." in candidate.parts
            or len(candidate.parts) > 4
        ):
            raise ValueError("invalid experiment file path")
        if candidate.name not in _ALLOWED_FILE_NAMES and candidate.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError("unsupported experiment file type")
        return candidate

    @staticmethod
    def _file_record(root: Path, path: Path) -> dict[str, Any]:
        raw = path.read_bytes()
        return {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def create(self, paper: dict[str, Any]) -> dict[str, Any]:
        paper_id = str(paper.get("paper_id") or "").strip()
        title = str(paper.get("title") or "").strip()
        if not paper_id or not title:
            raise ValueError("paper_id and title are required for an experiment project")
        project_id = f"experiment-{uuid.uuid4().hex[:12]}"
        directory = self._project_dir(project_id)
        now = self._now()
        metadata = {
            "schema_version": 1,
            "project_id": project_id,
            "paper_id": paper_id,
            "paper_title": title,
            "status": "queued",
            "reproduction_level": "pending",
            "implementation_path": "not_checked",
            "summary": "等待从论文页面级证据提取实验规格。",
            "revision": 0,
            "latest_run_id": "",
            "created_at": now,
            "updated_at": now,
        }
        spec = {
            "schema_version": 1,
            "project_id": project_id,
            "paper": {
                "paper_id": paper_id,
                "title": title,
                "indexed_at": str(paper.get("indexed_at") or ""),
            },
            "reproduction_level": "blocked",
            "source_refs": [],
            "parameters": [],
            "datasets": [],
            "unknowns": ["尚未开始提取论文实验规格。"],
            "acceptance_criteria": [],
            "implementation_notes": [],
            "code_source": {
                "mode": "not_checked",
                "status": "not_checked",
                "candidates": [],
            },
        }
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "workspace").mkdir()
        (directory / "revisions").mkdir()
        (directory / "runs").mkdir()
        self._write_json(directory / "metadata.json", metadata)
        self._write_json(directory / "spec.json", spec)
        self._write_json(directory / "validation.json", {"valid": False, "checks": [], "errors": []})
        return metadata

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for directory in self.paths.experiment_projects_dir.iterdir():
            if not directory.is_dir() or not _PROJECT_ID.fullmatch(directory.name):
                continue
            metadata = self._read_json(directory / "metadata.json")
            if metadata:
                records.append(metadata)
        records.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("project_id") or "")), reverse=True)
        return records[:max(1, min(int(limit), 100))]

    def read(self, project_id: str) -> dict[str, Any] | None:
        try:
            directory = self._project_dir(project_id)
        except ValueError:
            return None
        metadata = self._read_json(directory / "metadata.json")
        if not metadata:
            return None
        workspace = directory / "workspace"
        files = [self._file_record(workspace, path) for path in workspace.rglob("*") if path.is_file()]
        files.sort(key=lambda item: item["path"])
        return {
            **metadata,
            "spec": self._read_json(directory / "spec.json"),
            "validation": self._read_json(directory / "validation.json", {"valid": False, "checks": [], "errors": []}),
            "files": files,
        }

    def read_file(self, project_id: str, relative_path: str) -> dict[str, Any] | None:
        try:
            directory = self._project_dir(project_id)
            path = self._safe_relative_path(relative_path)
        except ValueError:
            return None
        candidate = directory / "workspace" / path
        if not candidate.is_file():
            return None
        try:
            return {
                "path": path.as_posix(),
                "content": candidate.read_text(encoding="utf-8"),
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            }
        except OSError:
            return None

    def mark_queued(self, project_id: str, *, summary: str = "正在重新生成实验规格与代码。") -> dict[str, Any]:
        """Expose a retry immediately without replacing the last usable revision."""
        current = self.read(project_id)
        if current is None:
            raise KeyError(project_id)
        metadata = {
            key: value for key, value in current.items()
            if key not in {"spec", "validation", "files"}
        }
        metadata.update({"status": "queued", "summary": str(summary)[:2_000], "updated_at": self._now()})
        self._write_json(self._project_dir(project_id) / "metadata.json", metadata)
        return self.read(project_id) or metadata

    def delete(self, project_id: str) -> bool:
        """Remove one explicitly selected experiment project and all its files."""
        try:
            directory = self._project_dir(project_id)
        except ValueError:
            return False
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True

    def apply_confirmation(
        self,
        project_id: str,
        *,
        confirmed_items: list[str],
        resolved_unknowns: list[str] | None = None,
        summary: str = "",
    ) -> dict[str, Any]:
        """Persist user decisions and mirror them into README/config for the next edit."""
        current = self.read(project_id)
        if current is None:
            raise KeyError(project_id)
        items = [" ".join(str(item).split())[:500] for item in confirmed_items]
        items = [item for item in items if item]
        if not items:
            raise ValueError("至少需要一条明确确认内容")
        resolved = {" ".join(str(item).split()) for item in (resolved_unknowns or []) if str(item).strip()}
        spec = dict(current.get("spec") if isinstance(current.get("spec"), dict) else {})
        existing = spec.get("confirmed_items") if isinstance(spec.get("confirmed_items"), list) else []
        known_text = {
            str(item.get("text") or "") for item in existing if isinstance(item, dict)
        }
        now = self._now()
        additions = [
            {"text": item, "provenance": "user_confirmation", "confirmed_at": now}
            for item in items if item not in known_text
        ]
        spec["confirmed_items"] = [*existing, *additions][-40:]
        unknowns = _as_string_list(spec.get("unknowns"))
        spec["unknowns"] = [item for item in unknowns if " ".join(item.split()) not in resolved]

        workspace_files = self._workspace_file_contents(project_id, current.get("files") or [])
        file_map = {item["path"]: item["content"] for item in workspace_files}
        config_path = "configs/reproduction.json"
        try:
            config = json.loads(file_map.get(config_path, "{}"))
            config = config if isinstance(config, dict) else {}
        except json.JSONDecodeError:
            config = {}
        confirmations = config.get("user_confirmations") if isinstance(config.get("user_confirmations"), list) else []
        config["user_confirmations"] = [*confirmations, *items][-40:]
        config["parameters"] = spec.get("parameters") if isinstance(spec.get("parameters"), list) else []
        config["datasets"] = spec.get("datasets") if isinstance(spec.get("datasets"), list) else []
        file_map[config_path] = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        readme = file_map.get("README.md", "# 论文复现实验\n")
        note_lines = ["", "## 用户已确认的实验前提", *[f"- {item}" for item in items], ""]
        file_map["README.md"] = readme.rstrip() + "\n" + "\n".join(note_lines)
        files = [{"path": path, "content": content} for path, content in sorted(file_map.items())]
        validation = _validate_workspace_files(files)
        status = "ready" if validation["valid"] and not spec["unknowns"] else "needs_review"
        result_summary = str(summary).strip() or "已将用户确认内容写入实验规格、配置与说明文件。"
        return self.record_result(
            project_id,
            run_id=f"revision-{uuid.uuid4().hex[:12]}",
            spec=spec,
            files=files,
            validation=validation,
            summary=result_summary,
            status=status,
            set_latest_run_id=False,
        )

    def record_repository_candidates(
        self,
        project_id: str,
        *,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Save unverified GitHub candidates without treating any as official."""
        current = self._required_project(project_id)
        source = self._code_source(current)
        connected = source.get("mode") == "repository" and isinstance(source.get("repository"), dict)
        source["candidates"] = [_repository_record(item, include_commit=False) for item in candidates[:10]]
        source["candidate_status"] = "candidates_found" if candidates else "no_candidates_found"
        source["last_checked_at"] = self._now()
        if not connected:
            source["mode"] = "repository_search"
            source["status"] = str(source["candidate_status"])
        return self._record_source_update(
            current,
            source=source,
            implementation_path="repository" if connected else ("repository_candidate" if candidates else "paper_only"),
            summary=(
                "已更新 GitHub 候选仓库；候选不等于论文官方实现，请核验后再连接。"
                if candidates else
                "未找到可确认的 GitHub 候选仓库；这不等于论文必然闭源，可提供地址或按论文方法还原。"
            ),
        )

    def connect_repository(
        self,
        project_id: str,
        *,
        repository: dict[str, Any],
        relationship: str,
    ) -> dict[str, Any]:
        """Attach an inspected GitHub repository, pinned to an observed commit."""
        if relationship not in {"user_provided", "official_confirmed", "community", "candidate_unverified"}:
            raise ValueError("invalid repository relationship")
        current = self._required_project(project_id)
        source = self._code_source(current)
        source.update({
            "mode": "repository",
            "status": "connected",
            "relationship": relationship,
            "repository": _repository_record(repository, include_commit=True),
            "last_checked_at": self._now(),
        })
        relationship_label = {
            "user_provided": "用户提供，待核验",
            "candidate_unverified": "检索候选，待核验",
            "official_confirmed": "用户确认的官方实现",
            "community": "社区实现",
        }[relationship]
        return self._record_source_update(
            current,
            source=source,
            implementation_path="repository",
            summary=f"已连接 GitHub 仓库（{relationship_label}）。仅记录元数据与 commit，尚未克隆或执行代码。",
        )

    def use_method_reconstruction(self, project_id: str) -> dict[str, Any]:
        """Record the deliberate paper-only path without asserting the paper is closed source."""
        current = self._required_project(project_id)
        source = {
            "mode": "method_reconstruction",
            "status": "paper_only",
            "relationship": "paper_evidence",
            "candidates": [],
            "last_checked_at": self._now(),
        }
        return self._record_source_update(
            current,
            source=source,
            implementation_path="method_reconstruction",
            summary="已选择按论文方法还原；未声明论文闭源，也不会把该结果称为原作者代码复现。",
        )

    def record_method_reconstruction(
        self,
        project_id: str,
        *,
        run_id: str,
        spec: dict[str, Any],
        files: list[dict[str, str]],
        summary: str,
        status: str,
    ) -> dict[str, Any]:
        """Persist a paper-only reference implementation and its explicit assumptions.

        This is deliberately a separate path from a checked-out repository:
        the result is editable reconstruction code, not a claim about an
        author's original implementation or reported metrics.
        """
        current = self._required_project(project_id)
        updated_spec = dict(spec)
        source = {
            "mode": "method_reconstruction",
            "status": "reconstructed" if status != "blocked" else "blocked",
            "relationship": "paper_evidence",
            "candidates": [],
            "last_checked_at": self._now(),
        }
        updated_spec["code_source"] = source
        reconstruction = _method_reconstruction_record(
            updated_spec.get("method_reconstruction"),
            source_refs=updated_spec.get("source_refs"),
            unknowns=updated_spec.get("unknowns"),
        )
        updated_spec["method_reconstruction"] = reconstruction
        mirrored_files = _mirror_method_reconstruction(
            _mirror_code_source(files, source), reconstruction,
        )
        validation = _validate_workspace_files(mirrored_files)
        updated = self.record_result(
            project_id,
            run_id=run_id,
            spec=updated_spec,
            files=mirrored_files,
            validation=validation,
            summary=summary,
            status=status,
        )
        metadata_path = self._project_dir(project_id) / "metadata.json"
        metadata = self._read_json(metadata_path)
        metadata["implementation_path"] = "method_reconstruction"
        self._write_json(metadata_path, metadata)
        return self.read(project_id) or updated

    def repository_source_dir(self, project_id: str) -> Path:
        """Return the fixed, non-workspace checkout location for one project."""
        return self._project_dir(project_id) / "repository" / "source"

    def record_repository_preparation(
        self,
        project_id: str,
        *,
        run_id: str,
        manifest: dict[str, Any],
        summary: str,
    ) -> dict[str, Any]:
        """Version a source checkout manifest without treating it as generated code."""
        current = self._required_project(project_id)
        spec = dict(current.get("spec") if isinstance(current.get("spec"), dict) else {})
        prepared = _preparation_record({**manifest, "status": "prepared"})
        spec["repository_preparation"] = prepared
        files = self._workspace_file_contents(project_id, current.get("files") or [])
        files = _mirror_repository_preparation(files, prepared)
        validation = _validate_workspace_files(files)
        updated = self.record_result(
            project_id,
            run_id=run_id,
            spec=spec,
            files=files,
            validation=validation,
            summary=summary,
            status=str(current.get("status") or "needs_review"),
            set_latest_run_id=False,
        )
        metadata_path = self._project_dir(project_id) / "metadata.json"
        metadata = self._read_json(metadata_path)
        metadata["repository_status"] = "prepared"
        metadata["repository_source_path"] = "repository/source"
        self._write_json(metadata_path, metadata)
        return self.read(project_id) or updated

    def record_repository_preparation_failure(self, project_id: str, *, run_id: str, reason: str) -> dict[str, Any]:
        """Keep a failed source-sync attempt visible without discarding the last checkout."""
        current = self._required_project(project_id)
        spec = dict(current.get("spec") if isinstance(current.get("spec"), dict) else {})
        previous = spec.get("repository_preparation") if isinstance(spec.get("repository_preparation"), dict) else {}
        prepared = _preparation_record({
            **previous,
            "status": "failed",
            "error": str(reason)[:500],
            "attempted_at": self._now(),
        })
        spec["repository_preparation"] = prepared
        files = self._workspace_file_contents(project_id, current.get("files") or [])
        files = _mirror_repository_preparation(files, prepared)
        updated = self.record_result(
            project_id,
            run_id=run_id,
            spec=spec,
            files=files,
            validation=_validate_workspace_files(files),
            summary=f"上游仓库准备失败：{str(reason)[:500]}",
            status=str(current.get("status") or "needs_review"),
            set_latest_run_id=False,
        )
        metadata_path = self._project_dir(project_id) / "metadata.json"
        metadata = self._read_json(metadata_path)
        metadata["repository_status"] = "failed"
        self._write_json(metadata_path, metadata)
        return self.read(project_id) or updated

    def _required_project(self, project_id: str) -> dict[str, Any]:
        current = self.read(project_id)
        if current is None:
            raise KeyError(project_id)
        return current

    @staticmethod
    def _code_source(project: dict[str, Any]) -> dict[str, Any]:
        spec = project.get("spec") if isinstance(project.get("spec"), dict) else {}
        existing = spec.get("code_source") if isinstance(spec.get("code_source"), dict) else {}
        return dict(existing) if existing else {"mode": "not_checked", "status": "not_checked", "candidates": []}

    def _record_source_update(
        self,
        current: dict[str, Any],
        *,
        source: dict[str, Any],
        implementation_path: str,
        summary: str,
    ) -> dict[str, Any]:
        """Version one source decision and mirror it into the editable workspace."""
        project_id = str(current["project_id"])
        spec = dict(current.get("spec") if isinstance(current.get("spec"), dict) else {})
        spec["code_source"] = source
        files = self._workspace_file_contents(project_id, current.get("files") or [])
        files = _mirror_code_source(files, source)
        validation = _validate_workspace_files(files)
        updated = self.record_result(
            project_id,
            run_id=f"source-{uuid.uuid4().hex[:12]}",
            spec=spec,
            files=files,
            validation=validation,
            summary=summary,
            status=str(current.get("status") or "needs_review"),
            set_latest_run_id=False,
        )
        metadata_path = self._project_dir(project_id) / "metadata.json"
        metadata = self._read_json(metadata_path)
        metadata["implementation_path"] = implementation_path
        self._write_json(metadata_path, metadata)
        return self.read(project_id) or updated

    def _workspace_file_contents(self, project_id: str, files: list[dict[str, Any]]) -> list[dict[str, str]]:
        contents: list[dict[str, str]] = []
        for item in files:
            record = self.read_file(project_id, str(item.get("path") or ""))
            if record is not None:
                contents.append({"path": record["path"], "content": record["content"]})
        return contents

    def record_result(
        self,
        project_id: str,
        *,
        run_id: str,
        spec: dict[str, Any],
        files: list[dict[str, str]],
        validation: dict[str, Any],
        summary: str,
        status: str,
        set_latest_run_id: bool = True,
    ) -> dict[str, Any]:
        directory = self._project_dir(project_id)
        current = self.read(project_id)
        if current is None:
            raise KeyError(project_id)
        code_source = spec.get("code_source") if isinstance(spec.get("code_source"), dict) else None
        if code_source and files:
            files = _mirror_code_source(files, code_source)
        if len(files) > _MAX_FILES:
            raise ValueError("too many generated experiment files")

        workspace = directory / "workspace"
        # Generated revisions replace the controlled project workspace as a coherent
        # unit.  Historical versions are copied below before the next replacement.
        staging = directory / f".workspace-{uuid.uuid4().hex[:8]}"
        staging.mkdir()
        try:
            seen: set[str] = set()
            for item in files:
                relative = self._safe_relative_path(str(item.get("path") or ""))
                key = relative.as_posix()
                content = str(item.get("content") or "")
                if key in seen or len(content.encode("utf-8")) > _MAX_FILE_BYTES:
                    raise ValueError("invalid generated experiment file")
                seen.add(key)
                self._write_text(staging / relative, content)
            if files and "README.md" not in seen:
                raise ValueError("generated experiment is missing README.md")

            revision = int(current.get("revision") or 0) + 1
            revision_dir = directory / "revisions" / f"r{revision:04d}"
            snapshot_source = staging if files else workspace
            if snapshot_source.exists():
                shutil.copytree(snapshot_source, revision_dir / "workspace")
            else:
                (revision_dir / "workspace").mkdir(parents=True, exist_ok=True)
            self._write_json(revision_dir / "spec.json", spec)
            self._write_json(revision_dir / "validation.json", validation)

            if files:
                archived = directory / f".workspace-old-{uuid.uuid4().hex[:8]}"
                if workspace.exists():
                    os.replace(workspace, archived)
                os.replace(staging, workspace)
                if archived.exists():
                    shutil.rmtree(archived)
            else:
                shutil.rmtree(staging)

            run_record = {
                "run_id": run_id,
                "project_id": project_id,
                "revision": revision,
                "status": status,
                "summary": str(summary)[:2_000],
                "created_at": self._now(),
            }
            self._write_json(directory / "runs" / f"{run_id}.json", run_record)
            self._write_json(directory / "spec.json", spec)
            self._write_json(directory / "validation.json", validation)
            metadata = {
                **current,
                "status": status,
                "reproduction_level": str(spec.get("reproduction_level") or "blocked"),
                "summary": str(summary)[:2_000],
                "revision": revision,
                "latest_run_id": run_id if set_latest_run_id else str(current.get("latest_run_id") or ""),
                "updated_at": self._now(),
            }
            metadata.pop("spec", None)
            metadata.pop("validation", None)
            metadata.pop("files", None)
            self._write_json(directory / "metadata.json", metadata)
            return self.read(project_id) or metadata
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def mark_blocked(self, project_id: str, *, run_id: str, reason: str) -> dict[str, Any]:
        current = self.read(project_id)
        if current is None:
            raise KeyError(project_id)
        spec = current.get("spec") if isinstance(current.get("spec"), dict) else {}
        spec.update({
            "reproduction_level": "blocked",
            "unknowns": [str(reason)[:500]],
            "source_refs": [],
        })
        return self.record_result(
            project_id,
            run_id=run_id,
            spec=spec,
            files=[],
            validation={"valid": False, "checks": [], "errors": [str(reason)[:500]]},
            summary=str(reason)[:2_000],
            status="blocked",
        )


def _as_string_list(value: Any) -> list[str]:
    return [str(item)[:500] for item in value] if isinstance(value, list) else []


def _repository_record(value: dict[str, Any], *, include_commit: bool) -> dict[str, Any]:
    """Keep only displayable provenance fields from a GitHub API response."""
    allowed = (
        "provider", "repository_url", "full_name", "description", "default_branch",
        "is_fork", "is_archived", "stars", "updated_at", "observed_at", "match_status",
        "evidence_refs",
    )
    result = {key: value[key] for key in allowed if key in value}
    if include_commit:
        result["commit_sha"] = str(value.get("commit_sha") or "")[:64]
    if not str(result.get("repository_url") or "").startswith("https://github.com/"):
        raise ValueError("invalid repository record")
    return result


def _code_source_markdown(source: dict[str, Any]) -> str:
    mode = str(source.get("mode") or "not_checked")
    if mode == "repository":
        repository = source.get("repository") if isinstance(source.get("repository"), dict) else {}
        url = str(repository.get("repository_url") or "")
        full_name = str(repository.get("full_name") or url)
        relationship = {
            "official_confirmed": "用户确认的官方实现",
            "community": "社区实现",
            "user_provided": "用户提供，待核验",
            "candidate_unverified": "检索候选，待核验",
        }.get(str(source.get("relationship") or ""), "待核验")
        commit = str(repository.get("commit_sha") or "")
        lines = [f"- 仓库：[{full_name}]({url})", f"- 来源性质：{relationship}"]
        if commit:
            lines.append(f"- 记录的 commit：`{commit}`")
        lines.append("- 当前仅解析仓库元数据；尚未克隆、安装依赖或执行其中代码。")
        return "\n".join(lines)
    if mode == "method_reconstruction":
        state = str(source.get("status") or "paper_only")
        description = "已生成可编辑的参考实现" if state == "reconstructed" else "已选择论文方法还原路径"
        return (
            f"- 路径：按论文方法还原（{description}）\n"
            "- 依据：本地论文页面级证据与用户确认内容，不含原作者实现。\n"
            "- 本项目不应被表述为原作者代码复现，也不代表已达到论文报告指标。"
        )
    if mode == "repository_search":
        count = len(source.get("candidates") or []) if isinstance(source.get("candidates"), list) else 0
        return f"- 已检索 GitHub 候选：{count} 个\n- 候选不等于官方实现，需由用户核验后连接。"
    return "- 尚未检索或连接公开代码；当前仅基于论文证据生成。"


def _replace_markdown_section(content: str, heading: str, body: str) -> str:
    """Replace one level-two README section while preserving all other content."""
    source = str(content or "# 论文复现实验\n")
    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\n.*?(?=^## |\Z)")
    section = f"## {heading}\n\n{body.strip()}\n\n"
    if pattern.search(source):
        return pattern.sub(section, source).rstrip() + "\n"
    return source.rstrip() + "\n\n" + section


def _mirror_code_source(files: list[dict[str, str]], source: dict[str, Any]) -> list[dict[str, str]]:
    """Expose the selected code source in both editable project entrypoints."""
    file_map = {str(item.get("path") or ""): str(item.get("content") or "") for item in files}
    try:
        config = json.loads(file_map.get("configs/reproduction.json", "{}"))
        config = config if isinstance(config, dict) else {}
    except json.JSONDecodeError:
        config = {}
    config["code_source"] = source
    file_map["configs/reproduction.json"] = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    file_map["README.md"] = _replace_markdown_section(
        file_map.get("README.md", "# 论文复现实验\n"),
        "代码来源",
        _code_source_markdown(source),
    )
    return [{"path": path, "content": content} for path, content in sorted(file_map.items()) if path]


def _method_reconstruction_record(
    value: Any,
    *,
    source_refs: Any,
    unknowns: Any,
) -> dict[str, Any]:
    """Keep the paper-only reconstruction plan small, displayable and auditable."""
    raw = value if isinstance(value, dict) else {}
    raw_components = raw.get("components") if isinstance(raw.get("components"), list) else []
    components: list[dict[str, Any]] = []
    for item in raw_components[:12]:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:160]
        if not name:
            continue
        evidence_ids = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
        components.append({
            "name": name,
            "responsibility": " ".join(str(item.get("responsibility") or "").split())[:500],
            "implementation_hint": " ".join(str(item.get("implementation_hint") or "").split())[:700],
            "evidence_ids": [str(ref)[:120] for ref in evidence_ids[:8] if str(ref).strip()],
        })
    assumptions = _as_string_list(raw.get("assumptions"))[:16]
    unresolved = _as_string_list(unknowns)[:16]
    refs = source_refs if isinstance(source_refs, list) else []
    return {
        "status": str(raw.get("status") or "draft")[:40],
        "basis": "paper_evidence",
        "method_summary": " ".join(str(raw.get("method_summary") or "").split())[:2_000],
        "components": components,
        "assumptions": assumptions,
        "unresolved": unresolved,
        "source_ref_count": len(refs),
    }


def _method_reconstruction_markdown(reconstruction: dict[str, Any]) -> str:
    """Describe what the generated reference implementation does and does not prove."""
    lines = [
        "- 依据：本地论文页面级证据，不包含原作者实现或未公开训练细节。",
        "- 产物：可编辑的参考实现；它用于检验方法理解，不等同于原作者代码复现。",
        "- 执行状态：仅通过静态语法验证，未安装依赖、下载数据、训练或评测。",
    ]
    summary = str(reconstruction.get("method_summary") or "")
    if summary:
        lines.append(f"- 方法概述：{summary}")
    components = reconstruction.get("components") if isinstance(reconstruction.get("components"), list) else []
    if components:
        lines.append("- 方法组件：")
        for component in components:
            if not isinstance(component, dict):
                continue
            name = str(component.get("name") or "未命名组件")
            responsibility = str(component.get("responsibility") or "待补充职责")
            lines.append(f"  - {name}：{responsibility}")
    assumptions = reconstruction.get("assumptions") if isinstance(reconstruction.get("assumptions"), list) else []
    if assumptions:
        lines.append("- 还原假设：")
        lines.extend(f"  - {item}" for item in assumptions)
    unresolved = reconstruction.get("unresolved") if isinstance(reconstruction.get("unresolved"), list) else []
    if unresolved:
        lines.append("- 仍待确认：")
        lines.extend(f"  - {item}" for item in unresolved)
    return "\n".join(lines)


def _mirror_method_reconstruction(
    files: list[dict[str, str]], reconstruction: dict[str, Any],
) -> list[dict[str, str]]:
    """Place the paper-only boundary next to the editable source files."""
    file_map = {str(item.get("path") or ""): str(item.get("content") or "") for item in files}
    try:
        config = json.loads(file_map.get("configs/reproduction.json", "{}"))
        config = config if isinstance(config, dict) else {}
    except json.JSONDecodeError:
        config = {}
    config["method_reconstruction"] = reconstruction
    file_map["configs/reproduction.json"] = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    file_map["README.md"] = _replace_markdown_section(
        file_map.get("README.md", "# 论文方法还原\n"),
        "方法还原边界",
        _method_reconstruction_markdown(reconstruction),
    )
    return [{"path": path, "content": content} for path, content in sorted(file_map.items()) if path]


def _preparation_record(value: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded, displayable checkout manifest rather than source contents."""
    strings = (
        "local_path", "repository_url", "full_name", "default_branch", "commit_sha", "sync_mode",
        "readme_path", "readme_preview", "prepared_at", "status", "error", "attempted_at",
    )
    result = {key: str(value.get(key) or "")[:12_000 if key == "readme_preview" else 500] for key in strings}
    for key in ("file_count_scanned",):
        raw = value.get(key)
        result[key] = max(0, int(raw)) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
    result["scan_truncated"] = bool(value.get("scan_truncated"))
    result["has_submodules"] = bool(value.get("has_submodules"))
    for key, limit in (("dependency_files", 20), ("entrypoint_candidates", 30), ("config_candidates", 30)):
        items = value.get(key) if isinstance(value.get(key), list) else []
        result[key] = [str(item)[:300] for item in items[:limit] if str(item).strip()]
    return result


def _preparation_markdown(prepared: dict[str, Any]) -> str:
    commit = str(prepared.get("commit_sha") or "")
    lines = [
        f"- 上游代码位置：`{prepared.get('local_path') or 'repository/source'}`",
        f"- 固定 commit：`{commit}`" if commit else "- 固定 commit：未记录",
        f"- 依赖声明：{', '.join(prepared.get('dependency_files') or []) or '未找到'}",
        f"- 训练/评测入口候选：{', '.join(prepared.get('entrypoint_candidates') or []) or '未找到'}",
        "- 当前仅完成克隆与静态分析；尚未安装依赖、下载数据或执行任何上游代码。",
    ]
    if prepared.get("status") == "failed":
        lines.append(f"- 最近同步失败：{prepared.get('error') or '未知错误'}")
    if prepared.get("has_submodules"):
        lines.append("- 检测到 Git submodule；当前未初始化，需在运行阶段单独确认。")
    return "\n".join(lines)


def _mirror_repository_preparation(files: list[dict[str, str]], prepared: dict[str, Any]) -> list[dict[str, str]]:
    """Make the static execution plan visible beside the editable project code."""
    file_map = {str(item.get("path") or ""): str(item.get("content") or "") for item in files}
    try:
        config = json.loads(file_map.get("configs/reproduction.json", "{}"))
        config = config if isinstance(config, dict) else {}
    except json.JSONDecodeError:
        config = {}
    config["repository_preparation"] = prepared
    file_map["configs/reproduction.json"] = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    file_map["README.md"] = _replace_markdown_section(
        file_map.get("README.md", "# 论文复现实验\n"),
        "上游仓库准备",
        _preparation_markdown(prepared),
    )
    return [{"path": path, "content": content} for path, content in sorted(file_map.items()) if path]


def _validate_workspace_files(files: list[dict[str, str]]) -> dict[str, Any]:
    """Keep user-confirmation writes under the same no-execution validation rule."""
    import ast

    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    paths = {str(item.get("path") or "") for item in files}
    for required in ("README.md", "configs/reproduction.json", "src/reproduction.py", "tests/test_smoke.py"):
        present = required in paths
        checks.append({"name": f"required:{required}", "passed": present})
        if not present:
            errors.append(f"缺少必需文件：{required}")
    for item in files:
        path = str(item.get("path") or "")
        if path.endswith(".py"):
            try:
                ast.parse(str(item.get("content") or ""), filename=path)
                checks.append({"name": f"syntax:{path}", "passed": True})
            except SyntaxError as exc:
                errors.append(f"Python 语法错误 {path}:{exc.lineno}")
                checks.append({"name": f"syntax:{path}", "passed": False})
    return {"valid": not errors, "checks": checks, "errors": errors}

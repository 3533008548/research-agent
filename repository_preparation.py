"""Prepare a pinned upstream repository for a paper-reproduction project.

This stage intentionally stops before dependency installation, data download or
training.  It gives the user a reproducible source snapshot and a compact,
inspectable execution plan before any third-party code is allowed to run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cancellation import RequestCancelledError
from experiment_projects import ExperimentProjectStore


ProgressCallback = Callable[[dict[str, str]], None]
_README_NAMES = {"readme", "readme.md", "readme.rst", "readme.txt"}
_DEPENDENCY_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "environment.yml",
    "environment.yaml", "pipfile", "poetry.lock", "conda.yaml", "conda.yml", "dockerfile",
}
_ENTRY_NAMES = {"train.py", "main.py", "run.py", "evaluate.py", "eval.py", "test.py"}
_MAX_FILES_TO_SCAN = 2_000


class RepositoryPreparationError(RuntimeError):
    """The selected upstream source could not be prepared reproducibly."""


@dataclass(frozen=True)
class RepositoryPreparationResult:
    status: str
    summary: str
    metrics: dict[str, int | float]
    error_type: str = ""


def _emit(callback: ProgressCallback | None, stage: str, status: str = "running") -> None:
    if callback:
        callback({"stage": stage, "status": status})


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelledError("repository preparation cancelled")


def _compact_output(output: str) -> str:
    return " ".join(str(output or "").split())[-500:]


def _run_git(args: list[str], *, cwd: Path, cancel_event, timeout_seconds: int = 120) -> str:
    command = ["git", *args]
    environment = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except OSError as exc:
        raise RepositoryPreparationError("运行环境未安装 git，无法同步上游仓库") from exc
    deadline = time.monotonic() + max(10, int(timeout_seconds))
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                process.wait(timeout=5)
                raise RequestCancelledError("repository preparation cancelled")
            if time.monotonic() >= deadline:
                process.terminate()
                process.wait(timeout=5)
                raise RepositoryPreparationError("同步上游仓库超时")
            time.sleep(0.15)
        output = process.communicate()[0] or ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    if process.returncode != 0:
        detail = _compact_output(output)
        raise RepositoryPreparationError(f"git {' '.join(args[:2])} 失败" + (f"：{detail}" if detail else ""))
    return output


def _read_preview(path: Path, *, max_bytes: int = 12_000) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def analyze_repository(source_dir: Path) -> dict[str, Any]:
    """Build a bounded, no-execution source inventory for the project page."""
    readmes: list[Path] = []
    dependencies: list[str] = []
    entrypoints: list[str] = []
    configs: list[str] = []
    submodules = (source_dir / ".gitmodules").is_file()
    count = 0
    for path in source_dir.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        count += 1
        if count > _MAX_FILES_TO_SCAN:
            break
        relative = path.relative_to(source_dir).as_posix()
        name = path.name.casefold()
        if name in _README_NAMES:
            readmes.append(path)
        if name in _DEPENDENCY_NAMES or (name.startswith("requirements") and name.endswith(".txt")):
            dependencies.append(relative)
        if name in _ENTRY_NAMES or (relative.startswith("scripts/") and path.suffix in {".py", ".sh"}):
            entrypoints.append(relative)
        if path.suffix.casefold() in {".yaml", ".yml", ".json"} and (
            "config" in relative.casefold() or "option" in relative.casefold()
        ):
            configs.append(relative)
    readmes.sort(key=lambda item: (len(item.parts), item.name.casefold()))
    primary_readme = readmes[0] if readmes else None
    return {
        "local_path": "repository/source",
        "file_count_scanned": min(count, _MAX_FILES_TO_SCAN),
        "scan_truncated": count > _MAX_FILES_TO_SCAN,
        "readme_path": primary_readme.relative_to(source_dir).as_posix() if primary_readme else "",
        "readme_preview": _read_preview(primary_readme) if primary_readme else "",
        "dependency_files": dependencies[:20],
        "entrypoint_candidates": entrypoints[:30],
        "config_candidates": configs[:30],
        "has_submodules": submodules,
        "prepared_at": ExperimentProjectStore._now(),
    }


class RepositoryPreparationOrchestrator:
    """Clone the user-selected commit and persist its static execution inventory."""

    def __init__(self, *, project_store: ExperimentProjectStore) -> None:
        self.project_store = project_store

    def prepare(
        self,
        project_id: str,
        *,
        run_id: str,
        cancel_event=None,
        on_progress: ProgressCallback | None = None,
    ) -> RepositoryPreparationResult:
        started = time.perf_counter()
        project = self.project_store.read(project_id)
        if project is None:
            raise KeyError(project_id)
        source = project.get("spec", {}).get("code_source") if isinstance(project.get("spec"), dict) else {}
        source = source if isinstance(source, dict) else {}
        repository = source.get("repository") if isinstance(source.get("repository"), dict) else {}
        repository_url = str(repository.get("repository_url") or "").strip()
        expected_sha = str(repository.get("commit_sha") or "").strip()
        if str(source.get("mode") or "") != "repository" or not repository_url or not expected_sha:
            raise RepositoryPreparationError("请先在项目页连接一个已固定 commit 的 GitHub 仓库")

        target = self.project_store.repository_source_dir(project_id)
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        _check_cancel(cancel_event)
        _emit(on_progress, "repository_clone")
        reused = target.is_dir() and self._prepared_manifest_matches(project_id, expected_sha)
        if not reused:
            self._clone_pinned_repository(
                target=target,
                repository_url=repository_url,
                expected_sha=expected_sha,
                cancel_event=cancel_event,
            )
        _check_cancel(cancel_event)
        _emit(on_progress, "repository_analysis")
        manifest = analyze_repository(target)
        manifest.update({
            "repository_url": repository_url,
            "full_name": str(repository.get("full_name") or ""),
            "default_branch": str(repository.get("default_branch") or ""),
            "commit_sha": expected_sha,
            "sync_mode": "reused" if reused else "cloned",
        })
        _check_cancel(cancel_event)
        _emit(on_progress, "execution_plan")
        summary = "已固定并分析上游仓库；下一步请核对依赖、数据集与训练入口后再显式执行。"
        self.project_store.record_repository_preparation(project_id, run_id=run_id, manifest=manifest, summary=summary)
        _emit(on_progress, "completed", "completed")
        return RepositoryPreparationResult(
            status="completed",
            summary=summary,
            metrics={
                "duration_ms": round((time.perf_counter() - started) * 1_000, 1),
                "repository_files": int(manifest["file_count_scanned"]),
                "dependency_files": len(manifest["dependency_files"]),
                "entrypoint_candidates": len(manifest["entrypoint_candidates"]),
            },
        )

    def _prepared_manifest_matches(self, project_id: str, expected_sha: str) -> bool:
        project = self.project_store.read(project_id) or {}
        spec = project.get("spec") if isinstance(project.get("spec"), dict) else {}
        manifest = spec.get("repository_preparation") if isinstance(spec.get("repository_preparation"), dict) else {}
        return str(manifest.get("commit_sha") or "") == expected_sha

    @staticmethod
    def _clone_pinned_repository(
        *,
        target: Path,
        repository_url: str,
        expected_sha: str,
        cancel_event,
    ) -> None:
        parent = target.parent
        staging = parent / f".source-{uuid.uuid4().hex[:10]}"
        previous = parent / f".source-old-{uuid.uuid4().hex[:10]}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            _run_git(["init", "--quiet"], cwd=staging, cancel_event=cancel_event)
            _run_git(["remote", "add", "origin", repository_url], cwd=staging, cancel_event=cancel_event)
            _run_git(["fetch", "--depth", "1", "origin", expected_sha], cwd=staging, cancel_event=cancel_event)
            _run_git(["checkout", "--detach", "--quiet", "FETCH_HEAD"], cwd=staging, cancel_event=cancel_event)
            actual_sha = _run_git(["rev-parse", "HEAD"], cwd=staging, cancel_event=cancel_event).strip()
            if actual_sha != expected_sha:
                raise RepositoryPreparationError("上游仓库检出的 commit 与项目记录不一致，已停止")
            if target.exists():
                os.replace(target, previous)
            os.replace(staging, target)
            if previous.exists():
                shutil.rmtree(previous)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if previous.exists() and not target.exists():
                os.replace(previous, target)
            raise

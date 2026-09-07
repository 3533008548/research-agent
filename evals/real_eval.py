"""Isolated runner for a small, real-model evaluation sample.

The deterministic benchmark and runtime replay suites deliberately do not call
external models.  This module is for the separate case where an operator wants
to collect a small, auditable sample from a real model without touching the
web application's runtime database.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.benchmark import ROOT, load_manifest, score_submission
from evals.capture import result_from_trace


PROJECT_ROOT = ROOT.parent
DEFAULT_REPORTS_DIR = ROOT / "reports"
_SECRET_ENV_NAMES = ("DEEPSEEK_API_KEY", "OPENALEX_API_KEY", "GLM_API_KEY")


def select_tasks(task_ids: list[str]) -> list[dict[str, Any]]:
    """Return requested manifest tasks and reject missing or duplicate ids."""
    manifest = load_manifest()
    available = {str(task["id"]): task for task in manifest["tasks"]}
    requested = [task_id.strip().upper() for task_id in task_ids if task_id.strip()]
    if not requested:
        raise ValueError("至少指定一个 --task，例如 --task T04")
    duplicates = sorted({task_id for task_id in requested if requested.count(task_id) > 1})
    if duplicates:
        raise ValueError(f"任务重复: {', '.join(duplicates)}")
    unknown = [task_id for task_id in requested if task_id not in available]
    if unknown:
        raise ValueError(f"未知评测任务: {', '.join(unknown)}")
    return [available[task_id] for task_id in requested]


def is_primary_runtime(path: str | Path) -> bool:
    """Whether a data directory is the application's mounted user runtime."""
    candidate = Path(path).expanduser().resolve()
    primary = (PROJECT_ROOT / "runtime").resolve()
    container_runtime = str(path).replace("\\", "/").rstrip("/") == "/app/runtime"
    return candidate == primary or primary in candidate.parents or container_runtime


def resolve_runtime(
    data_dir: str | None,
    *,
    keep_runtime: bool,
    allow_production_runtime: bool,
) -> tuple[Path, bool]:
    """Choose an isolated data root and report whether this call owns it."""
    if data_dir:
        if not keep_runtime:
            raise ValueError("指定 --data-dir 时必须同时传入 --keep-runtime，避免意外保留评测现场")
        root = Path(data_dir).expanduser().resolve()
        if is_primary_runtime(root) and not allow_production_runtime:
            raise ValueError(
                "拒绝使用主 runtime 运行真实评测；请省略 --data-dir 使用临时目录，"
                "或同时传入 --keep-runtime --allow-production-runtime。"
            )
        return root, False
    return Path(tempfile.mkdtemp(prefix="research-agent-real-eval-")), True


def redact_known_secrets(value: Any, secrets: tuple[str, ...]) -> Any:
    """Defence in depth for report data; execution traces are already sanitized."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact_known_secrets(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_known_secrets(item, secrets) for key, item in value.items()}
    return value


def _state_from_research_run(agent, session_id: str) -> dict[str, Any]:
    run = agent.sessions.get_latest_research_run(session_id)
    if not run:
        return {"research_status": "missing", "evidence_persisted": False}
    evidence = run.get("evidence")
    return {
        "research_status": str(run.get("status") or "unknown"),
        "evidence_persisted": isinstance(evidence, list) and bool(evidence),
    }


def _run_task(agent, task: dict[str, Any], *, research_scope: str) -> dict[str, Any]:
    """Execute one manifest prompt in the isolated runtime and capture its trace."""
    task_id = str(task["id"])
    session = agent.create_session(f"评测 {task_id}")
    session_id = str(session["thread_id"])
    prompt = str(task["prompt"])
    state: dict[str, Any] = {"isolated_runtime": True}

    if task_id == "T11":
        answer = agent.research(prompt, scope=research_scope, session_id=session_id)
        state.update(_state_from_research_run(agent, session_id))
    else:
        answer = agent.step(prompt, session_id=session_id)

    captured = result_from_trace(
        task_id,
        answer,
        agent.get_last_trace(session_id),
        state=state,
    )
    if task_id == "T13":
        # The provider is declared by the evaluated fixture. Key exposure is
        # checked against the captured answer and de-identified trace before
        # the report receives its final defence-in-depth redaction pass.
        key = str(agent.cfg.openalex_api_key or "")
        captured["state"].update({
            "provider": "openalex",
            "api_key_exposed": not key or key not in str(captured),
        })
    return captured


def write_real_report(report: dict[str, Any], reports_dir: str | Path) -> Path:
    """Persist the already-redacted result outside the application runtime."""
    directory = Path(reports_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("real-eval-%Y%m%dT%H%M%SZ.json")
    path = directory / filename
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_real_evaluation(
    task_ids: list[str],
    *,
    data_dir: str | None = None,
    keep_runtime: bool = False,
    allow_production_runtime: bool = False,
    with_rag: bool = False,
    research_scope: str = "public",
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run selected tasks in an isolated runtime and return a redacted report."""
    tasks = select_tasks(task_ids)
    runtime_root, owns_runtime = resolve_runtime(
        data_dir,
        keep_runtime=keep_runtime,
        allow_production_runtime=allow_production_runtime,
    )
    if dry_run:
        if owns_runtime:
            # ``mkdtemp`` is only used to show the exact disposal policy; no
            # application state is initialized during a dry run.
            shutil.rmtree(runtime_root, ignore_errors=True)
        return {
            "dry_run": True,
            "task_ids": [task["id"] for task in tasks],
            "runtime": "temporary" if owns_runtime else "operator-specified",
            "keep_runtime": keep_runtime,
            "rag_enabled": with_rag,
            "research_scope": research_scope,
        }

    original_data_dir = os.environ.get("APP_DATA_DIR")
    agent = None
    try:
        # Config also synchronizes APP_DATA_DIR for tools that do not receive a
        # Config instance. This must happen before the agent opens any store.
        from config import Config
        from research_agent import ResearchAgent

        config = Config.load({"data_dir": str(runtime_root), "rag_enabled": with_rag})
        agent = ResearchAgent(config)
        results = [_run_task(agent, task, research_scope=research_scope) for task in tasks]
        selected_manifest = {"version": load_manifest().get("version"), "tasks": tasks}
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "kind": "small-sample-manual-real-run-evaluation",
            "benchmark_version": selected_manifest["version"],
            "selection": {"task_ids": [task["id"] for task in tasks], "task_count": len(tasks)},
            "execution": {
                "uses_real_model": True,
                "model": agent.model,
                "isolated_temporary_runtime": owns_runtime and not keep_runtime,
                "rag_enabled": with_rag,
                "research_scope": research_scope,
                "trace_payload_policy": "final answer plus sanitized trace; no prompt, runtime DB, or key",
            },
            "results": results,
            "score": score_submission(selected_manifest, results),
            "limitations": [
                "This is a selected real-run sample, not a full online benchmark.",
                "State assertions are recorded only when observed from the isolated run.",
                "Manual rubrics still require human review.",
            ],
        }
        secrets = tuple(os.getenv(name, "") for name in _SECRET_ENV_NAMES)
        report = redact_known_secrets(report, secrets)
        report_path = write_real_report(report, reports_dir)
        return {
            "report_path": str(report_path),
            "task_ids": report["selection"]["task_ids"],
            "passed": report["score"]["passed"],
            "total": report["score"]["total"],
            "runtime_retained": keep_runtime or not owns_runtime,
            "runtime_root": str(runtime_root) if keep_runtime or not owns_runtime else None,
        }
    finally:
        if agent is not None:
            for store_name in ("sessions", "memory"):
                store = getattr(agent, store_name, None)
                close = getattr(store, "close", None)
                if callable(close):
                    close()
        if original_data_dir is None:
            os.environ.pop("APP_DATA_DIR", None)
        else:
            os.environ["APP_DATA_DIR"] = original_data_dir
        if owns_runtime and not keep_runtime:
            shutil.rmtree(runtime_root, ignore_errors=True)

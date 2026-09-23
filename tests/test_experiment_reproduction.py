from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.redis_runs import (
    QueuedExperimentRun,
    RedisExperimentRunManager,
    RedisExperimentRunWorker,
)
from experiment_projects import ExperimentProjectStore
from experiment_reproduction import ExperimentOrchestrator, ReproductionResult
from paper_artifacts import write_document_map
from runtime_paths import RuntimePaths


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return {"choices": [{"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}]}

    def close(self) -> None:
        return None


class _FakeLLM:
    def __init__(self) -> None:
        self.responses = [
            {
                "reproduction_level": "approximate",
                "parameters": [{"name": "epochs", "value": "100", "provenance": "paper"}],
                "datasets": [{"name": "ToySet", "split": "paper-defined"}],
                "unknowns": ["随机种子未在已解析证据中找到。"],
                "acceptance_criteria": ["先验证配置可加载。"],
                "implementation_notes": ["保持数据接口可替换。"],
            },
            {
                "files": [
                    {"path": "README.md", "content": "# Test reproduction\n"},
                    {"path": "requirements.txt", "content": "\n"},
                    {"path": "configs/reproduction.json", "content": "{}\n"},
                    {"path": "src/reproduction.py", "content": "def build_experiment_config():\n    return {'status': 'ok'}\n"},
                    {"path": "tests/test_smoke.py", "content": "from src.reproduction import build_experiment_config\nassert build_experiment_config()['status'] == 'ok'\n"},
                ]
            },
        ]

    def post(self, _payload, *, policy, cancel_event):
        del policy, cancel_event
        return _Response(self.responses.pop(0))


class _MethodFakeLLM:
    def __init__(self) -> None:
        self.responses = [
            {
                "method_summary": "先以编码器提取表征，再由调度模块生成动作。",
                "components": [
                    {
                        "name": "编码器",
                        "responsibility": "从输入序列提取表征。",
                        "implementation_hint": "保留可替换的特征提取接口。",
                        "evidence_ids": ["E1", "不存在的证据"],
                    },
                ],
                "assumptions": ["论文未给出层宽时，需由用户确认。"],
                "parameters": [{"name": "epochs", "value": "100", "provenance": "paper"}],
                "datasets": [{"name": "ToySet", "split": "paper-defined"}],
                "unknowns": ["随机种子未在已解析证据中找到。"],
                "acceptance_criteria": ["先通过最小静态检查。"],
                "implementation_notes": ["不得将参考实现称为原作者代码。"],
            },
            {
                "files": [
                    {"path": "README.md", "content": "# Paper-only reconstruction\n"},
                    {"path": "requirements.txt", "content": "\n"},
                    {"path": "configs/reproduction.json", "content": "{}\n"},
                    {
                        "path": "src/method.py",
                        "content": "def build_method_plan():\n    return {'implementation_basis': 'paper_evidence'}\n",
                    },
                    {
                        "path": "src/reproduction.py",
                        "content": "from src.method import build_method_plan\n\ndef build_experiment_config():\n    return build_method_plan()\n",
                    },
                    {
                        "path": "tests/test_smoke.py",
                        "content": "from src.method import build_method_plan\nassert build_method_plan()['implementation_basis'] == 'paper_evidence'\n",
                    },
                ],
            },
        ]

    def post(self, _payload, *, policy, cancel_event):
        del policy, cancel_event
        return _Response(self.responses.pop(0))


class ExperimentReproductionTests(unittest.TestCase):
    def _paths(self) -> RuntimePaths:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        paths = RuntimePaths.from_root(directory.name)
        paths.ensure_initialized()
        return paths

    def test_reproduction_persists_auditable_project_without_running_code(self) -> None:
        paths = self._paths()
        paper = {"paper_id": "paper-123", "title": "Evidence-backed test paper", "indexed_at": "now"}
        write_document_map(paths, paper_id="paper-123", document_map={
            "paper_id": "paper-123", "title": paper["title"], "total_pages": 1, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [
                {"id": "E1", "kind": "text", "page": 1, "section": "Method", "text": "Train for 100 epochs."},
                {"id": "E2", "kind": "table", "page": 1, "section": "Evaluation", "text": "Evaluate with accuracy."},
            ]}],
        })
        store = ExperimentProjectStore(paths)
        project = store.create(paper)
        result = ExperimentOrchestrator(
            paper_store=None, project_store=store, llm_client=_FakeLLM(), model="fake", paths=paths,
        ).reproduce(project["project_id"], run_id="experiment-test123")

        self.assertEqual(result.status, "completed")
        saved = store.read(project["project_id"])
        self.assertIsNotNone(saved)
        self.assertTrue(saved["validation"]["valid"])
        self.assertEqual(saved["reproduction_level"], "approximate")
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(store.read_file(project["project_id"], "src/reproduction.py")["path"], "src/reproduction.py")
        self.assertFalse((paths.experiment_projects_dir / project["project_id"] / "workspace" / ".env").exists())

    def test_missing_page_evidence_marks_project_blocked(self) -> None:
        paths = self._paths()
        store = ExperimentProjectStore(paths)
        project = store.create({"paper_id": "paper-none", "title": "No evidence"})
        result = ExperimentOrchestrator(
            paper_store=None, project_store=store, llm_client=None, model="", paths=paths,
        ).reproduce(project["project_id"], run_id="experiment-missing")

        self.assertEqual(result.status, "partial_failed")
        saved = store.read(project["project_id"])
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["spec"]["reproduction_level"], "blocked")

    def test_method_reconstruction_is_explicitly_paper_only_and_keeps_evidence_links(self) -> None:
        paths = self._paths()
        paper = {"paper_id": "paper-method", "title": "Method-only test paper", "indexed_at": "now"}
        write_document_map(paths, paper_id=paper["paper_id"], document_map={
            "paper_id": paper["paper_id"], "title": paper["title"], "total_pages": 1, "processed_pages": 1,
            "pages": [{"page": 1, "elements": [
                {"id": "E1", "kind": "text", "page": 1, "section": "Method", "text": "Encode a sequence then schedule actions."},
            ]}],
        })
        store = ExperimentProjectStore(paths)
        project = store.create(paper)

        result = ExperimentOrchestrator(
            paper_store=None, project_store=store, llm_client=_MethodFakeLLM(), model="fake", paths=paths,
        ).reconstruct_method(project["project_id"], run_id="experiment-method")

        self.assertEqual(result.status, "completed")
        saved = store.read(project["project_id"])
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "needs_review")
        self.assertEqual(saved["implementation_path"], "method_reconstruction")
        self.assertEqual(saved["reproduction_level"], "approximate")
        self.assertEqual(saved["spec"]["code_source"]["mode"], "method_reconstruction")
        reconstruction = saved["spec"]["method_reconstruction"]
        self.assertEqual(reconstruction["basis"], "paper_evidence")
        self.assertEqual(reconstruction["components"][0]["evidence_ids"], ["E1"])
        self.assertIn("论文未给出层宽", reconstruction["assumptions"][0])
        self.assertIn("方法还原边界", store.read_file(project["project_id"], "README.md")["content"])
        self.assertIn("method_reconstruction", store.read_file(project["project_id"], "configs/reproduction.json")["content"])
        self.assertIsNotNone(store.read_file(project["project_id"], "src/method.py"))

    def test_confirmation_updates_spec_config_and_preserves_project_files(self) -> None:
        paths = self._paths()
        store = ExperimentProjectStore(paths)
        project = store.create({"paper_id": "paper-confirm", "title": "Confirmation test"})
        store.record_result(
            project["project_id"],
            run_id="experiment-seed",
            spec={
                "schema_version": 1,
                "project_id": project["project_id"],
                "paper": {"paper_id": "paper-confirm", "title": "Confirmation test"},
                "reproduction_level": "approximate",
                "source_refs": [], "parameters": [], "datasets": [],
                "unknowns": ["随机种子未在已解析证据中找到。"],
                "acceptance_criteria": [], "implementation_notes": [],
            },
            files=[
                {"path": "README.md", "content": "# Confirmation test\n"},
                {"path": "requirements.txt", "content": "\n"},
                {"path": "configs/reproduction.json", "content": "{}\n"},
                {"path": "src/reproduction.py", "content": "def build_experiment_config():\n    return {}\n"},
                {"path": "tests/test_smoke.py", "content": "import unittest\n"},
            ],
            validation={"valid": True, "checks": [], "errors": []},
            summary="seed", status="needs_review",
        )
        updated = store.apply_confirmation(
            project["project_id"],
            confirmed_items=["随机种子固定为 42。"],
            resolved_unknowns=["随机种子未在已解析证据中找到。"],
        )

        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["spec"]["unknowns"], [])
        self.assertEqual(updated["spec"]["confirmed_items"][0]["provenance"], "user_confirmation")
        config = store.read_file(project["project_id"], "configs/reproduction.json")
        self.assertIn("随机种子固定为 42", config["content"])
        readme = store.read_file(project["project_id"], "README.md")
        self.assertIn("用户已确认的实验前提", readme["content"])

        connected = store.connect_repository(
            project["project_id"],
            repository={
                "provider": "github",
                "repository_url": "https://github.com/example/paper-code",
                "full_name": "example/paper-code",
                "default_branch": "main",
                "commit_sha": "f" * 40,
                "observed_at": "2026-09-08T00:00:00+00:00",
            },
            relationship="candidate_unverified",
        )
        self.assertEqual(connected["spec"]["code_source"]["relationship"], "candidate_unverified")
        config = store.read_file(project["project_id"], "configs/reproduction.json")
        self.assertIn("paper-code", config["content"])
        readme = store.read_file(project["project_id"], "README.md")
        self.assertIn("代码来源", readme["content"])

        prepared = store.record_repository_preparation(
            project["project_id"],
            run_id="experiment-prepare",
            manifest={
                "repository_url": "https://github.com/example/paper-code",
                "commit_sha": "f" * 40,
                "local_path": "repository/source",
                "dependency_files": ["requirements.txt"],
                "entrypoint_candidates": ["train.py"],
                "config_candidates": ["configs/base.yaml"],
                "file_count_scanned": 4,
                "prepared_at": "2026-09-09T00:00:00+00:00",
            },
            summary="prepared",
        )
        self.assertEqual(prepared["spec"]["repository_preparation"]["local_path"], "repository/source")
        config = store.read_file(project["project_id"], "configs/reproduction.json")
        self.assertIn("repository_preparation", config["content"])
        readme = store.read_file(project["project_id"], "README.md")
        self.assertIn("上游仓库准备", readme["content"])

    def test_method_reconstruction_uses_a_distinct_durable_action(self) -> None:
        paths = self._paths()
        store = ExperimentProjectStore(paths)
        project = store.create({"paper_id": "paper-action", "title": "Action test"})
        run = {
            "run_id": "experiment-action", "thread_id": "session-action",
            "project_id": project["project_id"], "status": "queued",
        }
        sessions = MagicMock()
        sessions.get.return_value = {"thread_id": "session-action"}
        sessions.create_experiment_run.return_value = run
        sessions.get_experiment_run.return_value = run
        agent = SimpleNamespace(sessions=sessions, model="fake")
        broker = MagicMock()
        manager = RedisExperimentRunManager(agent, store, broker)

        started = manager.start(
            "session-action", project_id=project["project_id"], action="reconstruct_method",
        )

        self.assertEqual(started["run_id"], "experiment-action")
        sessions.create_experiment_run.assert_called_once_with(
            "session-action", project["project_id"], action="reconstruct_method", status="queued",
        )
        broker.enqueue.assert_called_once_with(
            "experiment-action", "session-action", project["project_id"], action="reconstruct_method",
        )
        self.assertEqual(store.read(project["project_id"])["status"], "queued")

    def test_experiment_worker_dispatches_method_reconstruction_without_running_code(self) -> None:
        sessions = MagicMock()
        sessions.get_experiment_run.return_value = {
            "run_id": "experiment-worker", "thread_id": "session-worker",
            "project_id": "experiment-abcdef123456", "status": "queued",
        }
        broker = MagicMock()
        broker.cancel_requested.return_value = False
        result = ReproductionResult(
            status="completed", summary="paper-only draft", metrics={"file_count": 6}, error_type="",
        )
        orchestrator = MagicMock()
        orchestrator.reconstruct_method.return_value = result
        worker = RedisExperimentRunWorker(orchestrator, sessions, broker, consumer="test-worker")

        worker._process(QueuedExperimentRun(
            "job-1", "experiment-worker", "session-worker", "experiment-abcdef123456", "reconstruct_method",
        ))

        orchestrator.reconstruct_method.assert_called_once()
        orchestrator.reproduce.assert_not_called()
        self.assertTrue(any(
            call.kwargs.get("status") == "completed"
            for call in sessions.update_experiment_run.call_args_list
        ))
        self.assertIn(
            {"type": "done", "status": "completed", "answer": "paper-only draft"},
            [call.args[1] for call in broker.publish.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()

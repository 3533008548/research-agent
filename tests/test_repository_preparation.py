from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess

from repository_preparation import RepositoryPreparationOrchestrator, analyze_repository


class RepositoryPreparationTests(unittest.TestCase):
    def test_static_inventory_finds_readme_dependencies_and_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "README.md").write_text("# Upstream\n", encoding="utf-8")
            (source / "requirements.txt").write_text("torch\n", encoding="utf-8")
            (source / "train.py").write_text("print('not executed')\n", encoding="utf-8")
            (source / "configs").mkdir()
            (source / "configs" / "base.yaml").write_text("epochs: 1\n", encoding="utf-8")
            (source / ".git").mkdir()
            (source / ".git" / "ignored.py").write_text("bad syntax", encoding="utf-8")

            manifest = analyze_repository(source)

        self.assertEqual(manifest["readme_path"], "README.md")
        self.assertEqual(manifest["dependency_files"], ["requirements.txt"])
        self.assertEqual(manifest["entrypoint_candidates"], ["train.py"])
        self.assertEqual(manifest["config_candidates"], ["configs/base.yaml"])
        self.assertNotIn(".git/ignored.py", manifest["entrypoint_candidates"])

    def test_pinned_clone_checks_out_the_requested_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()
            def git(*args: str) -> str:
                result = subprocess.run(["git", *args], cwd=origin, check=True, capture_output=True, text=True)
                return result.stdout.strip()

            git("init", "--quiet")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (origin / "README.md").write_text("# Snapshot\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "--quiet", "-m", "snapshot")
            commit = git("rev-parse", "HEAD")
            target = root / "project" / "repository" / "source"
            target.parent.mkdir(parents=True)

            RepositoryPreparationOrchestrator._clone_pinned_repository(
                target=target,
                repository_url=str(origin),
                expected_sha=commit,
                cancel_event=None,
            )

            checked_out = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=target, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(checked_out, commit)


if __name__ == "__main__":
    unittest.main()

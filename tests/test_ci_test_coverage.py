"""Guard: every test module must actually run in CI.

Why this exists
---------------
The CI workflow listed its test modules explicitly::

    python -m unittest tests.test_core tests.test_chunking ...

Eight modules were simply never listed.  One of them
(``tests/test_research_evidence.py``) contained two broken tests that called
an instance method unbound — ``ResearchOrchestrator._worker_specs("public")``
passes the scope string in the ``self`` slot.  Those tests had been failing
for weeks without anyone noticing, because nothing ran them.

The failure mode is worse than a red build: a module that is missing from the
list does not fail, it *silently does not exist*.  A green CI badge then
certifies less than it appears to.

This guard compares the modules on disk against the ones named in the workflow
file, so adding a test file without wiring it into CI fails the build.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"

# Modules that need a live service, a browser, or a real model, so they
# cannot join the offline unit-test job.  Each one must still be run by
# *some* CI job; this guard checks the offline job only.
EXCLUDED_FROM_OFFLINE = {
    # Needs Chromium plus a mounted FastAPI server; it has its own CI job.
    "test_mounted_api_e2e",
}


def _test_modules_on_disk() -> set[str]:
    return {
        path.stem
        for path in TESTS_DIR.glob("test_*.py")
        if path.stem not in EXCLUDED_FROM_OFFLINE
    }


def _modules_named_in_workflow() -> set[str]:
    """Return the test modules named by the offline unit-test step.

    The workflow uses a YAML folded block, so the command spans several lines::

        run: >-
          python -m unittest tests.test_core tests.test_chunking
          tests.test_lexical ... -v

    Parsing stops at the next step rather than at the first newline, or the
    scan would only ever see the first line of the command.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "Run offline unit tests"
    start = text.find(marker)
    if start < 0:
        return set()
    rest = text[start:]
    # The step ends at the next step in the job.
    end = rest.find("\n      - name:")
    block = rest if end < 0 else rest[:end]
    return set(re.findall(r"\btests\.[a-z_0-9]+\b", block))


class TestCiCoversEveryTestModule(unittest.TestCase):
    def test_workflow_exists(self):
        self.assertTrue(WORKFLOW.exists(), "CI workflow 文件缺失")

    def test_every_test_module_is_named_in_the_offline_job(self):
        on_disk = _test_modules_on_disk()
        named = _modules_named_in_workflow()
        self.assertTrue(named, "无法从 CI 配置里解析出测试模块列表")

        missing = sorted(
            on_disk - {name.removeprefix("tests.") for name in named}
        )
        self.assertEqual(
            missing, [],
            "这些测试模块从未在 CI 跑过："
            f"{missing}。把它们加进 .github/workflows/test.yml 的 offline job。",
        )

    def test_the_offline_job_names_no_module_that_does_not_exist(self):
        on_disk = _test_modules_on_disk() | EXCLUDED_FROM_OFFLINE
        named = {name.removeprefix("tests.") for name in _modules_named_in_workflow()}
        unknown = sorted(named - on_disk)
        self.assertEqual(
            unknown, [],
            f"CI 引用了不存在的测试模块：{unknown}",
        )


if __name__ == "__main__":
    unittest.main()

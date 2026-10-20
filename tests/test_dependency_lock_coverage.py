"""Guard: every third-party import on the shipped surface must be in the lock.

Why this exists
---------------
The MCP server imported ``mcp`` at module level and shipped.  The package was
never added to ``requirements.txt``, so the lock file did not pin it, and CI
failed at ``import mcp`` — while every local machine was green, because the
author's environment happened to have it installed.

That failure mode is worth a permanent guard rather than a memory: a stale or
incomplete lock file silently produces a *different* build on CI than the one
the developer tested.  ``scripts/check_lock_consistency.py`` already checks that
``requirements.txt`` and the lock agree with each other; this checks that the
**code** and the lock agree, which is the gap it leaves.

The check is deliberately about *availability*, not about declaration.  A
transitive dependency such as ``pydantic`` (pulled in by FastAPI) is perfectly
fine even though no requirement names it.  What must not happen is an import
that no pinned package can satisfy.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Packages that live in this repository rather than coming from PyPI.
LOCAL_MODULES = {path.stem for path in ROOT.glob("*.py")} | {
    "api", "tools", "papers", "evals",
}

# The modules whose top-level imports constitute the shipped runtime surface.
# One-off analysis scripts under the repository root are deliberately excluded:
# they are not installed into the image and are not part of the product.
SHIPPED_SURFACE = (
    "api_server.py",
    "api_worker.py",
    "research_agent.py",
    "mcp_server.py",
    "daily_orchestrator.py",
    "graph_builder.py",
    "tool_runtime.py",
    "tool_catalog.py",
    "paper_store.py",
    "llm_client.py",
)

PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)")


def _normalize(name: str) -> str:
    """PEP 503 normalisation, so ``Foo.Bar`` and ``foo-bar`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_pins() -> set[str]:
    pins: set[str] = set()
    for raw in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(raw.strip())
        if match:
            pins.add(_normalize(match.group(1)))
    return pins


def _shipped_files() -> list[Path]:
    files: list[Path] = []
    for package in ("api", "tools", "papers"):
        files.extend((ROOT / package).rglob("*.py"))
    for name in SHIPPED_SURFACE:
        path = ROOT / name
        if path.exists():
            files.append(path)
    return files


class _ImportCollector(ast.NodeVisitor):
    """Collect required imports, skipping the ones guarded by ``try``.

    Optional imports — the ones inside a ``try`` — are excluded because the
    code degrades when the package is missing, so they are not part of the
    deployment contract.

    Imports nested inside functions count.  That matters here: the MCP server
    imports the SDK inside ``build_server`` on purpose, to keep module import
    cheap, and a module-level-only scan would have missed exactly the
    dependency this file exists to catch.
    """

    def __init__(self) -> None:
        self.required: set[str] = set()
        self._optional_depth = 0

    def visit_Try(self, node: ast.Try) -> None:
        # The body is optional; the handlers and finally are not.
        self._optional_depth += 1
        for statement in node.body:
            self.visit(statement)
        self._optional_depth -= 1
        for handler in node.handlers:
            self.visit(handler)
        for statement in node.orelse:
            self.visit(statement)
        for statement in node.finalbody:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        if not self._optional_depth:
            self.required.update(alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._optional_depth or node.level != 0 or not node.module:
            return
        self.required.add(node.module.split(".")[0])


def _required_imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.required


class LockCoverageTests(unittest.TestCase):
    def test_every_shipped_import_is_pinned_in_the_lock(self):
        pins = _lock_pins()
        stdlib = set(sys.stdlib_module_names)
        missing: dict[str, list[str]] = {}
        for path in _shipped_files():
            for name in _required_imports(path):
                if name in stdlib or name in LOCAL_MODULES:
                    continue
                if _normalize(name) not in pins:
                    missing.setdefault(name, []).append(path.name)
        self.assertEqual(
            missing, {},
            "以下顶层导入在锁文件里没有被固定，CI 会因缺少依赖而失败："
            f"{missing}。请把它们加入 requirements.txt 并重新生成锁文件。",
        )

    def test_the_mcp_sdk_is_pinned(self):
        # The specific regression this file exists for.
        pins = _lock_pins()
        self.assertIn("mcp", pins, "mcp SDK 未被锁文件固定")

    def test_mcp_server_declares_the_sdk_it_imports(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        declared = {
            _normalize(line.split("#")[0].split(">=")[0].split("==")[0].split("[")[0].strip())
            for line in requirements.splitlines()
            if line.split("#")[0].strip()
        }
        self.assertIn("mcp", declared)


if __name__ == "__main__":
    unittest.main()
"""The external tool must be reachable the way the agent reaches it.

``test_mcp_client.py`` proves the boundary and ``test_mcp_client_e2e.py``
proves the transport.  This file proves the two are actually joined: a real
external server, a real handshake, and then a call made through the ordinary
``execute_tool`` dispatcher — the same entry point the agent's tool loop uses.

If the wiring regressed — the external tool published but not dispatchable, or
dispatchable but not visible to the model — this is the test that fails.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from tool_catalog import clear_external_tools, get_tool_schemas


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _spec() -> "object":
    from mcp_client import McpServerSpec

    return McpServerSpec(
        name="self",
        command=sys.executable,
        args=(str(PROJECT_ROOT / "mcp_server.py"),),
        env={"PYTHONPATH": str(PROJECT_ROOT), "MCP_LOG_LEVEL": "WARNING"},
        timeout_seconds=60.0,
    )


class WiredExternalToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mcp_client import ExternalToolRegistry

        cls.registry = ExternalToolRegistry([_spec()])
        cls.registry.start(timeout_seconds=180.0)
        cls.registry.publish_to_catalog()

    @classmethod
    def tearDownClass(cls):
        cls.registry.unpublish_from_catalog()
        cls.registry.stop()
        clear_external_tools()

    def test_the_model_sees_the_external_tool(self):
        names = {schema["function"]["name"] for schema in get_tool_schemas()}
        self.assertIn("mcp__self__retrieval_metrics", names)

    def test_the_schema_carries_the_external_description(self):
        for schema in get_tool_schemas():
            if schema["function"]["name"] == "mcp__self__list_papers":
                self.assertTrue(schema["function"]["description"])
                self.assertEqual(
                    schema["function"]["parameters"].get("type", "object"), "object",
                )
                return
        self.fail("外部工具的 schema 不在工具列表里")

    def test_the_ordinary_dispatcher_reaches_the_external_tool(self):
        # This is the path the agent's tool loop uses.  If it returns the
        # server's payload, the wiring is complete.
        from tools import execute_tool

        result = execute_tool("mcp__self__retrieval_metrics", {})
        self.assertNotIn("未知工具", result)
        self.assertNotIn("❌", result)
        self.assertIn("duration_ms", result)

    def test_the_runtime_reaches_the_external_tool(self):
        from tool_runtime import ToolExecutionContext, ToolRuntime

        runtime = ToolRuntime(ToolExecutionContext(run_kind="research"))
        result = runtime.execute("mcp__self__retrieval_metrics", {})
        self.assertNotIn("❌", result)
        self.assertIn("duration_ms", result)

    def test_the_runtime_still_bounds_the_external_result(self):
        # The external tool inherits the result limit: it is the same runtime.
        from tool_runtime import ToolExecutionContext, ToolRuntime

        runtime = ToolRuntime(ToolExecutionContext(run_kind="research"))
        result = runtime.execute("mcp__self__list_papers", {})
        self.assertLess(len(result), 20_000)

    def test_the_builtin_tools_still_work_alongside(self):
        from tools import execute_tool

        result = execute_tool("list_papers", {})
        self.assertNotIn("未知工具", result)


if __name__ == "__main__":
    unittest.main()
"""End-to-end test of the MCP client against a real external server.

The unit tests in ``test_mcp_client.py`` cover the boundary.  This one covers
the transport: it launches an actual MCP server process, completes the real
handshake, lists the tools the server advertises and calls one of them.

The server used here is this project's own ``mcp_server.py``.  That is a
deliberate choice: it means the round trip is symmetric — the server side of
this repository is also a valid external server for the client side, so a
regression in either half shows up here.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from mcp_client import ExternalToolRegistry, McpServerSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _spec() -> McpServerSpec:
    return McpServerSpec(
        name="self",
        command=sys.executable,
        args=(str(PROJECT_ROOT / "mcp_server.py"),),
        env={"PYTHONPATH": str(PROJECT_ROOT), "MCP_LOG_LEVEL": "WARNING"},
        timeout_seconds=60.0,
    )


class McpClientEndToEndTests(unittest.TestCase):
    """The client must actually talk to a server over stdio."""

    @classmethod
    def setUpClass(cls):
        cls.registry = ExternalToolRegistry([_spec()])
        # The server warms its vector store before serving, so the first
        # handshake is slow on a cold model cache.
        cls.registry.start(timeout_seconds=180.0)
        cls.registry.publish_to_catalog()

    @classmethod
    def tearDownClass(cls):
        cls.registry.unpublish_from_catalog()
        cls.registry.stop()

    def test_tools_are_discovered(self):
        names = {tool.remote_name for tool in self.registry.tools()}
        self.assertIn("retrieval_metrics", names)
        self.assertIn("list_papers", names)

    def test_tools_are_namespaced(self):
        for tool in self.registry.tools():
            self.assertTrue(tool.qualified_name.startswith("mcp__self__"))

    def test_the_tool_is_visible_to_the_model(self):
        from tool_catalog import get_tool_schemas

        names = {schema["function"]["name"] for schema in get_tool_schemas()}
        self.assertIn("mcp__self__retrieval_metrics", names)

    def test_calling_an_external_tool_returns_its_result(self):
        result = self.registry.call("mcp__self__retrieval_metrics", {})
        self.assertNotIn("❌", result)
        self.assertIn("duration_ms", result)

    def test_calling_an_unknown_tool_is_reported_not_raised(self):
        result = self.registry.call("mcp__self__does_not_exist", {})
        self.assertIn("❌", result)

    def test_calling_a_tool_from_a_dead_server_is_reported(self):
        # A registry that never started has no session; the call must report
        # that rather than raising, because the model has to be able to
        # continue with the failure in hand.
        empty = ExternalToolRegistry([])
        result = empty.call("mcp__nothing__here", {})
        self.assertIn("❌", result)


class DegradationTests(unittest.TestCase):
    """A server that cannot start must not take the agent down."""

    def test_unstartable_server_is_recorded_not_raised(self):
        registry = ExternalToolRegistry([
            McpServerSpec(
                name="broken",
                command=sys.executable,
                args=("-c", "raise SystemExit(3)"),
                timeout_seconds=10.0,
            ),
        ])
        # Must not raise.
        registry.start(timeout_seconds=60.0)
        try:
            self.assertTrue(registry.errors())
            self.assertEqual(registry.tools(), [])
            # And a call against it is a message, not a crash.
            self.assertIn("❌", registry.call("mcp__broken__x", {}))
        finally:
            registry.stop()

    def test_disabled_server_is_never_launched(self):
        registry = ExternalToolRegistry([
            McpServerSpec(
                name="off",
                command=sys.executable,
                args=("-c", "raise SystemExit(3)"),
                enabled=False,
            ),
        ])
        registry.start(timeout_seconds=30.0)
        try:
            self.assertEqual(registry.tools(), [])
            self.assertEqual(registry.errors(), {})
        finally:
            registry.stop()

    def test_empty_registry_is_a_no_op(self):
        registry = ExternalToolRegistry([])
        registry.start(timeout_seconds=5.0)
        try:
            self.assertEqual(registry.tools(), [])
            self.assertEqual(registry.publish_to_catalog(), [])
        finally:
            registry.stop()


if __name__ == "__main__":
    unittest.main()
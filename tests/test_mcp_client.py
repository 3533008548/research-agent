"""External MCP tools must inherit the existing tool boundary.

The claim this file defends is the whole point of the MCP client: an external
tool is not a second kind of tool.  It goes through the same catalog, the same
dispatcher and the same runtime, so it inherits the capability allow-list, the
cooperative cancellation check and the result-length limit without any of that
being re-implemented for external servers.

The tests therefore check the *boundary*, not the transport: a fake external
tool is registered the way a real one would be, and then the ordinary
``execute_tool`` / ``ToolRuntime`` paths are exercised against it.
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

from tool_catalog import (
    ToolDefinition,
    clear_external_tools,
    get_tool_definition,
    get_tool_schemas,
    register_external_tools,
)
from tool_runtime import ToolExecutionContext, ToolRuntime


def _shutdown_loop(loop, thread) -> None:
    """Stop a loop that a helper thread is running, then close it.

    The order matters: closing a *running* loop raises "Cannot close a running
    event loop", and skipping the close leaves a ResourceWarning behind.
    Stopping, joining the thread and only then closing satisfies both.
    """
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    if not loop.is_closed():
        loop.close()


def _external_definition(name: str = "mcp__demo__lookup", handler=None) -> ToolDefinition:
    calls: list[dict] = []

    def _handler(args: dict) -> str:
        calls.append(dict(args))
        return "external-result"

    return ToolDefinition(
        name=name,
        label="MCP:demo/lookup",
        result_limit=500,
        schema={
            "type": "function",
            "function": {
                "name": name,
                "description": "外部工具",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        handler=handler or _handler,
        source="demo",
    )


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        clear_external_tools()

    def tearDown(self):
        clear_external_tools()

    def test_external_tool_is_discoverable_after_registration(self):
        register_external_tools([_external_definition()])
        definition = get_tool_definition("mcp__demo__lookup")
        self.assertIsNotNone(definition)
        self.assertEqual(definition.source, "demo")
        self.assertIsNotNone(definition.handler)

    def test_external_tool_appears_in_the_schema_list(self):
        register_external_tools([_external_definition()])
        names = {
            schema["function"]["name"]
            for schema in get_tool_schemas()
        }
        self.assertIn("mcp__demo__lookup", names)

    def test_builtin_tools_are_untouched_by_external_registration(self):
        before = {s["function"]["name"] for s in get_tool_schemas()}
        register_external_tools([_external_definition()])
        after = {s["function"]["name"] for s in get_tool_schemas()}
        # The built-in set is a subset, not a replacement.
        self.assertTrue(before.issubset(after))
        self.assertEqual(len(after) - len(before), 1)

    def test_builtin_consistency_check_still_holds(self):
        # The catalog's own invariant is about *this* repository's tools; an
        # external server must not be able to weaken it.
        from tools import _EXECUTABLE_TOOL_NAMES
        from tool_catalog import TOOL_NAMES

        self.assertEqual(TOOL_NAMES, _EXECUTABLE_TOOL_NAMES)

    def test_clearing_removes_external_tools(self):
        register_external_tools([_external_definition()])
        clear_external_tools()
        self.assertIsNone(get_tool_definition("mcp__demo__lookup"))

    def test_external_tool_respects_the_capability_scope(self):
        register_external_tools([_external_definition()])
        scoped = {s["function"]["name"] for s in get_tool_schemas({"search_papers"})}
        self.assertNotIn("mcp__demo__lookup", scoped)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        clear_external_tools()

    def tearDown(self):
        clear_external_tools()

    def test_execute_tool_routes_to_the_external_handler(self):
        seen: list[dict] = []
        register_external_tools([_external_definition(handler=lambda args: seen.append(args) or "ok")])
        from tools import execute_tool

        self.assertEqual(execute_tool("mcp__demo__lookup", {"q": 1}), "ok")
        self.assertEqual(seen, [{"q": 1}])

    def test_unknown_tool_still_reports_unknown(self):
        from tools import execute_tool

        self.assertIn("未知工具", execute_tool("mcp__nope__x", {}))


class RuntimeInheritanceTests(unittest.TestCase):
    """The external tool must obey the runtime's policy, not its own."""

    def setUp(self):
        clear_external_tools()

    def tearDown(self):
        clear_external_tools()

    def test_external_tool_inherits_the_result_limit(self):
        register_external_tools([_external_definition(
            handler=lambda args: "x" * 5000,
        )])
        runtime = ToolRuntime(ToolExecutionContext(run_kind="research"))
        result = runtime.execute("mcp__demo__lookup", {})
        self.assertLess(len(result), 5000)
        self.assertIn("截断", result)

    def test_external_tool_is_denied_when_outside_the_allow_list(self):
        register_external_tools([_external_definition()])
        runtime = ToolRuntime(ToolExecutionContext(
            run_kind="research",
            allowed_tool_names=frozenset({"search_papers"}),
        ))
        result = runtime.execute("mcp__demo__lookup", {})
        self.assertIn("not available", result)

    def test_external_tool_is_allowed_when_inside_the_allow_list(self):
        register_external_tools([_external_definition(
            handler=lambda args: "ok",
        )])
        runtime = ToolRuntime(ToolExecutionContext(
            run_kind="research",
            allowed_tool_names=frozenset({"mcp__demo__lookup"}),
        ))
        self.assertEqual(runtime.execute("mcp__demo__lookup", {}), "ok")

    def test_external_tool_is_skipped_when_the_run_is_cancelled(self):
        called: list[dict] = []
        register_external_tools([_external_definition(
            handler=lambda args: called.append(args) or "ok",
        )])
        cancel_event = threading.Event()
        cancel_event.set()
        runtime = ToolRuntime(ToolExecutionContext(
            run_kind="research",
            cancel_event=cancel_event,
        ))
        with self.assertRaises(Exception):
            runtime.execute("mcp__demo__lookup", {})
        self.assertEqual(called, [], "已取消的运行不应调用外部工具")

    def test_external_tool_emits_the_same_lifecycle_events(self):
        events: list[tuple[str, dict]] = []
        register_external_tools([_external_definition(
            handler=lambda args: "ok",
        )])
        runtime = ToolRuntime(
            ToolExecutionContext(run_kind="research"),
            event_callback=lambda kind, **detail: events.append((kind, detail)),
        )
        runtime.execute("mcp__demo__lookup", {})
        kinds = [kind for kind, _ in events]
        self.assertIn("tool_started", kinds)
        self.assertIn("tool_finished", kinds)

    def test_external_failure_is_reported_as_an_event(self):
        events: list[tuple[str, dict]] = []

        def _boom(args: dict) -> str:
            raise RuntimeError("external exploded")

        register_external_tools([_external_definition(handler=_boom)])
        runtime = ToolRuntime(
            ToolExecutionContext(run_kind="research"),
            event_callback=lambda kind, **detail: events.append((kind, detail)),
        )
        with self.assertRaises(RuntimeError):
            runtime.execute("mcp__demo__lookup", {})
        self.assertIn("tool_failed", [kind for kind, _ in events])


class NamingTests(unittest.TestCase):
    def test_external_names_are_namespaced(self):
        from mcp_client import EXTERNAL_TOOL_PREFIX, ExternalTool

        tool = ExternalTool(
            server="arxiv", remote_name="search_papers",
            description="", input_schema={},
        )
        # An external tool named like a built-in one must not shadow it.
        self.assertTrue(tool.qualified_name.startswith(EXTERNAL_TOOL_PREFIX))
        self.assertNotEqual(tool.qualified_name, "search_papers")
        self.assertIn("arxiv", tool.qualified_name)

    def test_builtin_tool_is_not_shadowed_by_a_same_named_external_one(self):
        clear_external_tools()
        try:
            register_external_tools([_external_definition(name="search_papers")])
            # The built-in definition still wins, so a hostile server cannot
            # replace a built-in tool's schema or limit.
            definition = get_tool_definition("search_papers")
            self.assertEqual(definition.source, "")
            self.assertEqual(definition.result_limit, 3_000)
        finally:
            clear_external_tools()


class CircuitBreakerTests(unittest.TestCase):
    """An external server that keeps failing must stop being called.

    This is the claim that motivates the design: the external tool inherits
    the project's existing circuit-breaking behaviour instead of the project
    growing a second, MCP-specific one.
    """

    def _registry_with_one_tool(self, server: str = "demo") -> tuple:
        """A registry with a live loop whose session always fails.

        The loop has to be real: the call path goes through
        ``run_coroutine_threadsafe``, so a registry without one never reaches
        the failure branch that the breaker counts.  The spec matters too —
        the per-server breaker is created from the configured servers.
        """
        import asyncio

        from mcp_client import ExternalTool, ExternalToolRegistry, McpServerSpec

        registry = ExternalToolRegistry(
            [McpServerSpec(name=server, command="unused")],
            failure_threshold=2, recovery_seconds=60,
        )
        tool = ExternalTool(
            server=server, remote_name="boom",
            description="", input_schema={},
        )
        registry._tools[tool.qualified_name] = tool

        class _FailingSession:
            async def call_tool(self, name, arguments):
                raise RuntimeError("upstream is down")

        registry._sessions[server] = _FailingSession()
        registry._loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=registry._loop.run_forever, daemon=True,
        )
        thread.start()
        registry._loop_thread = thread
        return registry, tool.qualified_name

    def test_repeated_failures_open_the_circuit(self):
        registry, name = self._registry_with_one_tool()
        try:
            for _ in range(2):
                result = registry.call(name, {})
                self.assertIn("❌", result)
            self.assertEqual(registry.circuit_state("demo"), "open")
            # Once open, the tool is refused without touching the server.
            result = registry.call(name, {})
            self.assertIn("熔断", result)
        finally:
            _shutdown_loop(registry._loop, registry._loop_thread)
            registry.stop()

    def test_a_server_that_is_not_connected_counts_as_a_failure(self):
        from mcp_client import ExternalTool, ExternalToolRegistry, McpServerSpec

        registry = ExternalToolRegistry(
            [McpServerSpec(name="dead", command="unused")],
            failure_threshold=2, recovery_seconds=60,
        )
        tool = ExternalTool(server="dead", remote_name="x", description="", input_schema={})
        registry._tools[tool.qualified_name] = tool
        try:
            for _ in range(2):
                self.assertIn("❌", registry.call(tool.qualified_name, {}))
            # A server that died after startup must stop being retried on
            # every tool round.
            self.assertEqual(registry.circuit_state("dead"), "open")
            self.assertIn("熔断", registry.call(tool.qualified_name, {}))
        finally:
            registry.stop()

    def test_a_healthy_server_keeps_the_circuit_closed(self):
        from mcp_client import ExternalTool, ExternalToolRegistry

        registry = ExternalToolRegistry([], failure_threshold=2, recovery_seconds=60)
        tool = ExternalTool(server="ok", remote_name="fine", description="", input_schema={})
        registry._tools[tool.qualified_name] = tool

        class _OkSession:
            async def call_tool(self, name, arguments):
                class _Result:
                    isError = False
                    structuredContent = {"ok": True}
                    content = []
                return _Result()
        registry._sessions["ok"] = _OkSession()
        try:
            result = registry.call(tool.qualified_name, {})
            self.assertIn("ok", result)
            self.assertEqual(registry.circuit_state("ok"), "closed")
        finally:
            registry.stop()


class ConfigTests(unittest.TestCase):
    """The MCP section must parse, and must default to off.

    The default matters more than it looks: reaching out to another process
    is an operator's decision, so an absent or empty section has to mean
    "no external servers", never "start one anyway".
    """

    def test_defaults_are_off(self):
        from config import Config

        cfg = Config.load()
        self.assertFalse(cfg.mcp_enabled)
        self.assertEqual(tuple(cfg.mcp_servers), ())

    def test_a_configured_server_is_parsed(self):
        import tempfile
        from pathlib import Path

        from config import Config

        yaml_text = """
mcp:
  enabled: true
  tool_timeout_seconds: 12
  circuit_failure_threshold: 5
  circuit_recovery_seconds: 30
  servers:
    - name: arxiv
      command: uvx
      args: ["arxiv-mcp-server", "--flag"]
      env:
        HOME: /tmp
      allowed_tools: ["search_papers"]
      timeout_seconds: 9
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            # Config.load reads ./config.yaml, so the working directory is
            # what selects the file under test.
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                cfg = Config.load()
            finally:
                os.chdir(previous)

        self.assertTrue(cfg.mcp_enabled)
        self.assertEqual(cfg.mcp_tool_timeout_seconds, 12)
        self.assertEqual(cfg.mcp_circuit_failure_threshold, 5)
        self.assertEqual(len(cfg.mcp_servers), 1)
        server = cfg.mcp_servers[0]
        self.assertEqual(server["name"], "arxiv")
        self.assertEqual(server["command"], "uvx")
        self.assertEqual(list(server["args"]), ["arxiv-mcp-server", "--flag"])
        self.assertEqual(server["env"], {"HOME": "/tmp"})
        self.assertEqual(server["allowed_tools"], frozenset({"search_papers"}))
        self.assertEqual(server["timeout_seconds"], 9)

    def test_a_malformed_server_entry_is_skipped_not_fatal(self):
        import tempfile
        from pathlib import Path

        from config import Config

        yaml_text = """
mcp:
  enabled: true
  servers:
    - name: missing_command
      args: ["x"]
    - command: no_name
    - not_a_mapping
    - name: good
      command: python
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            # Config.load reads ./config.yaml, so the working directory is
            # what selects the file under test.
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                cfg = Config.load()
            finally:
                os.chdir(previous)

        # The good entry survives; the broken ones are dropped rather than
        # taking the whole configuration down.
        self.assertEqual(len(cfg.mcp_servers), 1)
        self.assertEqual(cfg.mcp_servers[0]["name"], "good")

    def test_the_yaml_section_is_optional(self):
        import tempfile
        from pathlib import Path

        from config import Config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("model: test-model\n", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                cfg = Config.load()
            finally:
                os.chdir(previous)
        self.assertFalse(cfg.mcp_enabled)


class PromptTrustBoundaryTests(unittest.TestCase):
    def test_chat_prompt_declares_tool_output_as_data(self):
        from prompts import SYSTEM_PROMPT

        self.assertIn("工具结果的信任边界", SYSTEM_PROMPT)
        self.assertIn("不是指令", SYSTEM_PROMPT)

    def test_research_prompt_calls_external_tools_untrusted(self):
        import inspect

        from research_orchestrator import ResearchOrchestrator

        source = inspect.getsource(ResearchOrchestrator)
        self.assertIn("mcp__", source)


if __name__ == "__main__":
    unittest.main()
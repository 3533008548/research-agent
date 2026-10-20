"""End-to-end check: talk to the server the way a real MCP client would.

Unit tests cover the service and the schemas.  This one launches the actual
process and drives it over stdio with the official client, because that is the
only way to catch protocol-level faults: a stdout banner, a missing handshake,
or a tool whose return type the client cannot decode all pass every unit test.
"""

from __future__ import annotations

import os
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpStdioIntegrationTests(unittest.TestCase):
    """Launch ``python mcp_server.py`` and inspect it as a client would."""

    @classmethod
    def setUpClass(cls):
        from mcp.shared.exceptions import McpError

        cls._McpError = McpError

    def test_client_can_initialize_and_list_tools(self):
        async def exercise():
            params = StdioServerParameters(
                command=sys.executable,
                args=[os.path.join(PROJECT_ROOT, "mcp_server.py")],
                # The project must be importable when launched from any cwd.
                env={**os.environ, "PYTHONPATH": PROJECT_ROOT, "MCP_LOG_LEVEL": "WARNING"},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return [tool.name for tool in listed.tools], [
                        tool.inputSchema for tool in listed.tools
                    ]

        import anyio

        try:
            names, schemas = anyio.run(exercise)
        except self._McpError as exc:  # pragma: no cover - surfaces a protocol fault
            self.fail(f"MCP 初始化失败：{exc}")

        self.assertEqual(
            set(names),
            {"search_papers", "query_papers", "list_papers", "list_indexed_papers", "retrieval_metrics"},
        )
        # Every tool must publish a usable schema, or a client cannot call it.
        for schema in schemas:
            self.assertEqual(schema.get("type"), "object")

    def test_list_papers_round_trips_through_the_protocol(self):
        async def exercise():
            params = StdioServerParameters(
                command=sys.executable,
                args=[os.path.join(PROJECT_ROOT, "mcp_server.py")],
                env={**os.environ, "PYTHONPATH": PROJECT_ROOT, "MCP_LOG_LEVEL": "WARNING"},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("list_papers", {})

        import anyio

        result = anyio.run(exercise)
        # structuredContent is the field a programmatic client actually reads;
        # a text blob would mean the tool degraded to prose on the wire.
        payload = getattr(result, "structuredContent", None) or {}
        self.assertIn("count", payload, f"未收到结构化结果：{result}")
        self.assertIsInstance(payload["count"], int)
        self.assertIn("papers", payload)

    def test_retrieval_metrics_is_callable_without_a_corpus(self):
        async def exercise():
            params = StdioServerParameters(
                command=sys.executable,
                args=[os.path.join(PROJECT_ROOT, "mcp_server.py")],
                env={**os.environ, "PYTHONPATH": PROJECT_ROOT, "MCP_LOG_LEVEL": "WARNING"},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("retrieval_metrics", {})

        import anyio

        result = anyio.run(exercise)
        payload = getattr(result, "structuredContent", None) or {}
        self.assertEqual(payload.get("unit"), "duration_ms")
        # The whole point of the tool: it must not need a vector store.
        for stage in ("semantic", "lexical", "fusion", "rerank"):
            self.assertIn(stage, payload.get("stages", {}))

    @unittest.skipUnless(
        os.path.isdir(os.path.join(PROJECT_ROOT, "runtime")),
        "本地没有论文库，跳过需要加载模型的用例",
    )
    def test_store_backed_tool_answers_over_stdio(self):
        """A tool that builds ``PaperStore`` must actually return.

        Building the store from inside the event loop deadlocks the server, so
        the symptom is a successful handshake followed by silence. Only a real
        call can catch it, which is why the warm-up case lives here and not in
        the unit tests.
        """
        async def exercise():
            params = StdioServerParameters(
                command=sys.executable,
                args=[os.path.join(PROJECT_ROOT, "mcp_server.py")],
                env={**os.environ, "PYTHONPATH": PROJECT_ROOT, "MCP_LOG_LEVEL": "WARNING"},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Generous: this covers a cold model load, and the point is
                    # to distinguish "slow" from "deadlocked", not to benchmark.
                    with anyio.fail_after(600):
                        return await session.call_tool("list_indexed_papers", {})

        import anyio

        try:
            result = anyio.run(exercise)
        except TimeoutError:
            self.fail("list_indexed_papers 超过 600 秒无响应：store 预热顺序可能已被改坏")

        payload = getattr(result, "structuredContent", None) or {}
        self.assertTrue(payload.get("available"), "论文库不可用，本用例的前提不成立")


if __name__ == "__main__":
    unittest.main()
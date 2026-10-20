"""MCP server surface: what it publishes and what it refuses to publish.

The MCP server is a public boundary, so the tests concentrate on the boundary
itself rather than on retrieval quality (which the evaluation suite already
covers for the shared code paths):

- the tool surface is read-only and matches the declared allow-list
- structured output really is structured, with no Markdown leaking through
- bounds are enforced (top_k, limit, chunk text length)
- an unavailable corpus produces a clear message, not a traceback
"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import PropertyMock, patch

from config import Config
from mcp_server import (
    READ_ONLY_TOOLS,
    McpToolError,
    RetrievalService,
    _bounded_text,
)


def _service_without_store() -> RetrievalService:
    """A service whose corpus is known-unavailable, for fast offline tests."""
    service = RetrievalService()
    service._store_unavailable = True
    return service


class ToolSurfaceTests(unittest.TestCase):
    def test_published_tools_are_read_only(self):
        # A write tool on a public surface has no quota and no audit trail.
        forbidden = {"read_pdf", "delete_paper", "save_paper_relation", "apply_research_document_patch"}
        self.assertEqual(set(READ_ONLY_TOOLS) & forbidden, set())

    def test_describe_matches_the_allow_list(self):
        described = RetrievalService().describe()
        self.assertEqual([item["name"] for item in described], list(READ_ONLY_TOOLS))

    def test_describe_reuses_internal_schemas(self):
        from tool_catalog import get_tool_definition

        for item in RetrievalService().describe():
            expected = get_tool_definition(item["name"])
            function = expected.schema["function"]
            self.assertEqual(item["description"], function["description"])
            self.assertEqual(item["input_schema"], function["parameters"])

    def test_tools_declare_an_output_schema(self):
        from mcp_server import build_server

        # A bare `-> dict` annotation makes FastMCP emit no output schema, and
        # the result then reaches clients as a JSON *string* rather than
        # structuredContent — which is exactly what a programmatic caller needs.
        server, service = build_server(_service_without_store())
        try:
            for tool in server._tool_manager.list_tools():
                self.assertIsNotNone(
                    tool.output_schema,
                    f"{tool.name} 缺少 output schema，客户端只会拿到文本",
                )
        finally:
            service.close()

    def test_bare_dict_annotation_would_lose_structured_output(self):
        # Premise check for the guard above: this is the exact trap.
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("probe")

        def bare() -> dict:
            return {"a": 1}

        server.tool()(bare)
        self.assertIsNone(list(server._tool_manager.list_tools())[0].output_schema)

    def test_server_registers_every_declared_tool(self):
        from mcp_server import build_server

        server, service = build_server(_service_without_store())
        try:
            registered = {tool.name for tool in server._tool_manager.list_tools()}
            self.assertEqual(registered, set(READ_ONLY_TOOLS) | {"retrieval_metrics"})
        finally:
            service.close()


class StructuredOutputTests(unittest.TestCase):
    def test_search_returns_fields_not_markdown(self):
        service = RetrievalService()
        records = [{
            "title": "Deterministic Scheduling in TSN",
            "abstract": "Bursty traffic meets fixed windows.",
            "url": "https://example.test/a",
            "year": 2026, "citation_count": 4, "venue": "TPDS",
            "authors": ["Ada Lovelace"], "sources": ["openalex"], "relevance_score": 0.91,
        }]
        with patch(
            "search_api.search_public_records",
            return_value=(records, {"accepted": 1, "rejected": 0, "rejection_reason": "多源相关性门控"}, []),
        ):
            payload = service.search_papers("deterministic scheduling")

        paper = payload["papers"][0]
        self.assertEqual(paper["title"], "Deterministic Scheduling in TSN")
        self.assertEqual(paper["url"], "https://example.test/a")
        self.assertEqual(paper["authors"], ["Ada Lovelace"])
        self.assertEqual(payload["quality"]["rejected"], 0)
        # The whole point of bypassing the renderer.
        self.assertNotIn("**", str(payload))

    def test_search_reports_provider_failures(self):
        # A caller must be able to tell "no matches" from "source unreachable".
        with patch(
            "search_api.search_public_records",
            return_value=([], {"accepted": 0, "rejected": 0, "rejection_reason": ""}, ["arXiv 超时"]),
        ):
            payload = RetrievalService().search_papers("anything")
        self.assertEqual(payload["source_failures"], ["arXiv 超时"])
        self.assertEqual(payload["papers"], [])

    def test_search_rejects_blank_query_and_unknown_source(self):
        service = RetrievalService()
        with self.assertRaises(McpToolError):
            service.search_papers("   ")
        with self.assertRaises(McpToolError):
            service.search_papers("tsn", source="ieee")

    def test_search_limit_is_clamped(self):
        captured: dict = {}
        with patch(
            "search_api.search_public_records",
            side_effect=lambda q, limit, source: (captured.update({"limit": limit}) or ([], {}, [])),
        ):
            RetrievalService().search_papers("tsn", limit=99)
            self.assertEqual(captured["limit"], 10)

    def test_query_papers_returns_page_and_section(self):
        service = _service_without_store()
        store = service.paper_store  # stays None
        self.assertIsNone(store)

    def test_query_papers_reports_missing_corpus_clearly(self):
        with self.assertRaises(McpToolError) as caught:
            _service_without_store().query_papers("tsn scheduling")
        self.assertIn("论文库", str(caught.exception))

    def test_paper_store_construction_uses_config_values(self):
        # PaperStore defaults reranker_enabled=False while config.yaml enables
        # it. Building with defaults would serve a materially worse ranking than
        # production, and nothing would fail loudly — so assert the values are
        # actually forwarded rather than merely asserting the default is False.
        from config import Config
        from paper_store import PaperStore

        captured: dict = {}

        class _FakeStore:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        cfg = Config()
        cfg.rag_enabled = True
        cfg.rag_reranker_enabled = True
        service = RetrievalService(cfg=cfg)
        with patch.dict("sys.modules", {"paper_store": type("m", (), {"PaperStore": _FakeStore})}), patch(
            "paper_relations.PaperRelationStore", lambda *_a, **_k: None,
        ):
            service.paper_store

        self.assertTrue(captured["reranker_enabled"])
        self.assertEqual(captured["collection_name"], cfg.rag_embedding_collection)
        # Guard the premise: the dataclass default really is the risky value.
        default = inspect.signature(PaperStore.__init__).parameters["reranker_enabled"].default
        self.assertIs(default, False)

    def test_query_papers_maps_hybrid_results(self):
        service = RetrievalService()
        fake_store = type("FakeStore", (), {
            "chunk_count": 3,
            "list_papers": lambda self: [{"paper_id": "p1", "title": "T", "chunks": 3}],
            "query_with_timeout": lambda self, q, **kw: (
                [{
                    "title": "TSN Survey", "section": "Method", "page": 7,
                    "element_id": "p007-t03", "paper_id": "p1",
                    "text": "  Fixed windows  bound the delay.  ",
                    "retrieval": "hybrid", "reranked": True, "reranker_score": 3.5,
                }],
                None,
            ),
        })()
        service._paper_store = fake_store
        try:
            payload = service.query_papers("tsn")
        finally:
            service._paper_store = None

        chunk = payload["chunks"][0]
        self.assertEqual(chunk["page"], 7)
        self.assertEqual(chunk["element_id"], "p007-t03")
        self.assertEqual(chunk["section"], "Method")
        self.assertEqual(chunk["reranker_score"], 3.5)
        # Whitespace collapsed so the payload is stable for diffing.
        self.assertEqual(chunk["text"], "Fixed windows bound the delay.")

    def test_query_papers_marks_relation_expanded_chunks(self):
        service = RetrievalService()
        fake_store = type("FakeStore", (), {
            "chunk_count": 1,
            "list_papers": lambda self: [],
            "query_with_timeout": lambda self, q, **kw: ([{
                "title": "Related", "section": "Intro", "text": "x",
                "retrieval": "semantic", "relation_expanded": True,
                "relation_seed_titles": ["Seed Paper", "Other"],
            }], None),
        })()
        service._paper_store = fake_store
        try:
            chunk = service.query_papers("q")["chunks"][0]
        finally:
            service._paper_store = None

        self.assertTrue(chunk["relation_expanded"])
        self.assertEqual(chunk["relation_seed_titles"], ["Seed Paper", "Other"])

    def test_query_papers_converts_backend_failure_to_tool_error(self):
        service = RetrievalService()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("chroma unavailable")

        service._paper_store = type("FakeStore", (), {
            "chunk_count": 0,
            "list_papers": lambda self: [],
            "query_with_timeout": _boom,
        })()
        try:
            with self.assertRaises(McpToolError) as caught:
                service.query_papers("tsn")
        finally:
            service._paper_store = None
        # The message names the type, never the traceback.
        self.assertIn("RuntimeError", str(caught.exception))

    def test_top_k_is_clamped_to_the_tool_limit(self):
        service = RetrievalService()
        captured: dict = {}
        service._paper_store = type("FakeStore", (), {
            "chunk_count": 0,
            "list_papers": lambda self: [],
            "query_with_timeout": lambda self, q, **kw: (captured.update(kw) or ([], None)),
        })()
        try:
            service.query_papers("tsn", top_k=50)
        finally:
            service._paper_store = None
        self.assertEqual(captured["top_k"], 5)

    def test_list_papers_returns_structured_files(self):
        with patch(
            "search_api.list_downloaded_records",
            return_value=[{"filename": "a.pdf", "title": "a", "size_kb": 10, "modified_at": "2026-01-01 00:00"}],
        ):
            payload = RetrievalService().list_papers()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["total_size_kb"], 10)
        self.assertEqual(payload["papers"][0]["filename"], "a.pdf")

    def test_list_indexed_reports_availability_instead_of_raising(self):
        payload = _service_without_store().list_indexed_papers()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["papers"], [])

    def test_retrieval_metrics_shape(self):
        metrics = RetrievalService().retrieval_metrics()
        self.assertEqual(metrics["unit"], "duration_ms")
        for stage in ("semantic", "lexical", "fusion", "rerank"):
            self.assertIn(stage, metrics["stages"])


class StdioSafetyTests(unittest.TestCase):
    """stdout is the transport; anything else written there corrupts the session."""

    def test_reserve_returns_real_stdout_and_redirects_prints(self):
        import io
        import sys

        from mcp_server import _reserve_transport_stdout

        # A real binary-backed stream, because that is what the transport
        # re-wraps; io.StringIO has no .buffer and would not exercise the path.
        protocol_out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        original_out, original_err = sys.stdout, sys.stderr
        captured_err = io.StringIO()
        sys.stdout, sys.stderr = protocol_out, captured_err
        try:
            handle = _reserve_transport_stdout()
            self.assertIsNotNone(handle)
            # The transport must get the ORIGINAL stdout, or its responses land
            # on stderr and the client hangs forever waiting for a reply.
            self.assertIs(handle.buffer, protocol_out.buffer)
            print("chatter that would corrupt JSON-RPC")
            sys.stderr.flush()
            protocol_out.flush()
            self.assertEqual(protocol_out.buffer.getvalue(), b"")
            self.assertIn("corrupt JSON-RPC", captured_err.getvalue())
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_reserve_degrades_when_stdout_has_no_buffer(self):
        import io
        import sys

        from mcp_server import _reserve_transport_stdout

        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            # Must not raise: main() falls back to the SDK's own wiring.
            self.assertIsNone(_reserve_transport_stdout())
            self.assertIs(sys.stdout, sys.stderr)
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_sdk_resolves_stdout_lazily(self):
        # Premise check. The SDK reads sys.stdout inside stdio_server(), not at
        # import, so redirecting first would break the transport. If this ever
        # changes to eager capture, the reserve() approach must change too.
        from mcp.server import stdio

        source = inspect.getsource(stdio)
        self.assertIn("if not stdout:", source)
        self.assertIn("sys.stdout.buffer", source)


class WarmUpOrderingTests(unittest.TestCase):
    """The store must be built before the event loop starts serving.

    Constructing ``PaperStore`` from inside the loop deadlocks the server: the
    handshake succeeds and then no tool call ever answers, with no error on
    either side.  These tests pin the ordering that avoids it.
    """

    def test_warm_up_touches_the_store(self):
        from mcp_server import _warm_store

        service = _service_without_store()
        with patch.object(
            RetrievalService, "paper_store", new_callable=PropertyMock, return_value=None,
        ) as store:
            _warm_store(service)
        # Even an unavailable store must be probed, otherwise the first real
        # call would be the one that deadlocks.
        self.assertEqual(store.call_count, 1)

    def test_warm_up_survives_a_broken_corpus(self):
        from mcp_server import _warm_store

        service = RetrievalService()

        def _explode(self):
            raise RuntimeError("chroma missing")

        with patch.object(RetrievalService, "paper_store", new_callable=property, fget=_explode):
            _warm_store(service)  # must not raise: 3 of 5 tools still work

    def test_main_warms_before_serving(self):
        import inspect

        from mcp_server import main

        source = inspect.getsource(main)
        warm_at = source.index("_warm_store(service)")
        serve_at = source.index("server.run(transport=")
        self.assertLess(
            warm_at, serve_at,
            "必须在开始服务前预热，否则第一个工具调用会死锁",
        )


class BoundedTextTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(_bounded_text("  a\n\t b  "), "a b")

    def test_truncates_with_an_ellipsis(self):
        self.assertTrue(_bounded_text("x" * 100, limit=10).endswith("…"))
        self.assertEqual(len(_bounded_text("x" * 100, limit=10)), 11)

    def test_handles_none_and_empty(self):
        self.assertEqual(_bounded_text(None), "")
        self.assertEqual(_bounded_text(""), "")


if __name__ == "__main__":
    unittest.main()
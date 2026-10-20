"""Expose the project's existing retrieval capability as an MCP server.

Scope is deliberately narrow.  This server publishes capabilities that already
exist and already sit behind quality gates:

- ``search_papers``        public metadata search (OpenAlex / arXiv)
- ``query_papers``         local hybrid retrieval over the indexed corpus
- ``list_papers``          downloaded paper metadata
- ``list_indexed_papers``  what is actually in the vector store
- ``retrieval_metrics``    this process's aggregation latency breakdown

Two exclusions are decisions rather than omissions:

* **No write tools.**  ``read_pdf`` mutates the index and ``save_paper_relation``
  mutates user data.  An MCP server is a public surface: a write it performs has
  no quota, no audit trail and no quality gate, so those stay behind the app.
* **No prose parsing.**  The internal tool handlers return Markdown formatted for
  a model reading a transcript.  An MCP client is a program, so this layer calls
  the same functions those handlers call and returns their native dicts.  That
  avoids a text round-trip whose parser would break the first time a format
  string changed — and it keeps a single implementation of the ranking.

The transport is stdio: a local single-user workbench has no use for a TCP port
or an auth handshake, and stdio lets the client own the process lifetime.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from config import Config
from logger import get_logger


log = get_logger(__name__)


# The public tool surface.  An explicit allow-list rather than a slice of the
# internal catalog, which holds ~30 tools, most of which either write state or
# only make sense inside a chat transcript.
READ_ONLY_TOOLS = (
    "search_papers",
    "query_papers",
    "list_papers",
    "list_indexed_papers",
)

SERVER_NAME = "research-agent-retrieval"
SERVER_VERSION = "1.0.0"

# Chunk text is the useful payload but also the easy place to blow up a
# response.  The internal handlers bound this at the tool layer; the MCP layer
# needs its own bound because it is a different, non-model consumer.
_CHUNK_TEXT_LIMIT = 2_000


class McpToolError(RuntimeError):
    """A tool call failed in a way the caller can act on."""


def _bounded_text(value: object, limit: int = _CHUNK_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class RetrievalService:
    """Adapter over the project's existing retrieval functions.

    It holds no retrieval logic.  Its jobs are to build the same ``PaperStore``
    the application builds, to call the same search functions, and to bound what
    crosses the boundary.  Building the store from ``Config`` rather than from
    defaults matters: ``PaperStore`` defaults ``reranker_enabled=False`` while
    ``config.yaml`` enables it, so using defaults would silently serve a
    materially worse ranking than production.
    """

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config.load()
        self._paper_store: Any = None
        self._store_unavailable = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def paper_store(self) -> Any:
        """Build the store on first use so start-up stays fast and import-safe."""
        if self._paper_store is not None or self._store_unavailable:
            return self._paper_store
        if not self.cfg.rag_enabled:
            self._store_unavailable = True
            return None
        try:
            from paper_store import PaperStore
            from paper_relations import PaperRelationStore

            self._paper_store = PaperStore(
                persist_dir=self.cfg.chroma_dir,
                embedding_model=self.cfg.rag_embedding_model,
                embedding_dimensions=self.cfg.rag_embedding_dimensions,
                embedding_max_length=self.cfg.rag_embedding_max_length,
                embedding_batch_size=self.cfg.rag_embedding_batch_size,
                embedding_device=self.cfg.rag_embedding_device,
                collection_name=self.cfg.rag_embedding_collection,
                # Must come from config: the dataclass default is False.
                reranker_enabled=self.cfg.rag_reranker_enabled,
                reranker_model=self.cfg.rag_reranker_model,
                reranker_candidate_limit=self.cfg.rag_reranker_candidate_limit,
                reranker_max_length=self.cfg.rag_reranker_max_length,
                relation_store=PaperRelationStore(self.cfg.runtime_paths),
            )
        except Exception as exc:  # noqa: BLE001 - a missing corpus must not kill the server
            log.warning("MCP: 论文库不可用，检索类工具将返回空结果 (%s)", type(exc).__name__)
            self._store_unavailable = True
            return None
        return self._paper_store

    def close(self) -> None:
        store, self._paper_store = self._paper_store, None
        closer = getattr(store, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    # -- tools -------------------------------------------------------------

    def search_papers(self, query: str, source: str = "all", limit: int = 5) -> dict[str, Any]:
        """Search public metadata sources, returning the ranked records."""
        from search_api import search_public_records

        cleaned = str(query or "").strip()
        if not cleaned:
            raise McpToolError("请提供论文检索关键词。")
        selected = str(source or "all").lower()
        if selected not in {"all", "openalex", "arxiv"}:
            raise McpToolError("source 仅支持 all、openalex 或 arxiv；IEEE Xplore 暂时停用。")
        bounded = max(1, min(10, int(limit or 5)))

        papers, quality, failures = search_public_records(cleaned, limit=bounded, source=selected)
        return {
            "query": cleaned,
            "source": selected,
            "quality": {
                "accepted": quality.get("accepted", 0),
                "rejected": quality.get("rejected", 0),
                "rejection_reason": quality.get("rejection_reason", ""),
            },
            # A provider that failed is reported, never silently swallowed: the
            # caller must be able to tell "no matches" from "source unreachable".
            "source_failures": failures,
            "papers": [
                {
                    "title": _bounded_text(item.get("title"), 300),
                    "url": str(item.get("url") or ""),
                    "year": item.get("year"),
                    "venue": _bounded_text(item.get("venue"), 160),
                    "authors": [_bounded_text(name, 80) for name in (item.get("authors") or [])[:8]],
                    "citation_count": item.get("citation_count"),
                    "sources": list(item.get("sources") or []),
                    "relevance_score": item.get("relevance_score"),
                    "abstract": _bounded_text(item.get("abstract"), 600),
                }
                for item in papers
            ],
        }

    def query_papers(
        self,
        query: str,
        top_k: int = 3,
        section: str | None = None,
        paper_id_or_title: str | None = None,
    ) -> dict[str, Any]:
        """Run hybrid retrieval over the local corpus and return page-anchored chunks."""
        from tools.search import _resolve_scope

        cleaned = str(query or "").strip()
        if not cleaned:
            raise McpToolError("请提供要在论文库中检索的问题。")
        store = self.paper_store
        if store is None:
            raise McpToolError("RAG 功能未启用或论文库不可用，无法执行本地检索。")

        bounded_k = max(1, min(5, int(top_k or 3)))
        paper_ids = None
        if paper_id_or_title:
            # Same refusal semantics as the chat tool: an unresolvable paper
            # scope must not silently widen to the whole corpus.
            paper_ids, scope_error = _resolve_scope(store, paper_id_or_title)
            if scope_error:
                raise McpToolError(scope_error)

        try:
            results, _ = store.query_with_timeout(
                cleaned, top_k=bounded_k, paper_ids=paper_ids, section=section or None,
            )
        except Exception as exc:  # noqa: BLE001 - report, do not leak a traceback
            raise McpToolError(f"检索失败：{type(exc).__name__}") from exc

        chunks = []
        for item in results:
            record: dict[str, Any] = {
                "title": _bounded_text(item.get("title"), 300),
                "section": _bounded_text(item.get("section") or "未标注", 120),
                "text": _bounded_text(item.get("text")),
                "retrieval": str(item.get("retrieval") or "semantic"),
            }
            for source_key, target in (
                ("page", "page"),
                ("element_id", "element_id"),
                ("paper_id", "paper_id"),
            ):
                value = item.get(source_key)
                if value not in (None, ""):
                    record[target] = value
            if item.get("reranked") and item.get("reranker_score") is not None:
                record["reranker_score"] = item["reranker_score"]
            elif item.get("hybrid_score") is not None:
                record["hybrid_score"] = item["hybrid_score"]
            # Relation-expanded chunks are recall aids, not direct hits.  The
            # flag travels with the result so a client can tell the difference
            # and cite accordingly.
            if item.get("relation_expanded"):
                record["relation_expanded"] = True
                record["relation_seed_titles"] = [
                    _bounded_text(title, 200)
                    for title in (item.get("relation_seed_titles") or [])[:3]
                ]
            chunks.append(record)
        return {"query": cleaned, "count": len(chunks), "chunks": chunks}

    def list_papers(self) -> dict[str, Any]:
        """List locally downloaded PDFs."""
        from search_api import list_downloaded_records

        records = list_downloaded_records()
        return {
            "count": len(records),
            "total_size_kb": sum(item["size_kb"] for item in records),
            "papers": records,
        }

    def list_indexed_papers(self) -> dict[str, Any]:
        """List the papers actually present in the vector store."""
        store = self.paper_store
        if store is None:
            return {"count": 0, "chunk_count": 0, "papers": [], "available": False}
        papers = store.list_papers()
        return {
            "count": len(papers),
            "chunk_count": store.chunk_count,
            "papers": [
                {
                    "paper_id": str(item.get("paper_id") or ""),
                    "title": _bounded_text(item.get("title"), 300),
                    "chunks": item.get("chunks"),
                }
                for item in papers
            ],
            "available": True,
        }

    def retrieval_metrics(self) -> dict[str, Any]:
        """Aggregated stage latency for this process."""
        from papers.metrics import snapshot

        return snapshot()

    def describe(self) -> list[dict[str, Any]]:
        """Describe the published tools, reusing the internal JSON schemas."""
        from tool_catalog import get_tool_definition

        described = []
        for name in READ_ONLY_TOOLS:
            definition = get_tool_definition(name)
            if definition is None:
                # The catalog validates itself at import, so this means a name
                # was added to READ_ONLY_TOOLS without a catalog entry.
                raise McpToolError(f"工具目录中缺少 {name}")
            function = definition.schema.get("function", {})
            described.append({
                "name": name,
                "label": definition.label,
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            })
        return described


def build_server(service: RetrievalService | None = None):
    """Create the FastMCP server with the published tools registered."""
    from mcp.server.fastmcp import FastMCP

    active = service or RetrievalService()
    # ``version`` is not a FastMCP constructor argument in the pinned SDK, so the
    # version is advertised through the instructions instead of being dropped.
    server = FastMCP(
        SERVER_NAME,
        instructions=(
            f"{SERVER_NAME} {SERVER_VERSION}：只读的学术检索能力。"
            "检索本地论文库用 query_papers，检索公开元数据用 search_papers；"
            "两者都经过项目自身的相关性门控与重排。"
        ),
    )

    @server.tool()
    def search_papers(query: str, source: str = "all", limit: int = 5) -> dict[str, Any]:
        """搜索学术论文元数据（OpenAlex / arXiv），返回标题、作者、年份、链接与摘要。"""
        return active.search_papers(query, source=source, limit=limit)

    @server.tool()
    def query_papers(
        query: str,
        top_k: int = 3,
        section: str | None = None,
        paper_id_or_title: str | None = None,
    ) -> dict[str, Any]:
        """在本地已索引论文库中做混合检索（语义+词法+重排），返回带页码与章节的原文段落。"""
        return active.query_papers(
            query, top_k=top_k, section=section, paper_id_or_title=paper_id_or_title,
        )

    @server.tool()
    def list_papers() -> dict[str, Any]:
        """列出已下载到本地的论文（文件名、体积、修改时间）。"""
        return active.list_papers()

    @server.tool()
    def list_indexed_papers() -> dict[str, Any]:
        """列出已进入向量库、可被混合检索的论文及其块数。"""
        return active.list_indexed_papers()

    @server.tool()
    def retrieval_metrics() -> dict[str, Any]:
        """返回本进程检索各阶段的聚合耗时统计（语义 / 词法 / 融合 / 重排）。"""
        return active.retrieval_metrics()

    return server, active


def _reserve_transport_stdout():
    """Hand the transport the real stdout, then point ``sys.stdout`` at stderr.

    The stdio transport wraps ``sys.stdout.buffer`` for its JSON-RPC framing and
    resolves that attribute *when the server starts*.  ``FastMCP.run()`` accepts
    no stream override, so redirecting first would send every response to stderr
    — which presents as a client that initializes forever with no error, the
    worst kind of failure to diagnose.

    Therefore: capture the real stdout, swap ``sys.stdout`` to stderr so stray
    ``print()`` calls in this dependency-heavy codebase cannot inject non-JSON
    into the protocol stream, and return the captured handle for the transport
    to use explicitly.

    stderr stays the diagnostic channel: a client can redirect logs to a file,
    but it cannot recover a corrupted stdout.
    """
    import io

    transport_stdout = sys.stdout
    raw = getattr(transport_stdout, "buffer", None)
    sys.stdout.flush()
    sys.stdout = sys.stderr
    if raw is None:  # pragma: no cover - only when stdout is not a real file
        return None
    return io.TextIOWrapper(raw, encoding="utf-8")


def _run_stdio_with_reserved_stdout(server: Any, transport_stdout: Any) -> None:
    """Drive ``stdio_server`` against the reserved handle.

    Overriding the one async entry point keeps the SDK's own framing, session
    and initialisation logic intact instead of reimplementing the transport.
    The handle must be async-wrapped: ``stdio_server`` expects an already-wrapped
    async file and only falls back to wrapping ``sys.stdout`` itself when it is
    given nothing — which is exactly the path we are avoiding.
    """
    import anyio
    from mcp.server.stdio import stdio_server

    async def _serve() -> None:
        async with stdio_server(
            stdout=anyio.wrap_file(transport_stdout),
        ) as (read_stream, write_stream):
            await server._mcp_server.run(  # noqa: SLF001 - the documented internal shape
                read_stream,
                write_stream,
                server._mcp_server.create_initialization_options(),  # noqa: SLF001
            )

    anyio.run(_serve)


def main() -> int:
    """Run the server over stdio."""
    transport_stdout = _reserve_transport_stdout()
    service = None
    try:
        server, service = build_server()
        log.info("MCP 服务启动，公开工具：%s", ", ".join(READ_ONLY_TOOLS))
        # The default level is INFO and would write to the transport stream.
        server.settings.log_level = os.getenv("MCP_LOG_LEVEL", "INFO").upper()
        if transport_stdout is None:
            server.run(transport="stdio")
        else:
            _run_stdio_with_reserved_stdout(server, transport_stdout)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    sys.exit(main())
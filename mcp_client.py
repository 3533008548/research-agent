"""Consume external MCP servers as a source of model-facing tools.

This is the mirror image of :mod:`mcp_server`.  The server *publishes* this
project's retrieval capability; this module *consumes* somebody else's server
so the agent can call tools we do not implement.

The design goal is stated in the plan and is worth repeating because it is the
whole point of this module:

    External MCP tools must automatically inherit the existing
    circuit-breaking, timeout, retry and degradation behaviour.

That is achieved by **not** building a second execution path.  An external tool
is registered into the very same :mod:`tool_catalog` the built-in tools live in,
and is executed by the very same :class:`tool_runtime.ToolRuntime`.  The runtime
already applies the capability allow-list, the cooperative cancellation check,
the result-length limit and the failure events; the external tool inherits all
of them for free because it never bypasses the runtime.

If a second dispatcher had been written for external tools, that guarantee would
have been lost, and the abstraction would have been in the wrong place.

Three properties are deliberate:

* **Read-only by default.**  An external server's tool may mutate the world;
  the operator opts in per server.
* **Namespaced.**  An external tool named ``search_papers`` must not shadow a
  built-in one, so external tools are exposed as ``mcp__<server>__<tool>``.
* **Degrades, never breaks.**  A server that will not start is reported and
  skipped; the agent keeps its built-in tools.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from logger import get_logger
from resilience import CircuitBreaker


log = get_logger(__name__)


# The prefix that keeps external tools from colliding with built-in ones.
EXTERNAL_TOOL_PREFIX = "mcp__"

# Default ceiling on what an external tool may return to the model.  The
# built-in catalog has per-tool limits; external servers are unknown, so a
# single conservative ceiling applies to all of them.
DEFAULT_EXTERNAL_RESULT_LIMIT = 8_000


class McpClientError(RuntimeError):
    """An external MCP server could not be used."""


@dataclass(frozen=True)
class McpServerSpec:
    """How to launch and talk to one external MCP server.

    ``command``/``args`` describe a stdio server, which is the only transport
    this project supports for external servers: it needs no port, no auth
    handshake and no inbound network exposure.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    timeout_seconds: float = 30.0
    # Whether the operator allows this server's tools to be called at all.
    enabled: bool = True
    # Names of individual tools to expose.  Empty means "every tool the server
    # advertises", which is only sensible for a server the operator trusts.
    allowed_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExternalTool:
    """One tool advertised by an external MCP server."""

    server: str
    remote_name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        """The name the model sees, namespaced by server."""
        return f"{EXTERNAL_TOOL_PREFIX}{self.server}__{self.remote_name}"


def _render_result(result: Any) -> str:
    """Render an MCP tool result as the plain text the model expects.

    Structured content is preferred when present because it is the server's own
    representation; the text blocks are the fallback.  A tool that returns
    nothing usable is reported as such rather than as an empty success.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        import json

        return json.dumps(structured, ensure_ascii=False, indent=2)
    blocks = getattr(result, "content", None) or []
    texts = [getattr(block, "text", "") for block in blocks]
    joined = "\n".join(text for text in texts if text).strip()
    if joined:
        return joined
    return "（外部工具没有返回可读内容）"


class ExternalToolRegistry:
    """Hold the external tools discovered from every configured server.

    The registry is the bridge between two worlds: MCP sessions are async and
    the agent's tool path is synchronous.  A dedicated background thread owns
    the event loop and the live sessions; synchronous callers submit a
    coroutine to that loop and wait for the result.

    That thread also owns the per-server circuit breaker, so a server that has
    started failing stops being called at all instead of failing once per tool
    round.
    """

    def __init__(
        self,
        specs: list[McpServerSpec] | tuple[McpServerSpec, ...] = (),
        *,
        result_limit: int = DEFAULT_EXTERNAL_RESULT_LIMIT,
        failure_threshold: int = 3,
        recovery_seconds: int = 120,
    ) -> None:
        self.specs = tuple(specs)
        self.result_limit = max(1_000, int(result_limit))
        self._tools: dict[str, ExternalTool] = {}
        self._breakers: dict[str, CircuitBreaker] = {
            spec.name: CircuitBreaker(
                failure_threshold=failure_threshold,
                recovery_seconds=recovery_seconds,
            )
            for spec in self.specs
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._started = False
        self._errors: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout_seconds: float = 60.0) -> None:
        """Launch the servers and discover their tools.

        Failures are recorded per server and never raised: a missing external
        server must not stop the agent from working with its built-in tools.
        """
        with self._lock:
            if self._started:
                return
            self._started = True
        if not self.specs:
            return
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve(ready))
            except Exception as exc:  # noqa: BLE001 - a broken server is not fatal
                self._errors["__loop__"] = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception:  # noqa: BLE001
                    pass
                self._loop.close()

        self._thread = threading.Thread(
            target=_run, name="mcp-client-loop", daemon=True,
        )
        self._thread.start()
        if not ready.wait(timeout_seconds):
            self._errors["__startup__"] = f"启动超时（{timeout_seconds}s）"
            return
        with self._lock:
            for name, error in self._errors.items():
                log.warning("MCP 外部服务 %s 不可用：%s", name, error)

    async def _serve(self, ready: threading.Event) -> None:
        """Hold every server session open for the process lifetime."""
        from contextlib import AsyncExitStack

        servers = [spec for spec in self.specs if spec.enabled]
        if not servers:
            ready.set()
            await asyncio.Event().wait()
            return
        # The stack owns the context managers for the lifetime of the session.
        # Entering them with a bare ``__aenter__`` would let the underlying
        # async generators be garbage-collected, and finalising one of them
        # tears the server process down — which shows up as a bare
        # "Connection closed" with no other symptom.
        async with AsyncExitStack() as stack:
            await self._open_sessions(servers, stack)
            ready.set()
            await asyncio.Event().wait()

    async def _open_sessions(
        self, servers: list[McpServerSpec], stack: Any,
    ) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        import os

        async def _open(spec: McpServerSpec) -> None:
            try:
                env = {**os.environ, **spec.env} if spec.env else None
                params = StdioServerParameters(
                    command=spec.command,
                    args=list(spec.args),
                    env=env,
                    cwd=spec.cwd or None,
                )
                # The server's stderr is discarded: an external process must
                # not be able to scribble on our own stdout, which carries the
                # protocol stream of *our* server when we are embedded.
                reader, writer = await stack.enter_async_context(
                    stdio_client(params, errlog=open(os.devnull, "w")),
                )
                session = await stack.enter_async_context(
                    ClientSession(reader, writer),
                )
                # A server that never answers the handshake must not hang the
                # whole startup, so the connect phase is bounded per server.
                await asyncio.wait_for(
                    session.initialize(), timeout=max(5.0, spec.timeout_seconds),
                )
                listed = await asyncio.wait_for(
                    session.list_tools(), timeout=max(5.0, spec.timeout_seconds),
                )
                with self._lock:
                    self._sessions[spec.name] = session
                    self._register_tools(spec, listed.tools)
                log.info(
                    "MCP 外部服务 %s 已连接，公开 %d 个工具",
                    spec.name, len(self._tools),
                )
            except Exception as exc:  # noqa: BLE001 - one bad server must not win
                self._errors[spec.name] = f"{type(exc).__name__}: {exc}"
                log.warning("MCP 外部服务 %s 连接失败：%s", spec.name, exc)

        await asyncio.gather(*(_open(spec) for spec in servers))

    def _register_tools(self, spec: McpServerSpec, remote_tools: list[Any]) -> None:
        for tool in remote_tools:
            remote_name = str(getattr(tool, "name", "") or "")
            if not remote_name:
                continue
            if spec.allowed_tools and remote_name not in spec.allowed_tools:
                continue
            description = str(getattr(tool, "description", "") or "")
            schema = getattr(tool, "inputSchema", None) or {
                "type": "object", "properties": {},
            }
            external = ExternalTool(
                server=spec.name,
                remote_name=remote_name,
                description=description,
                input_schema=schema,
            )
            with self._lock:
                self._tools[external.qualified_name] = external

    def stop(self) -> None:
        """Close every session and stop the background loop."""
        with self._lock:
            sessions = dict(self._sessions)
            self._sessions.clear()
            self._tools.clear()
            loop = self._loop
        for session in sessions.values():
            closer = getattr(session, "__aexit__", None)
            if closer is None:
                continue
            try:
                if loop is not None and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        closer(None, None, None), loop,
                    ).result(timeout=5)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    # -- introspection -----------------------------------------------------

    def tools(self) -> list[ExternalTool]:
        with self._lock:
            return list(self._tools.values())

    def qualified_names(self) -> set[str]:
        with self._lock:
            return set(self._tools)

    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def circuit_state(self, server: str) -> str:
        breaker = self._breakers.get(server)
        return breaker.state if breaker else "closed"

    # -- catalog integration ------------------------------------------------

    def publish_to_catalog(self) -> list[str]:
        """Register the discovered tools into the public tool catalog.

        After this call the model can see the external tools and call them
        through the ordinary tool loop — there is no separate MCP branch in
        the graph, which is the point: the external tools inherit the
        capability allow-list, the cancellation check and the result limit
        because they never leave the standard path.
        """
        from tool_catalog import ToolDefinition, register_external_tools

        definitions = []
        for tool in self.tools():
            schema = {
                "type": "function",
                "function": {
                    "name": tool.qualified_name,
                    "description": tool.description
                    or f"外部 MCP 服务 {tool.server} 提供的工具 {tool.remote_name}。",
                    "parameters": tool.input_schema,
                },
            }
            definitions.append(ToolDefinition(
                name=tool.qualified_name,
                label=f"MCP:{tool.server}/{tool.remote_name}",
                result_limit=self.result_limit,
                schema=schema,
                handler=lambda args, qualified=tool.qualified_name: self.call(qualified, args),
                source=tool.server,
            ))
        register_external_tools(definitions)
        return [definition.name for definition in definitions]

    def unpublish_from_catalog(self) -> None:
        """Remove the external tools from the public catalog."""
        from tool_catalog import clear_external_tools

        clear_external_tools()

    # -- execution ---------------------------------------------------------

    def call(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Call one external tool and return its rendered text.

        The call is bounded three ways: the circuit breaker refuses to call a
        server that has been failing, the per-call deadline caps the wait, and
        a failure is reported as text so the model can continue rather than
        crashing the run.
        """
        with self._lock:
            tool = self._tools.get(qualified_name)
            session = self._sessions.get(tool.server) if tool else None
            loop = self._loop
        if tool is None:
            return f"❌ 未注册的外部工具：{qualified_name}"

        breaker = self._breakers.get(tool.server)
        if breaker is not None and not breaker.allow_request():
            return (
                f"❌ 外部 MCP 服务 {tool.server} 连续失败已熔断，"
                f"暂时不调用 {tool.remote_name}。"
            )

        if session is None or loop is None or loop.is_closed():
            # A server that is not connected is a failure of that server, so
            # it counts toward the circuit.  Otherwise a server that died
            # after startup would be retried on every single tool round
            # forever, and each attempt would pay the full timeout.
            if breaker is not None:
                breaker.record_failure()
            return f"❌ 外部 MCP 服务 {tool.server} 当前不可用。"

        spec = next((s for s in self.specs if s.name == tool.server), None)
        timeout = spec.timeout_seconds if spec else 30.0

        async def _invoke() -> Any:
            return await session.call_tool(tool.remote_name, dict(arguments or {}))

        try:
            future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
            result = future.result(timeout=timeout)
        except TimeoutError:
            if breaker is not None:
                breaker.record_failure()
            return f"❌ 外部工具 {qualified_name} 超时（{timeout:g}s）。"
        except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
            if breaker is not None:
                breaker.record_failure()
            return f"❌ 外部工具 {qualified_name} 调用失败：{type(exc).__name__}: {exc}"

        if breaker is not None:
            breaker.record_success()
        if getattr(result, "isError", False):
            return f"❌ 外部工具 {qualified_name} 返回错误：{_render_result(result)}"
        return _render_result(result)

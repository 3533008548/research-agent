"""The small declarative catalog for model-facing tools.

This is deliberately not a dynamic plugin system.  It gives the bundled
tools one public definition for their name, function-calling schema, user-safe
label and model-visible result limit.  New tools remain ordinary Python
handlers, but adding one now fails early if its execution and declaration
contracts get out of sync.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from tool_schemas import _raw_tool_schemas


@dataclass(frozen=True)
class ToolDefinition:
    """Non-secret contract for one tool the model may request."""

    name: str
    label: str
    result_limit: int
    schema: dict[str, Any]
    # External tools (from an MCP server) carry a handler, because they are not
    # implemented in this repository.  Built-in tools leave it unset and are
    # dispatched by name.
    handler: Callable[[dict[str, Any]], str] | None = None
    # The server an external tool came from, for diagnostics.
    source: str = ""


_PRESENTATION = {
    "search_papers": ("论文搜索", 3_000),
    "read_pdf": ("论文阅读", 12_000),
    "generate_paper_card": ("论文证据卡", 3_000),
    "list_paper_relations": ("论文关系查看", 6_000),
    "list_relation_candidates": ("论文关系候选", 6_000),
    "save_paper_relation": ("论文关系确认保存", 3_000),
    "delete_paper_relation": ("论文关系删除", 1_500),
    "save_research_document": ("研究档案创建", 1_500),
    "search_research_documents": ("研究档案检索", 4_000),
    "read_research_document": ("研究档案阅读", 12_000),
    "list_research_document_sections": ("研究档案章节", 3_000),
    "read_research_document_section": ("研究档案章节阅读", 8_000),
    "prepare_research_document_patch_context": ("完整补丁上下文", 32_000),
    "apply_research_document_patch": ("研究档案安全更新", 2_000),
    "list_research_document_versions": ("研究档案版本", 2_000),
    "restore_research_document_version": ("研究档案版本恢复", 1_500),
    "compare_papers_to_research_document": ("论文档案比对", 10_000),
    "read_research_document_ledger": ("证据与假设账本", 8_000),
    "apply_research_document_ledger_patch": ("更新证据与假设账本", 3_000),
    "review_research_document_innovation": ("科研档案创新性审查", 12_000),
    "review_new_paper_impact_on_research_document": ("新论文综合审查", 14_000),
    "update_experiment_project": ("复现实验确认", 2_500),
    "describe_image": ("图像分析", 2_000),
    "query_papers": ("论文检索", 5_000),
    "list_papers": ("本地论文列表", 3_000),
    "list_indexed_papers": ("索引论文列表", 3_000),
    "delete_paper": ("论文删除", 3_000),
    "update_profile": ("用户画像更新", 3_000),
    "memory_search": ("记忆检索", 1_500),
}


def _build_catalog() -> tuple[ToolDefinition, ...]:
    schemas = _raw_tool_schemas()
    declared_names = []
    definitions = []
    for schema in schemas:
        name = str(schema.get("function", {}).get("name") or "")
        declared_names.append(name)
        presentation = _PRESENTATION.get(name)
        if presentation is None:
            raise RuntimeError(f"工具目录缺少展示契约：{name or 'unknown'}")
        label, result_limit = presentation
        definitions.append(ToolDefinition(name, label, result_limit, schema))
    if len(declared_names) != len(set(declared_names)):
        raise RuntimeError("工具目录包含重复名称")
    if set(declared_names) != set(_PRESENTATION):
        raise RuntimeError("工具 schema 与展示契约不一致")
    return tuple(definitions)


TOOL_CATALOG = _build_catalog()
TOOL_NAMES = frozenset(definition.name for definition in TOOL_CATALOG)
_BY_NAME = {definition.name: definition for definition in TOOL_CATALOG}

# External tools live in their own namespace because they are discovered at
# runtime rather than declared in this repository.  Keeping them out of
# ``TOOL_CATALOG`` means the built-in consistency check above stays meaningful:
# it still proves that every declared schema has a presentation contract and
# a dispatcher, which is a statement about *this* repository's tools only.
_EXTERNAL_LOCK = threading.RLock()
_EXTERNAL_BY_NAME: dict[str, ToolDefinition] = {}


def register_external_tools(definitions: list[ToolDefinition]) -> None:
    """Register external (MCP-provided) tools into the public catalog.

    Registering rather than mutating keeps the built-in catalog immutable, so
    an external server can never weaken the contract that every built-in tool
    has a schema, a label and a result limit.
    """
    with _EXTERNAL_LOCK:
        for definition in definitions:
            _EXTERNAL_BY_NAME[definition.name] = definition


def clear_external_tools() -> None:
    """Remove every external tool, e.g. when the servers shut down."""
    with _EXTERNAL_LOCK:
        _EXTERNAL_BY_NAME.clear()


def get_tool_definition(name: str) -> ToolDefinition | None:
    """Return a tool definition, or ``None`` for an unknown name."""
    key = str(name or "")
    found = _BY_NAME.get(key)
    if found is not None:
        return found
    with _EXTERNAL_LOCK:
        return _EXTERNAL_BY_NAME.get(key)


def get_tool_schemas(allowed_tool_names: set[str] | frozenset[str] | None = None) -> list[dict]:
    """Return caller-owned schemas, optionally filtered by the capability scope."""
    allowed = None if allowed_tool_names is None else frozenset(allowed_tool_names)
    with _EXTERNAL_LOCK:
        external = list(_EXTERNAL_BY_NAME.values())
    return [
        deepcopy(definition.schema)
        for definition in (*TOOL_CATALOG, *external)
        if allowed is None or definition.name in allowed
    ]

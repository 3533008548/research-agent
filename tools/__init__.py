"""
🔧 工具实现 — 按领域分模块
"""

from tools.search import handle_search_papers, handle_query_papers, handle_list_papers, handle_list_indexed, handle_delete_paper
from tools.read_pdf import handle_read_pdf
from tools.paper_card import handle_generate_paper_card
from tools.paper_relations import (
    handle_delete_paper_relation,
    handle_list_paper_relations,
    handle_list_relation_candidates,
    handle_save_paper_relation,
)
from tools.describe import handle_describe_image
from tools.profile_tool import handle_read_profile, handle_update_profile
from cancellation import raise_if_cancelled
from tool_catalog import TOOL_NAMES, get_tool_definition


_TOOL_HANDLERS = {
    "search_papers": handle_search_papers,
    "read_pdf": handle_read_pdf,
    "generate_paper_card": handle_generate_paper_card,
    "list_paper_relations": handle_list_paper_relations,
    "list_relation_candidates": handle_list_relation_candidates,
    "save_paper_relation": handle_save_paper_relation,
    "delete_paper_relation": handle_delete_paper_relation,
    "describe_image": handle_describe_image,
    "query_papers": handle_query_papers,
    "list_papers": handle_list_papers,
    "list_indexed_papers": handle_list_indexed,
    "delete_paper": handle_delete_paper,
    "update_profile": handle_update_profile,
    "read_profile": handle_read_profile,
}

_EXECUTABLE_TOOL_NAMES = frozenset({*_TOOL_HANDLERS, "memory_search"})
if TOOL_NAMES != _EXECUTABLE_TOOL_NAMES:
    raise RuntimeError("工具目录与执行分发器不一致")


def _search_memory(args: dict, memory_store, session_id: str = "") -> str:
    """Render current-session summaries and global paper facts compactly."""
    if not memory_store:
        return "❌ 记忆模块未启用。"
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "❌ 请提供记忆搜索词。"
    summaries = memory_store.search_summaries(query, session_id, limit=3)
    triples = memory_store.search_triples(query, limit=5)
    if not summaries and not triples:
        return "📭 未找到相关记忆。"
    lines = [f"📚 记忆搜索结果 — 「{query}」:"]
    if summaries:
        lines.append("\n**当前会话摘要**")
        lines.extend(
            f"  • {item['summary']}"
            for item in summaries
        )
    if triples:
        lines.append("\n**论文事实**")
        lines.extend(
            f"  • {item['paper_title']} → {item['relation']}: {item['value']}"
            for item in triples
        )
    return "\n".join(lines)


def execute_tool(
    name: str,
    args: dict,
    paper_store=None,
    vision_model: str = "",
    profile_manager=None,
    memory_store=None,
    session_id: str = "",
    llm_client=None,
    model: str = "",
    cancel_event=None,
) -> str:
    """工具分发入口"""
    raise_if_cancelled(cancel_event, "工具调用已取消")
    if name == "memory_search":
        result = _search_memory(args, memory_store, session_id)
        raise_if_cancelled(cancel_event, "工具调用已取消")
        return result
    handler = _TOOL_HANDLERS.get(name)
    if handler:
        result = handler(
            args, paper_store=paper_store, vision_model=vision_model,
            profile_manager=profile_manager, memory_store=memory_store,
            llm_client=llm_client, model=model,
            cancel_event=cancel_event,
        )
        raise_if_cancelled(cancel_event, "工具调用已取消")
        return result
    # External (MCP-provided) tools are dispatched through the catalog rather
    # than through a second path here, so they reach the model and the caller
    # through exactly the same boundary as the built-in ones.
    definition = get_tool_definition(name)
    if definition is not None and definition.handler is not None:
        result = definition.handler(args)
        raise_if_cancelled(cancel_event, "工具调用已取消")
        return result
    return f"❌ 未知工具: {name}"

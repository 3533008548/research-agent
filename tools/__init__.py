"""
🔧 工具实现 — 按领域分模块
"""

from tools.search import handle_search_papers, handle_query_papers, handle_list_papers, handle_list_indexed, handle_delete_paper
from tools.read_pdf import handle_read_pdf
from tools.describe import handle_describe_image
from tools.profile_tool import handle_update_profile


def execute_tool(name: str, args: dict, paper_store=None, glm_api_key: str = "", profile_manager=None, memory_store=None) -> str:
    """工具分发入口"""
    handlers = {
        "search_papers": handle_search_papers,
        "read_pdf": handle_read_pdf,
        "describe_image": handle_describe_image,
        "query_papers": handle_query_papers,
        "list_papers": handle_list_papers,
        "list_indexed_papers": handle_list_indexed,
        "delete_paper": handle_delete_paper,
        "update_profile": handle_update_profile,
    }
    # memory_search 内联
    if name == "memory_search":
        if not memory_store:
            return "❌ 记忆模块未启用。"
        q = args.get("query", "")
        triples = memory_store.search_triples(q, limit=5)
        if not triples:
            return "📭 未找到相关记忆。"
        lines = [f"📚 记忆搜索结果 — 「{q}」:\n"]
        for t in triples:
            lines.append(f"  • {t['paper_title']} → {t['relation']}: {t['value']}")
        return "\n".join(lines)
    handler = handlers.get(name)
    if handler:
        return handler(args, paper_store=paper_store, glm_api_key=glm_api_key, profile_manager=profile_manager, memory_store=memory_store)
    return f"❌ 未知工具: {name}"

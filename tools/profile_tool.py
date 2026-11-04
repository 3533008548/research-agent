"""
👤 用户画像读取与更新工具
"""

def handle_read_profile(args: dict, profile_manager=None, **kw) -> str:
    if not profile_manager:
        return "❌ 画像功能未启用。"
    try:
        section = args.get("section", "")
        entries = profile_manager.read_section(section)
        return f"## {section}\n" + ("\n".join(f"- {entry}" for entry in entries) or "（暂无条目）")
    except Exception as e:
        return f"❌ 画像读取失败: {e}"


def handle_update_profile(args: dict, profile_manager=None, **kw) -> str:
    if not profile_manager:
        return "❌ 画像功能未启用。"
    action = args.get("action", "")
    content = args.get("content", "")
    if not action or not content:
        return "❌ 请提供 action 和 content。"
    try:
        changed = profile_manager.update_from_agent(
            action, content, section=args.get("section", ""),
            replacement=args.get("replacement", ""),
        )
        return f"✅ 画像已更新: {action}" if changed else "ℹ️ 画像未变更：条目已存在或内容相同。"
    except Exception as e:
        return f"❌ 画像更新失败: {e}"

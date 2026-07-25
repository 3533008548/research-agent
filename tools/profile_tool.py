"""
👤 update_profile 工具 — 自动更新用户画像
"""


def handle_update_profile(args: dict, profile_manager=None, **kw) -> str:
    if not profile_manager:
        return "❌ 画像功能未启用。"
    action = args.get("action", "")
    content = args.get("content", "")
    if not action or not content:
        return "❌ 请提供 action 和 content。"
    try:
        profile_manager.update_from_agent(action, content)
        return f"✅ 画像已更新: {action}"
    except Exception as e:
        return f"❌ 画像更新失败: {e}"

"""Model-facing confirmation writes for user-owned reproduction projects."""

from __future__ import annotations

from cancellation import raise_if_cancelled
from experiment_projects import ExperimentProjectStore
from runtime_paths import get_runtime_paths


def handle_update_experiment_project(args: dict, *, cancel_event=None, **_kwargs) -> str:
    """Write only user-confirmed facts; the tool never executes generated code."""
    raise_if_cancelled(cancel_event, "更新复现实验项目前已取消")
    project_id = str(args.get("project_id") or "").strip()
    confirmed_items = args.get("confirmed_items")
    if not project_id:
        return "❌ 请提供 experiment- 开头的实验项目 ID。"
    if not isinstance(confirmed_items, list) or not confirmed_items:
        return "❌ 请列出至少一条用户明确确认的实验前提。"
    placeholders = ("在此替换", "请填写", "未确认请删除", "例如：")
    cleaned_items = [
        str(item) for item in confirmed_items
        if str(item).strip() and not any(marker in str(item) for marker in placeholders)
    ]
    if not cleaned_items:
        return "❌ 尚未检测到用户填写的确认事实；请先补全对话框中的占位行后再发送。"
    try:
        project = ExperimentProjectStore(get_runtime_paths()).apply_confirmation(
            project_id,
            confirmed_items=cleaned_items,
            resolved_unknowns=[str(item) for item in args.get("resolved_unknowns") or []],
            summary=str(args.get("summary") or "").strip(),
        )
    except KeyError:
        return "❌ 未找到该复现实验项目。"
    except ValueError as exc:
        return f"❌ 无法更新复现实验项目：{exc}"
    raise_if_cancelled(cancel_event, "更新复现实验项目后已取消")
    unresolved = project.get("spec", {}).get("unknowns", []) if isinstance(project.get("spec"), dict) else []
    return (
        f"✅ 已更新复现实验项目：{project.get('paper_title') or project_id}\n"
        f"项目 ID：`{project_id}` · 修订 {project.get('revision')} · 状态：{project.get('status')}\n"
        f"本次写入 {len(cleaned_items)} 条用户确认；仍待确认 {len(unresolved) if isinstance(unresolved, list) else 0} 项。\n"
        "已同步更新项目规格、`configs/reproduction.json` 与 `README.md`；未执行训练或评测。"
    )

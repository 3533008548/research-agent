"""Agent-facing handlers for explicit paper-to-paper retrieval relations."""

from __future__ import annotations

from typing import Any

from paper_relations import PaperRelationStore, relation_type_label
from runtime_paths import get_runtime_paths


def _relation_store(paper_store) -> PaperRelationStore | None:
    if not paper_store:
        return None
    store = getattr(paper_store, "relation_store", None)
    # Older test doubles and externally constructed stores remain readable;
    # production PaperStore receives this same store during agent startup.
    return store or PaperRelationStore(get_runtime_paths())


def _resolve_paper(paper_store, identifier: str) -> tuple[dict[str, Any] | None, str]:
    needle = str(identifier or "").strip()
    if not needle:
        return None, "请提供论文标题或 paper_id。"
    papers = list(paper_store.list_papers())
    exact = [paper for paper in papers if needle == str(paper.get("paper_id") or "")]
    if not exact:
        exact = [
            paper for paper in papers
            if needle.casefold() == str(paper.get("title") or "").casefold()
        ]
    if len(exact) == 1:
        return exact[0], ""
    fuzzy = [
        paper for paper in papers
        if needle.casefold() in str(paper.get("paper_id") or "").casefold()
        or needle.casefold() in str(paper.get("title") or "").casefold()
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if len(fuzzy) > 1:
        choices = "；".join(str(paper.get("title") or "") for paper in fuzzy[:5])
        return None, f"找到多篇匹配论文，请改用精确标题或 paper_id：{choices}"
    return None, f"未找到已索引论文：{needle}"


def _normalize_evidence(args: dict, paper_store, source: dict, target: dict) -> tuple[list[dict] | None, str]:
    evidence = args.get("evidence")
    if not isinstance(evidence, list):
        return None, "论文关系需要 evidence 数组，并至少包含一个页码或片段锚点。"
    normalized = []
    for raw in evidence:
        if not isinstance(raw, dict):
            return None, "论文关系证据格式无效。"
        reference = str(raw.get("paper_id") or raw.get("paper_id_or_title") or "").strip()
        resolved, error = _resolve_paper(paper_store, reference)
        if error:
            return None, f"关系证据无法定位：{error}"
        if resolved is None:
            return None, "关系证据无法定位论文。"
        normalized.append({
            **raw,
            "paper_id": str(resolved["paper_id"]),
            "title": str(resolved.get("title") or ""),
        })
    source_id = str(source["paper_id"])
    target_id = str(target["paper_id"])
    if any(str(item.get("paper_id") or "") not in {source_id, target_id} for item in normalized):
        return None, "关系证据只能引用这两篇论文中的页码或片段。"
    return normalized, ""


def _render_relation(relation: dict[str, Any], *, include_evidence: bool = True) -> list[str]:
    relation_type = str(relation.get("relation_type") or "")
    lines = [
        f"- [{relation.get('relation_id', '')}] "
        f"{relation.get('source_title') or relation.get('source_paper_id')}"
        f" —{relation_type_label(relation_type)}→ "
        f"{relation.get('target_title') or relation.get('target_paper_id')}",
        f"  说明：{relation.get('note') or '未填写'}",
    ]
    if include_evidence:
        for evidence in relation.get("evidence") or []:
            location = []
            if evidence.get("page") is not None:
                location.append(f"p.{evidence['page']}")
            if evidence.get("chunk_index") is not None:
                location.append(f"chunk {evidence['chunk_index']}")
            lines.append(
                f"  证据：{evidence.get('title') or evidence.get('paper_id')}"
                f"（{' · '.join(location)}）— {evidence.get('note') or '未填写'}"
            )
    return lines


def handle_list_paper_relations(args: dict, paper_store=None, **_kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    store = _relation_store(paper_store)
    if store is None:
        return "❌ 论文关系功能未启用。"
    reference = str(args.get("paper_id_or_title") or "").strip()
    paper_id = ""
    if reference:
        paper, error = _resolve_paper(paper_store, reference)
        if error:
            return f"❌ {error}"
        paper_id = str(paper["paper_id"])
    relations = store.list(paper_id)
    if not relations:
        return "📭 尚未记录论文关系。关系只能在你确认且附有页码或片段锚点后保存。"
    title = f"与「{reference}」相关的论文关系" if reference else "已确认的论文关系"
    lines = [f"🔗 {title}（{len(relations)} 条）"]
    for relation in relations:
        lines.extend(_render_relation(relation))
    lines.append("这些关系只用于辅助检索候选与重排，不等同于论文事实或创新结论。")
    return "\n".join(lines)


def handle_save_paper_relation(args: dict, paper_store=None, **_kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    if args.get("confirmed_by_user") is not True:
        return "❌ 保存论文关系前需要用户明确确认；请先给出关系、证据锚点和拟写入说明。"
    source, source_error = _resolve_paper(paper_store, args.get("source_paper_id_or_title", ""))
    if source_error:
        return f"❌ 源论文：{source_error}"
    target, target_error = _resolve_paper(paper_store, args.get("target_paper_id_or_title", ""))
    if target_error:
        return f"❌ 目标论文：{target_error}"
    evidence, evidence_error = _normalize_evidence(args, paper_store, source, target)
    if evidence_error:
        return f"❌ {evidence_error}"
    store = _relation_store(paper_store)
    if store is None:
        return "❌ 论文关系功能未启用。"
    try:
        relation, action = store.upsert(
            source_paper=source,
            target_paper=target,
            relation_type=str(args.get("relation_type") or ""),
            note=str(args.get("note") or ""),
            evidence=evidence or [],
            relation_id=str(args.get("relation_id") or ""),
        )
    except ValueError as exc:
        return f"❌ 无法保存论文关系：{exc}"
    action_text = "已更新" if action == "updated" else "已保存"
    return "\n".join([f"✅ {action_text}论文关系：", *_render_relation(relation)])


def handle_delete_paper_relation(args: dict, paper_store=None, **_kw) -> str:
    if not paper_store:
        return "❌ RAG 功能未启用。"
    if args.get("confirmed_by_user") is not True:
        return "❌ 删除论文关系前需要用户明确确认。"
    store = _relation_store(paper_store)
    if store is None:
        return "❌ 论文关系功能未启用。"
    try:
        deleted = store.delete(str(args.get("relation_id") or ""))
    except ValueError as exc:
        return f"❌ 无法删除论文关系：{exc}"
    return "🗑️ 已删除论文关系。" if deleted else "❌ 未找到该论文关系。"

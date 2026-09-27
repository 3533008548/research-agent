"""Agent-facing handlers for explicit paper-to-paper retrieval relations."""

from __future__ import annotations

from typing import Any

from paper_artifacts import TITLE_MATCH_THRESHOLD, title_match_score
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
    """Resolve one indexed paper by id or title.

    Files are renamed for readability, so a literal title comparison fails on
    punctuation or truncation.  Candidates are scored and the best one wins;
    demanding a unique match made an indexed paper unreachable whenever a
    near-duplicate title existed.
    """
    needle = str(identifier or "").strip()
    if not needle:
        return None, "请提供论文标题或 paper_id。"
    papers = list(paper_store.list_papers() or [])
    if not papers:
        return None, "论文库为空，请先 read_pdf 阅读并索引论文。"
    for paper in papers:
        if needle == str(paper.get("paper_id") or ""):
            return paper, ""
    scored = sorted(
        (
            (title_match_score(needle, str(paper.get("title") or "")), index, paper)
            for index, paper in enumerate(papers)
        ),
        key=lambda entry: (-entry[0], entry[1]),
    )
    best_score, _, best = scored[0]
    if best is None or best_score < TITLE_MATCH_THRESHOLD:
        return None, f"未找到已索引论文：{needle}"
    return best, ""


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
    try:
        relations = store.list(paper_id)
    except ValueError as exc:
        # Unreadable relation data must not take down the rest of retrieval:
        # expansion already degrades on its own, so listing should too.
        return f"⚠️ 论文关系数据暂不可用（{exc}）。检索已自动跳过关系扩展，其余功能不受影响。"
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


def _review_store(paper_store) -> Any | None:
    reviewer = getattr(paper_store, "relation_reviewer", None)
    if reviewer is not None:
        return reviewer.store
    try:
        from paper_relation_review import PaperRelationReviewStore

        return PaperRelationReviewStore(get_runtime_paths())
    except Exception:  # noqa: BLE001 - review state is optional
        return None


def handle_list_relation_candidates(args: dict, paper_store=None, **_kw) -> str:
    """Show automatically detected relations waiting for user confirmation."""
    if not paper_store:
        return "❌ RAG 功能未启用。"
    store = _review_store(paper_store)
    if store is None:
        return "❌ 论文关系审查功能未启用。"
    paper_id = ""
    reference = str(args.get("paper_id_or_title") or "").strip()
    if reference:
        paper, error = _resolve_paper(paper_store, reference)
        if error:
            return f"❌ {error}"
        paper_id = str(paper["paper_id"])
    status = str(args.get("status") or "pending").strip() or "pending"
    try:
        candidates = store.list_candidates(paper_id, status=status)
    except Exception as exc:  # noqa: BLE001 - derived state must not break listing
        return f"⚠️ 论文关系候选暂不可用（{type(exc).__name__}: {exc}）。"
    if not candidates:
        return f"📭 没有{status}状态的论文关系候选。"
    lines = [f"🔗 论文关系候选（{status}，共 {len(candidates)} 条）"]
    for candidate in candidates:
        lines.extend([
            f"- [{candidate.get('candidate_id', '')}] "
            f"{candidate.get('source_title')} —{relation_type_label(candidate.get('relation_type'))}→ "
            f"{candidate.get('target_title')}",
            f"  说明：{candidate.get('note') or '未填写'}",
            f"  检测方式：{candidate.get('detector') or '未知'}｜置信度：{candidate.get('confidence')}",
        ])
        for evidence in (candidate.get("evidence") or [])[:2]:
            location = []
            if evidence.get("page") is not None:
                location.append(f"p.{evidence['page']}")
            if evidence.get("chunk_index") is not None:
                location.append(f"chunk {evidence['chunk_index']}")
            lines.append(
                f"  证据：{' · '.join(location) or '未定位'} — {evidence.get('note') or '未填写'}"
            )
    lines.append(
        "候选由自动审查生成，**未经确认**。确认时用 save_paper_relation 写入，"
        "它会照常要求 confirmed_by_user 与页码/片段证据。"
    )
    return "\n".join(lines)


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

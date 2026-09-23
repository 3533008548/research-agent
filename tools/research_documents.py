"""Model-facing tools for durable, section-safe research dossiers."""

from __future__ import annotations

import re

from cancellation import raise_if_cancelled
from research_documents import RESEARCH_DOSSIER_SECTIONS, ResearchDocumentStore
from runtime_paths import get_runtime_paths


def _store() -> ResearchDocumentStore:
    return ResearchDocumentStore(get_runtime_paths())


def _limit(value, default: int = 5) -> int:
    try:
        return max(1, min(int(value), 10))
    except (TypeError, ValueError):
        return default


def handle_save_research_document(args: dict, *, cancel_event=None, **_kw) -> str:
    """Create a dossier instead of placing it in memory or profile."""
    raise_if_cancelled(cancel_event, "保存研究文档前已取消")
    try:
        document = _store().save(
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            document_id=str(args.get("document_id") or ""),
        )
    except ValueError as exc:
        return f"❌ 保存研究文档失败：{exc}"
    raise_if_cancelled(cancel_event, "保存研究文档后已取消")
    return (
        f"✅ 已创建研究档案：{document['title']}\n"
        f"文档 ID：`{document['document_id']}`（第 {document['revision']} 版）\n"
        f"Markdown：{document['markdown_path']}\n"
        f"Word：{document['docx_path']}\n"
        "已按固定四章节模板保存；该档案不会写入用户画像或会话摘要。"
    )


def handle_search_research_documents(args: dict, **_kw) -> str:
    query = str(args.get("query") or "").strip()
    documents = _store().search(query, limit=_limit(args.get("limit")))
    if not documents:
        return "📭 未找到研究档案。"
    label = f"「{query}」" if query else "最近更新"
    lines = [f"📁 研究档案 — {label}（{len(documents)} 份）"]
    for index, document in enumerate(documents, 1):
        lines.append(
            f"{index}. {document['title']}\n"
            f"   ID：`{document['document_id']}` · 第 {document['revision']} 版 · 更新：{document['updated_at']}\n"
            f"   摘要：{document.get('excerpt') or document.get('summary') or '（无摘要）'}"
        )
    lines.append("如需修改，请先调用 `list_research_document_sections`，再读取目标章节。")
    return "\n".join(lines)


def handle_read_research_document(args: dict, **_kw) -> str:
    document_id = str(args.get("document_id") or "").strip()
    document = _store().read(document_id)
    if document is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    return (
        f"# {document['title']}\n\n"
        f"> 文档 ID：`{document['document_id']}` · 第 {document['revision']} 版 · 更新：{document['updated_at']}\n\n"
        f"{document['content']}\n\n"
        f"Word 导出：{document['docx_path']}"
    )


def handle_list_research_document_sections(args: dict, **_kw) -> str:
    """List the stable sections without injecting a whole dossier into context."""
    document_id = str(args.get("document_id") or "").strip()
    document = _store().list_sections(document_id)
    if document is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    lines = [
        f"📑 {document['title']}（ID：`{document['document_id']}` · 第 {document['revision']} 版）",
        "修改前请选择一个章节读取；不要用完整文档重新生成后覆盖。",
    ]
    for section in document["sections"]:
        lines.append(
            f"- `{section['section_id']}` · {section['heading']} · "
            f"{section['content_length']} 字符 · 摘要：{section['summary'] or '（待补充）'}"
        )
    return "\n".join(lines)


def handle_read_research_document_section(args: dict, **_kw) -> str:
    """Read one section and expose the optimistic-concurrency values for a patch."""
    document_id = str(args.get("document_id") or "").strip()
    section_id = str(args.get("section_id") or "").strip()
    try:
        section = _store().read_section(document_id, section_id)
    except ValueError as exc:
        return f"❌ 读取研究档案章节失败：{exc}"
    if section is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    return (
        f"# {section['title']} — {section['heading']}\n\n"
        f"> 文档 ID：`{section['document_id']}` · 第 {section['revision']} 版 · "
        f"章节 ID：`{section['section_id']}` · 哈希：`{section['content_hash']}`\n\n"
        f"{section['content']}\n\n"
        "如需写入，只能基于以上 revision 和 hash 调用 `apply_research_document_patch`。"
    )


def handle_prepare_research_document_patch_context(args: dict, **_kw) -> str:
    """Read all four sections once for a user-confirmed, atomic dossier patch.

    This deliberately packages the four existing section reads into one model
    tool result.  It is used only after the user has confirmed that all listed
    edits should be written, so the following model turn can submit one checked
    patch instead of spending several tool/LLM rounds rediscovering the same
    document state.
    """
    document_id = str(args.get("document_id") or "").strip()
    store = _store()
    document = store.list_sections(document_id)
    if document is None:
        return "📭 未找到该研究档案。"

    sections = []
    for section_id, heading in RESEARCH_DOSSIER_SECTIONS:
        section = store.read_section(document_id, section_id)
        if section is None:
            return "❌ 读取科研档案失败，请稍后重试。"
        sections.append((section_id, heading, section))

    # The normal per-section reader is limited to 8k characters.  Keep this
    # aggregate beneath the dedicated tool contract as well; exceeding it must
    # fail explicitly rather than silently dropping a tail and overwriting it.
    content_chars = sum(len(str(section["content"])) for _sid, _heading, section in sections)
    if content_chars > 28_000:
        lengths = "、".join(f"{heading} {len(str(section['content']))} 字符" for _sid, heading, section in sections)
        return (
            "⚠️ 本次完整写入未执行：四章节正文共 "
            f"{content_chars} 字符，超过一次性写入上下文上限。\n"
            f"各章节长度：{lengths}。\n"
            "请改为确认具体章节的局部修改；不得基于被截断的全文提交补丁。"
        )

    lines = [
        f"# {document['title']} — 已确认写入的完整补丁上下文",
        f"> 文档 ID：`{document_id}` · 第 {document['revision']} 版",
        "以下四章均完整返回。现在只应调用一次 `apply_research_document_patch`，"
        "使用该 revision 与每章 hash 写入所有已确认修改；不要重新检索论文或再次读取档案。",
    ]
    for section_id, heading, section in sections:
        lines.extend([
            f"\n## {heading}",
            f"> 章节 ID：`{section_id}` · 哈希：`{section['content_hash']}`",
            str(section["content"]),
        ])
    return "\n\n".join(lines)


def handle_apply_research_document_patch(args: dict, *, cancel_event=None, **_kw) -> str:
    """Apply a narrow checked patch; full-document rewrites are intentionally absent."""
    raise_if_cancelled(cancel_event, "更新研究档案前已取消")
    try:
        document = _store().apply_patch(
            str(args.get("document_id") or "").strip(),
            base_revision=args.get("base_revision"),
            operations=args.get("operations"),
        )
    except ValueError as exc:
        return f"❌ 更新研究档案失败：{exc}"
    raise_if_cancelled(cancel_event, "更新研究档案后已取消")
    changes = "、".join(f"{item['heading']}（{item['action']}）" for item in document["changes"])
    return (
        f"✅ 已安全更新研究档案：{document['title']}（第 {document['revision']} 版）\n"
        f"变更：{changes}\n"
        "写入前的完整版本已保存为历史快照；可用 `list_research_document_versions` 查看和恢复。"
    )


def handle_list_research_document_versions(args: dict, **_kw) -> str:
    document_id = str(args.get("document_id") or "").strip()
    versions = _store().list_versions(document_id, limit=_limit(args.get("limit"), default=10))
    if not versions:
        return "📭 未找到该研究档案，或该档案暂无版本记录。"
    lines = [f"🕘 研究档案版本（`{document_id}`）"]
    for item in versions:
        label = "当前版本" if item["current"] else "可恢复快照"
        lines.append(f"- 第 {item['revision']} 版 · {label} · {item['updated_at']}")
    return "\n".join(lines)


def handle_restore_research_document_version(args: dict, *, cancel_event=None, **_kw) -> str:
    """Restore only after explicit user confirmation; restoration itself remains versioned."""
    raise_if_cancelled(cancel_event, "恢复研究档案前已取消")
    try:
        document = _store().restore_version(
            str(args.get("document_id") or "").strip(),
            base_revision=args.get("base_revision"),
            revision=args.get("revision"),
        )
    except (TypeError, ValueError) as exc:
        return f"❌ 恢复研究档案失败：{exc}"
    raise_if_cancelled(cancel_event, "恢复研究档案后已取消")
    return (
        f"✅ 已将第 {document['restored_from_revision']} 版恢复为当前第 {document['revision']} 版。\n"
        "恢复前的当前内容也已保留为历史快照。"
    )


def handle_read_research_document_ledger(args: dict, **_kw) -> str:
    """Read claims, evidence links and falsification conditions without prose."""
    document_id = str(args.get("document_id") or "").strip()
    ledger = _store().read_ledger(document_id)
    if ledger is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    summary = ledger["summary"]
    lines = [
        f"📒 {ledger['title']} — 证据与假设账本（账本第 {ledger['ledger_revision']} 版）",
        f"共 {summary['items']} 项：已支持 {summary['supported']} · 存在争议 {summary['contested']} · 章节链接失效 {summary['stale_links']}。",
    ]
    if not ledger["items"]:
        lines.append("账本尚为空。先阅读相关章节和论文证据，提出条目，再在用户确认后写入。")
    for item in ledger["items"]:
        freshness = "当前" if item["section_current"] else "章节已改动，需复核"
        lines.append(
            f"- `{item['item_id']}` · {item['heading']} · {item['kind']} / {item['status']} · {freshness}\n"
            f"  陈述：{item['statement']}"
        )
        if item["falsification"]:
            lines.append(f"  可证伪条件：{item['falsification']}")
        for source in item["evidence"]:
            anchor = _evidence_anchor(source)
            lines.append(f"  证据（{source.get('relation')}）：{anchor}{'；' + source['note'] if source.get('note') else ''}")
    lines.append("写入时必须携带当前账本修订号和目标章节哈希；不要把合理推测标成 supported。")
    return "\n".join(lines)


def handle_apply_research_document_ledger_patch(args: dict, *, cancel_event=None, **_kw) -> str:
    """Persist only user-confirmed research claims and their locatable evidence."""
    raise_if_cancelled(cancel_event, "更新证据与假设账本前已取消")
    try:
        ledger = _store().apply_ledger_patch(
            str(args.get("document_id") or "").strip(),
            base_revision=args.get("base_revision"),
            operations=args.get("operations"),
        )
    except ValueError as exc:
        return f"❌ 更新证据与假设账本失败：{exc}"
    raise_if_cancelled(cancel_event, "更新证据与假设账本后已取消")
    changes = "、".join(f"{item['action']}：{_compact(item['statement'], 80)}" for item in ledger["changes"])
    return (
        f"✅ 已更新证据与假设账本（第 {ledger['ledger_revision']} 版）\n"
        f"{changes}\n"
        "这不会改写科研档案正文；正文修改仍必须走章节局部补丁。"
    )


def handle_compare_papers_to_research_document(args: dict, *, paper_store=None, **_kw) -> str:
    """Scan every dossier section against one or more indexed papers, read-only.

    This is evidence-candidate generation, not a semantic judgement or a write
    action.  The chat model uses the returned anchors to explain overlap and
    possible borrowing without ever replacing the dossier.
    """
    document_id = str(args.get("document_id") or "").strip()
    requested = args.get("paper_ids_or_titles") or []
    if not isinstance(requested, list):
        return "❌ 论文参数必须是论文 ID 或标题组成的列表。"
    requested = [str(item).strip() for item in requested if str(item).strip()]
    if not requested:
        return "❌ 至少指定一篇已索引论文。"
    if len(requested) > 5:
        return "❌ 一次最多比对 5 篇论文。"
    document = _store().read(document_id)
    if document is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    if paper_store is None:
        return "❌ 本地论文库未启用，无法执行论文比对。"
    papers = _resolve_papers(paper_store, requested)
    if isinstance(papers, str):
        return papers
    sections = document.get("sections") or []
    section_bodies = {
        item["section_id"]: _store().read_section(document_id, item["section_id"])["content"]
        for item in sections
    }
    units = [
        (section_id, _SECTION_TEXT[section_id], piece)
        for section_id, body in section_bodies.items()
        for piece in _logical_units(body)
    ]
    lines = [
        f"📊 论文 × 科研档案只读比对：{document['title']}",
        f"覆盖范围：4 个固定章节、{len(units)} 个档案逻辑单元；不会修改档案。",
        "以下是词汇重合证据候选，不是“已证实的研究重叠”；未命中也不能证明不存在概念关系。",
    ]
    for paper in papers:
        chunks = list(paper_store.get_paper_chunks(paper["paper_id"]) or [])
        if not chunks:
            lines.append(f"\n## {paper['title']}\n该论文没有可用的已索引切块，无法完成比对。")
            continue
        lines.append(f"\n## {paper['title']}（`{paper['paper_id']}`）")
        lines.append(f"已扫描论文 {len(chunks)} 个切块 × 档案 {len(units)} 个逻辑单元。")
        for section_id, heading in _SECTION_TEXT.items():
            section_units = [unit for unit in units if unit[0] == section_id]
            candidates = _rank_evidence(section_units, chunks, limit=2)
            lines.append(f"\n### {heading}")
            if not candidates:
                lines.append("未发现明显词汇重合候选；仍需结合语义和研究目标判断。")
                continue
            for candidate in candidates:
                chunk = candidate["chunk"]
                page = chunk.get("page")
                page_label = f"第 {page} 页" if isinstance(page, int) and page > 0 else "页码未知"
                lines.append(
                    f"- 词汇重合 {candidate['score']:.2f} · {page_label} · {chunk.get('section') or '未标注'}\n"
                    f"  档案：{_compact(candidate['unit'], 180)}\n"
                    f"  论文：{_compact(str(chunk.get('text') or ''), 220)}"
                )
    lines.append(
        "\n请基于上述证据逐篇说明：①可能重叠的研究内容；②可借鉴之处；③差异或风险；④仍不能判断的事项。"
        "如需改档案，先征得用户确认，再只读取并补丁修改对应章节。"
    )
    return "\n".join(lines)


def handle_review_research_document_innovation(args: dict, *, paper_store=None, **_kw) -> str:
    """Build a five-axis evidence pack for a read-only novelty review.

    The tool deliberately returns evidence candidates and review constraints,
    rather than declaring a research idea novel.  The model must evaluate the
    supplied paper anchors and explicitly distinguish a possible difference
    from a validated innovation claim.
    """
    document_id = str(args.get("document_id") or "").strip()
    requested = args.get("paper_ids_or_titles") or []
    if not isinstance(requested, list):
        return "❌ 论文参数必须是论文 ID 或标题组成的列表。"
    requested = [str(item).strip() for item in requested if str(item).strip()]
    if not requested or len(requested) > 5:
        return "❌ 请指定一至五篇已索引论文。"
    store = _store()
    document = store.read(document_id)
    ledger = store.read_ledger(document_id)
    if document is None or ledger is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    if paper_store is None:
        return "❌ 本地论文库未启用，无法执行创新性审查。"
    papers = _resolve_papers(paper_store, requested)
    if isinstance(papers, str):
        return papers
    chunks = [
        chunk
        for paper in papers
        for chunk in list(paper_store.get_paper_chunks(paper["paper_id"]) or [])
    ]
    if not chunks:
        return "❌ 所选论文没有可用的已索引切块，无法执行创新性审查。"

    axes = _innovation_axes(store, document_id, ledger)
    paper_ids = [str(paper["paper_id"]) for paper in papers]
    summary = ledger["summary"]
    lines = [
        f"🧪 科研档案创新性审查证据包：{document['title']}",
        f"范围：{len(papers)} 篇论文、{len(chunks)} 个论文切块、五个审查维度。"
        f"账本：{summary['items']} 项（已支持 {summary['supported']}，争议 {summary['contested']}，失效链接 {summary['stale_links']}）。",
        "词汇扫描覆盖各维度关联的全部档案逻辑单元和全部所选论文切块；混合检索候选用于补充语义相近但措辞不同的段落。",
    ]
    warnings: list[str] = []
    for axis in axes:
        lexical = _rank_evidence(
            [(axis["key"], axis["label"], unit) for unit in axis["units"]], chunks, limit=2,
        )
        semantic, warning = _semantic_candidates(paper_store, axis["query"], paper_ids)
        if warning:
            warnings.append(warning)
        candidates = _merge_review_candidates(lexical, semantic, limit=3)
        lines.append(f"\n## {axis['label']}")
        lines.append(f"档案线索：{_compact(axis['query'], 300)}")
        if not candidates:
            lines.append("未得到足够的可定位候选；这属于证据不足，不表示不存在相同或相近工作。")
            continue
        for candidate in candidates:
            chunk = candidate["chunk"]
            page = chunk.get("page")
            page_label = f"第 {page} 页" if isinstance(page, int) and page > 0 else "页码未知"
            lines.append(
                f"- {candidate['source']} · {page_label} · {chunk.get('section') or '未标注'}\n"
                f"  论文：{_compact(str(chunk.get('text') or ''), 240)}"
            )
    if warnings:
        lines.append("\n检索提示：" + "；".join(dict.fromkeys(warnings))[:600])
    lines.append(
        "\n请据此完成审查，但不要直接声称“有创新”。对每个候选点必须依次写："
        "①已有工作的可确认基线；②你的方案的可观察差异；③该差异成立的前提及反证；"
        "④最低成本的证伪/区分实验；⑤结论状态（仅可能差异、证据不足、或有待核验的创新候选）。"
        "除非用户明确确认，否则不得写入科研档案或证据账本。"
    )
    return "\n".join(lines)


def handle_review_new_paper_impact_on_research_document(
    args: dict, *, paper_store=None, cancel_event=None, **_kw,
) -> str:
    """Review one paper's dossier impact from one shared read-only snapshot.

    This deliberately combines the three questions that normally follow a
    newly imported paper: possible overlap, feasibility impact and novelty
    candidates.  It avoids asking the chat model to sequence several large
    dossier scans, while leaving all judgement and any later edit to the
    model-and-user confirmation flow.
    """
    document_id = str(args.get("document_id") or "").strip()
    paper_identifier = str(args.get("paper_id_or_title") or "").strip()
    if not paper_identifier:
        return "❌ 请指定一篇已索引论文的精确标题或 paper_id；不能按“最新论文”猜测。"
    if paper_store is None:
        return "❌ 本地论文库未启用，无法执行新论文综合审查。"

    store = _store()
    document = store.read(document_id)
    ledger = store.read_ledger(document_id)
    if document is None or ledger is None:
        return "📭 未找到该研究档案。请先搜索并使用返回的文档 ID。"
    papers = _resolve_papers(paper_store, [paper_identifier])
    if isinstance(papers, str):
        return papers
    paper = papers[0]
    chunks = list(paper_store.get_paper_chunks(paper["paper_id"]) or [])
    if not chunks:
        return "❌ 所选论文没有可用的已索引切块，无法执行综合审查。"

    raise_if_cancelled(cancel_event, "读取新论文综合审查材料前已取消")
    section_bodies = {
        section_id: str(store.read_section(document_id, section_id)["content"])
        for section_id in _SECTION_TEXT
    }
    units_by_section = {
        section_id: [
            (section_id, _SECTION_TEXT[section_id], unit)
            for unit in _analysis_units(body)
        ]
        for section_id, body in section_bodies.items()
    }

    lines = [
        f"🧭 新论文 × 科研档案综合影响审查：{document['title']}",
        f"论文：{paper['title']}（`{paper['paper_id']}`）",
        f"材料范围：4 个档案章节、{len(chunks)} 个论文切块、账本 {ledger['summary']['items']} 项。",
        "这是只读证据包：用于生成修改建议，不会修改档案、账本或版本。",
        "证据未命中只表示本轮未找到足够候选，不表示不存在重叠、不可行或没有创新。",
        "\n## 研究内容重叠与可借鉴",
    ]
    for section_id, heading in _SECTION_TEXT.items():
        raise_if_cancelled(cancel_event, "扫描档案与新论文的重叠证据时已取消")
        candidates = _rank_evidence(units_by_section[section_id], chunks, limit=1)
        lines.append(f"\n### {heading}")
        if not candidates:
            lines.append("未发现明显词汇重合候选；仍需结合语义、研究目标和实验设置判断。")
            continue
        lines.extend(_render_evidence_candidates(candidates, unit_limit=160, chunk_limit=220))

    lines.append("\n## 研究方案与可行性影响")
    # Feasibility evidence is grounded in the dedicated methodology chapter.
    # The outlook section often includes a bibliography or a to-do list, both
    # of which can share vocabulary with a paper but do not establish whether
    # the proposed approach is executable.
    feasibility_units = units_by_section["approach_feasibility"]
    feasibility = _rank_evidence(feasibility_units, chunks, limit=2)
    if feasibility:
        lines.extend(_render_evidence_candidates(feasibility, unit_limit=220, chunk_limit=260))
    else:
        lines.append("未发现直接重合候选；请重点核验方法假设、数据/仿真条件、对比基线与评价指标是否可复用。")
    lines.append(
        "请基于这些锚点说明：新论文带来的前提冲突、可复用设置、缺失验证，以及是否需要调整实验或资源计划。"
    )

    lines.append("\n## 创新性候选（五维）")
    warnings: list[str] = []
    for axis in _innovation_axes(store, document_id, ledger, section_bodies=section_bodies):
        raise_if_cancelled(cancel_event, "扫描创新性候选时已取消")
        lexical = _rank_evidence(
            [(axis["key"], axis["label"], unit) for unit in axis["units"]], chunks, limit=1,
        )
        semantic, warning = _semantic_candidates(
            paper_store, axis["query"], [str(paper["paper_id"])],
        )
        if warning:
            warnings.append(warning)
        candidates = _merge_review_candidates(lexical, semantic, limit=2)
        lines.append(f"\n### {axis['label']}")
        lines.append(f"档案线索：{_compact(str(axis['query']), 220)}")
        if not candidates:
            lines.append("证据不足；不能据此得出不存在相近工作的结论。")
            continue
        for candidate in candidates:
            chunk = candidate["chunk"]
            lines.append(
                f"- {candidate['source']} · {_paper_location(chunk)} · {chunk.get('section') or '未标注'}\n"
                f"  论文：{_compact(str(chunk.get('text') or ''), 220)}"
            )
    if warnings:
        lines.append("\n检索提示：" + "；".join(dict.fromkeys(warnings))[:500])
    lines.extend([
        "\n## 本轮输出要求",
        "请将以上证据整理为：①可能重叠或可借鉴点；②可行性影响与待核验条件；③创新性候选、反证与最低成本区分实验；④逐章节拟修改项。",
        "不要把候选差异说成已证实创新，不要调用任何写入工具。最后请用户明确确认哪些章节、哪些拟修改项可以写入。",
    ])
    return "\n".join(lines)


_SECTION_TEXT = {
    "background": "研究背景与研究现状",
    "content_innovation": "研究内容与创新",
    "approach_feasibility": "研究方案与可行性",
    "outlook_plan": "研究展望与计划",
}


def _resolve_papers(paper_store, requested: list[str]) -> list[dict] | str:
    available = list(paper_store.list_papers() or [])
    selected = []
    for needle in requested:
        folded = needle.casefold()
        exact = [item for item in available if str(item.get("paper_id") or "") == needle or str(item.get("title") or "").casefold() == folded]
        matches = exact or [item for item in available if folded in str(item.get("paper_id") or "").casefold() or folded in str(item.get("title") or "").casefold()]
        if len(matches) != 1:
            options = "；".join(f"{item.get('title')}（{item.get('paper_id')}）" for item in matches[:5])
            return f"❌ 无法唯一定位论文“{needle}”。{'候选：' + options if options else '请先调用 list_papers 获取论文 ID。'}"
        selected.append(matches[0])
    return selected


def _innovation_axes(
    store: ResearchDocumentStore,
    document_id: str,
    ledger: dict,
    *,
    section_bodies: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    if section_bodies is None:
        section_bodies = {
            section_id: str(store.read_section(document_id, section_id)["content"])
            for section_id in _SECTION_TEXT
        }
    items = list(ledger.get("items") or [])

    def claim_units(kinds: set[str]) -> list[str]:
        return [str(item.get("statement") or "") for item in items if str(item.get("kind") or "") in kinds]

    def axis(key: str, label: str, bodies: list[str], claims: list[str]) -> dict[str, object]:
        units = [piece for body in bodies for piece in _analysis_units(body)] + [claim for claim in claims if claim]
        units = units or ["待补充。"]
        return {"key": key, "label": label, "units": units, "query": _compact("\n".join(units), 1_200)}

    return [
        axis("question", "研究问题", [section_bodies["background"]], claim_units({"research_question"})),
        axis("hypothesis", "核心假设", [section_bodies["content_innovation"]], claim_units({"hypothesis"})),
        axis("mechanism", "方法机制", [section_bodies["content_innovation"]], claim_units({"innovation_candidate"})),
        axis("conditions", "适用条件与可行性", [section_bodies["approach_feasibility"]], claim_units({"decision"})),
        axis("evaluation", "评价与研究计划", [section_bodies["approach_feasibility"], section_bodies["outlook_plan"]], []),
    ]


def _semantic_candidates(paper_store, query: object, paper_ids: list[str]) -> tuple[list[dict], str]:
    """Reuse the existing hybrid RAG path; lexical scanning remains the fallback."""
    search = getattr(paper_store, "query_with_timeout", None)
    if not callable(search):
        return [], "本地论文库未提供混合检索，本次只使用全量词汇扫描。"
    try:
        results, warning = search(str(query), top_k=3, paper_ids=paper_ids, timeout_seconds=2.0)
    except Exception as exc:
        return [], f"混合检索不可用，已保留词汇扫描：{type(exc).__name__}"
    return [dict(item) for item in results or [] if isinstance(item, dict)], str(warning or "")


def _merge_review_candidates(lexical: list[dict], semantic: list[dict], *, limit: int) -> list[dict]:
    candidates = []
    seen = set()
    for source, values in (("全量词汇扫描", lexical), ("混合检索候选", semantic)):
        for value in values:
            chunk = dict(value.get("chunk") or value)
            key = (str(chunk.get("paper_id") or ""), int(chunk.get("chunk_index") or 0))
            if key in seen or not str(chunk.get("text") or "").strip():
                continue
            seen.add(key)
            candidates.append({"source": source, "chunk": chunk})
            if len(candidates) >= limit:
                return candidates
    return candidates


def _evidence_anchor(source: dict) -> str:
    title = str(source.get("title") or source.get("paper_id") or "未命名论文")
    locator = []
    if source.get("page"):
        locator.append(f"第 {source['page']} 页")
    if source.get("chunk_index") is not None:
        locator.append(f"切块 {source['chunk_index']}")
    return title + (f"（{' · '.join(locator)}）" if locator else "")


def _logical_units(text: str, limit: int = 900) -> list[str]:
    """Keep every non-empty paragraph while bounding scoring work."""
    units = []
    for paragraph in str(text or "").split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > limit:
            cut = paragraph.rfind("。", 0, limit)
            if cut < max(100, limit // 3):
                cut = paragraph.rfind(" ", 0, limit)
            if cut < max(100, limit // 3):
                cut = limit
            units.append(paragraph[:cut + 1].strip())
            paragraph = paragraph[cut + 1:].strip()
        if paragraph:
            units.append(paragraph)
    return units or ["待补充。"]


def _analysis_units(text: str) -> list[str]:
    """Exclude a standalone bibliography when comparing research content.

    A bibliography is useful provenance, but matching its author/title words
    against a newly read paper is not evidence of method overlap or
    feasibility.  If a section contains only bibliography-like material, keep
    it as a fallback so an otherwise empty dossier remains inspectable.
    """
    units = _logical_units(text)
    substantive = [unit for unit in units if not _looks_like_bibliography(unit)]
    return substantive or units


def _looks_like_bibliography(unit: str) -> bool:
    normalized = str(unit or "").casefold()
    if "参考文献" in normalized or "references" in normalized:
        return True
    numbered_items = len(re.findall(r"(?:^|\n)\s*\d{1,3}[.)、]", str(unit)))
    years = len(re.findall(r"\b(?:19|20)\d{2}\b", str(unit)))
    return numbered_items >= 2 and years >= 2


def _tokens(text: str) -> set[str]:
    import re
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", text.lower())
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    return set(latin + [cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))])


def _rank_evidence(units: list[tuple[str, str, str]], chunks: list[dict], *, limit: int) -> list[dict]:
    ranked = []
    for _, _, unit in units:
        unit_tokens = _tokens(unit)
        if not unit_tokens:
            continue
        for chunk in chunks:
            chunk_text = str(chunk.get("text") or "")
            chunk_tokens = _tokens(chunk_text)
            shared = unit_tokens & chunk_tokens
            if len(shared) < 2:
                continue
            score = len(shared) / max(1, (len(unit_tokens) * len(chunk_tokens)) ** 0.5)
            ranked.append({"score": score, "unit": unit, "chunk": chunk})
    ranked.sort(key=lambda item: (-item["score"], str(item["chunk"].get("chunk_index") or "")))
    result = []
    seen = set()
    for item in ranked:
        key = (str(item["chunk"].get("paper_id") or ""), int(item["chunk"].get("chunk_index") or 0))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _paper_location(chunk: dict) -> str:
    page = chunk.get("page")
    return f"第 {page} 页" if isinstance(page, int) and page > 0 else "页码未知"


def _render_evidence_candidates(
    candidates: list[dict], *, unit_limit: int, chunk_limit: int,
) -> list[str]:
    """Render compact, locatable lexical evidence for a dossier review."""
    lines = []
    for candidate in candidates:
        chunk = candidate["chunk"]
        lines.append(
            f"- 词汇重合 {candidate['score']:.2f} · {_paper_location(chunk)} · "
            f"{chunk.get('section') or '未标注'}\n"
            f"  档案：{_compact(candidate['unit'], unit_limit)}\n"
            f"  论文：{_compact(str(chunk.get('text') or ''), chunk_limit)}"
        )
    return lines


def _compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"

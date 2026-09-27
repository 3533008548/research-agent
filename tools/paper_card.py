"""Model-facing paper evidence-card tool built on local managed PDFs."""

from __future__ import annotations

from pathlib import Path

from cancellation import RequestCancelledError
from llm_client import RequestPolicy, RequestPriority
from paper_artifacts import (
    create_source_map,
    build_source_map_from_document,
    fallback_paper_card,
    load_document_map,
    paper_card_prompt,
    select_evidence_blocks,
    TITLE_MATCH_THRESHOLD,
    title_match_key,
    title_match_score,
    update_document_map_source_file,
    write_paper_artifact,
)
from runtime_paths import get_runtime_paths


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _find_paper(paper_store, query: str) -> tuple[dict | None, list[dict], str]:
    """Locate one indexed paper by id or title.

    Files are renamed for readability, so a literal title comparison fails on
    punctuation, truncation or casing.  Candidates are scored instead, and the
    best one wins: demanding a unique match made an already-indexed paper
    unfindable whenever two similar titles existed.
    """
    papers = list(paper_store.list_papers() or []) if paper_store else []
    if not papers:
        return None, [], ""
    needle = str(query or "").strip()
    for item in papers:
        if needle and needle == str(item.get("paper_id") or ""):
            return item, papers, "paper_id"
    scored = [
        (
            title_match_score(needle, str(item.get("title") or "")),
            index,
            item,
        )
        for index, item in enumerate(papers)
    ]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    best_score, _, best = scored[0]
    if best_score < TITLE_MATCH_THRESHOLD:
        return None, papers, ""
    return best, papers, f"title(score={best_score:.2f})"


def _find_source_pdf(paths, paper_id: str, title: str) -> tuple[Path | None, str]:
    """Resolve the managed PDF for one paper.

    The authoritative mapping is ``document_map.source_file``: it records the
    exact file name at index time and survives later renames being recorded
    incorrectly.  Only when that is missing or stale do we fall back to
    comparing the normalised title against file names on disk.
    """
    document_map = load_document_map(paths, paper_id)
    source_file = str((document_map or {}).get("source_file") or "").strip()
    if source_file:
        candidate = paths.papers_dir / source_file
        if candidate.is_file():
            return candidate, "document_map"
    wanted = title_match_key(title)
    if wanted:
        best: tuple[float, str, Path] | None = None
        for index, candidate in enumerate(sorted(paths.papers_dir.glob("*.pdf"))):
            score = title_match_score(wanted, title_match_key(candidate.stem))
            if score < TITLE_MATCH_THRESHOLD:
                continue
            entry = (score, str(candidate), candidate)
            if best is None or entry[0] > best[0] or (entry[0] == best[0] and entry[1] < best[1]):
                best = entry
        if best is not None:
            return best[2], f"file_name(score={best[0]:.2f})"
    return None, ""


def _call_card_model(llm_client, model: str, prompt: str, cancel_event) -> str:
    response = llm_client.post(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "输出必须遵守证据锚点和标题约束。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        policy=RequestPolicy(
            purpose="paper_card",
            priority=RequestPriority.SUMMARY,
            deadline_seconds=45,
            max_retries=0,
            counts_toward_circuit=False,
        ),
        cancel_event=cancel_event,
    )
    try:
        content = response.json()["choices"][0]["message"].get("content")
    finally:
        response.close()
    if not isinstance(content, str) or not content.strip():
        raise ValueError("论文证据卡模型未返回正文")
    return content.strip() + "\n"


def handle_generate_paper_card(
    args: dict,
    *,
    paper_store=None,
    llm_client=None,
    model: str = "",
    cancel_event=None,
    **_kwargs,
) -> str:
    """Generate and persist an evidence-bounded Markdown card for an indexed PDF."""
    selector = str(args.get("paper_id_or_title", "") or "").strip()
    if not selector:
        return "❌ 请提供已索引论文的标题或 paper_id。"
    paper, available, match_kind = _find_paper(paper_store, selector)
    if paper is None:
        # Naming the real candidates lets the caller recover in one turn
        # instead of guessing why an indexed paper "does not exist".
        options = "；".join(
            f"{item.get('title')}（{item.get('paper_id')}）" for item in available[:5]
        )
        hint = f"已索引论文：{options}" if options else "论文库为空；请先 read_pdf 阅读并索引。"
        return f"❌ 未能匹配到已索引论文“{selector}”。{hint}"

    paths = get_runtime_paths()
    paper_id = str(paper.get("paper_id") or "")
    source_pdf, pdf_match_kind = _find_source_pdf(paths, paper_id, str(paper.get("title") or ""))
    if source_pdf is None:
        on_disk = "；".join(sorted(path.name for path in paths.papers_dir.glob("*.pdf"))[:5])
        hint = f"papers 目录实际文件：{on_disk}" if on_disk else "papers 目录为空。"
        return f"❌ 未找到该论文对应的本地 PDF，无法建立可追溯证据卡。{hint}"
    if pdf_match_kind.startswith("file_name"):
        # The recorded name went stale after a rename; repair it so later
        # lookups do not have to fall back again.
        update_document_map_source_file(paths, paper_id, source_pdf.name)

    max_pages = _bounded_int(args.get("max_pages"), 100, 1, 100)
    try:
        document_map = load_document_map(paths, str(paper["paper_id"]))
        if document_map is not None:
            source_map = build_source_map_from_document(document_map)
        else:
            source_map = create_source_map(
                source_pdf,
                paper_id=str(paper["paper_id"]),
                title=str(paper.get("title") or source_pdf.stem),
                max_pages=max_pages,
            )
        evidence = select_evidence_blocks(source_map)
        if llm_client is not None and model:
            card = _call_card_model(llm_client, model, paper_card_prompt(source_map, evidence), cancel_event)
            mode = "模型已完成"
        else:
            card = fallback_paper_card(source_map, evidence, "当前运行未配置可用模型")
            mode = "已生成证据草稿"
    except RequestCancelledError:
        raise
    except Exception as exc:
        # Source extraction failures cannot yield a trustworthy card.  Model
        # failures can still leave the user with an auditable evidence draft.
        if "source_map" not in locals():
            return f"❌ 论文证据卡生成失败: {type(exc).__name__}: {exc}"
        card = fallback_paper_card(source_map, locals().get("evidence", []), type(exc).__name__)
        mode = "模型未完成，已保留证据草稿"

    card_path, source_map_path, audit = write_paper_artifact(
        paths,
        paper_id=str(paper["paper_id"]),
        source_map=source_map,
        card=card,
    )
    coverage = source_map.get("coverage") or {}
    status = "通过" if audit["valid"] else "有警告"
    return (
        f"📇 {mode}：{paper.get('title')}\n"
        f"来源覆盖：{coverage.get('processed_pages') or 0}/{coverage.get('total_pages') or '未知'} 页；"
        f"证据块 {len(source_map.get('blocks') or [])} 个；审计 {status}。\n"
        f"证据卡：{card_path}\n来源映射：{source_map_path}"
    )

"""Model-facing paper evidence-card tool built on local managed PDFs."""

from __future__ import annotations

from pathlib import Path

from cancellation import RequestCancelledError
from llm_client import RequestPolicy, RequestPriority
from paper_artifacts import (
    create_source_map,
    fallback_paper_card,
    paper_card_prompt,
    select_evidence_blocks,
    write_paper_artifact,
)
from runtime_paths import get_runtime_paths


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _find_paper(paper_store, query: str) -> dict | None:
    normalized = query.casefold()
    papers = paper_store.list_papers() if paper_store else []
    exact = next((item for item in papers if query == item.get("paper_id")), None)
    if exact:
        return exact
    matches = [
        item for item in papers
        if normalized in str(item.get("paper_id") or "").casefold()
        or normalized in str(item.get("title") or "").casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _find_source_pdf(papers_dir: Path, title: str) -> Path | None:
    expected = title.replace(" ", "_").casefold()
    for candidate in papers_dir.glob("*.pdf"):
        if candidate.stem.casefold() == expected:
            return candidate
    return None


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
    paper = _find_paper(paper_store, selector)
    if paper is None:
        return "❌ 未找到唯一的已索引论文；请先使用 read_pdf 阅读并索引，再提供完整标题或 paper_id。"

    paths = get_runtime_paths()
    source_pdf = _find_source_pdf(paths.papers_dir, str(paper.get("title") or ""))
    if source_pdf is None:
        return "❌ 未找到该论文对应的本地 PDF，无法建立可追溯证据卡。"

    max_pages = _bounded_int(args.get("max_pages"), 100, 1, 100)
    try:
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

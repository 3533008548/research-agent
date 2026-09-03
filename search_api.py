"""Public academic search adapters for interactive and deep-research tools.

Provider transport is separate from formatting so normal chat search shares
paper identity rules with daily discovery without adding another service.
"""

from __future__ import annotations

import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import requests

from paper_records import deduplicate_paper_records, normalize_arxiv_id, normalize_doi


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
_QUERY_STOPWORDS = {
    "and", "for", "the", "with", "from", "under", "using", "about",
    "paper", "research", "study", "model", "models", "condition", "conditions",
}
_UNRELATED_VIDEO_MARKERS = (
    "temporal segment", "video action", "action recognition", "action detection",
)


def _is_network_tsn_query(query: str) -> bool:
    normalized = query.casefold()
    network_markers = ("网络", "调度", "流量", "负载", "network", "scheduling", "traffic", "flow")
    tsn_markers = ("tsn", "time-sensitive networking", "time sensitive networking")
    return any(marker in normalized for marker in tsn_markers) and any(
        marker in normalized for marker in network_markers
    )


def _content_terms(query: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", query.casefold())
        if token not in _QUERY_STOPWORDS
    }


def _openalex_search_query(query: str) -> str:
    if not _is_network_tsn_query(query):
        return query
    return '"Time-Sensitive Networking" AND (scheduling OR traffic OR bursty OR flow OR latency)'


def _score_public_relevance(query: str, title: str, abstract: str) -> tuple[float, str]:
    """Score a provider candidate without another model call."""
    text = f"{title}\n{abstract}".casefold()
    if any(marker in text for marker in _UNRELATED_VIDEO_MARKERS):
        return 0.0, "命中与网络研究无关的视频动作识别术语"

    if _is_network_tsn_query(query):
        has_tsn_domain = bool(re.search(r"time[- ]sensitive networks?|\btsn\b", text))
        if not has_tsn_domain:
            return 0.0, "未命中 Time-Sensitive Networking 或 TSN"
        score = 0.55
        reasons = ["命中 Time-Sensitive Networking/TSN"]
        if any(marker in text for marker in ("schedul", "gate control", "time-aware")):
            score += 0.20
            reasons.append("命中调度条件")
        if any(marker in text for marker in ("traffic", "flow", "burst", "arrival", "load")):
            score += 0.20
            reasons.append("命中流量或负载条件")
        if "difftsn" in text:
            score += 0.25
            reasons.append("命中 DiffTSN")
        score = min(round(score, 2), 1.0)
        if score < 0.75:
            return score, "仅命中 TSN 领域，未命中调度、流量或 DiffTSN 条件"
        return score, "；".join(reasons)

    terms = _content_terms(query)
    if not terms:
        return 0.5, "查询缺少可判定的英文术语，保留来源排序"
    matched = sum(term in text for term in terms)
    if not matched:
        return 0.0, "标题和摘要均未命中查询术语"
    return round(matched / len(terms), 2), f"命中 {matched}/{len(terms)} 个查询术语"


def _filter_public_papers(query: str, papers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reject low-confidence provider hits before they enter research evidence."""
    accepted: list[dict[str, Any]] = []
    rejected_reasons: dict[str, int] = {}
    strict_tsn = _is_network_tsn_query(query)
    for paper in papers:
        score, reason = _score_public_relevance(
            query, str(paper.get("title") or ""), str(paper.get("abstract") or ""),
        )
        keep = score >= 0.75 if strict_tsn else score > 0
        if keep:
            accepted.append({**paper, "relevance_score": score, "relevance_reason": reason})
        else:
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

    scores = [float(item["relevance_score"]) for item in accepted]
    return accepted, {
        "total": len(papers), "accepted": len(accepted), "rejected": len(papers) - len(accepted),
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "rejection_reason": "；".join(
            f"{reason}×{count}" for reason, count in sorted(rejected_reasons.items())
        ) or "无",
    }


def _quality_line(quality: dict[str, Any]) -> str:
    if not quality["accepted"]:
        return (
            "📭 未找到通过相关性校验的论文。"
            f"已拒绝 {quality['rejected']}/{quality['total']} 条（{quality['rejection_reason']}）。"
        )
    return (
        f"📊 检索质量：保留 {quality['accepted']}/{quality['total']} 条 | "
        f"相关性评分: {quality['score_min']:.2f}-{quality['score_max']:.2f} | "
        f"已拒绝 {quality['rejected']} 条（{quality['rejection_reason']}）"
    )


def _fetch_arxiv_records(query: str, max_results: int = 5) -> tuple[list[dict[str, Any]], str | None]:
    safe_query = urllib.parse.quote(query)
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query=all:{safe_query}&start=0&max_results={min(max_results, 10)}"
        "&sortBy=relevance&sortOrder=descending"
    )
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError) as exc:
        return [], f"arXiv API 请求失败: {exc}"

    records: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        link = _xml_text(entry, "atom:id")
        published = _xml_text(entry, "atom:published")[:10]
        arxiv_id = normalize_arxiv_id(link)
        records.append({
            "title": _xml_text(entry, "atom:title") or "N/A",
            "abstract": _xml_text(entry, "atom:summary"),
            "authors": [
                _xml_text(author, "atom:name")
                for author in entry.findall("atom:author", ARXIV_NS)
                if _xml_text(author, "atom:name")
            ],
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "published_at": published, "venue": "arXiv", "citation_count": 0,
            "doi": "", "url": link, "arxiv_id": arxiv_id,
            "sources": ["arxiv"], "source_ids": {"arxiv": arxiv_id or link},
        })
    return records, None


def _fetch_openalex_records(
    query: str,
    limit: int = 5,
    timeout: int | float | tuple[float, float] = 30,
    api_key: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, str]:
    provider_query = _openalex_search_query(query)
    params = {
        "search": provider_query, "per-page": min(limit, 10), "sort": "relevance_score:desc",
        "select": (
            "id,title,authorships,publication_year,publication_date,cited_by_count,doi,"
            "primary_location,abstract_inverted_index"
        ),
    }
    key = (api_key if api_key is not None else os.getenv("OPENALEX_API_KEY", "")).strip()
    if key:
        params["api_key"] = key
    try:
        response = requests.get("https://api.openalex.org/works", params=params, timeout=timeout)
        response.raise_for_status()
        results = response.json().get("results", [])
    except requests.RequestException as exc:
        return [], f"OpenAlex API 请求失败: {exc}", provider_query

    records: list[dict[str, Any]] = []
    for paper in results:
        location = paper.get("primary_location") or {}
        record_id = str(paper.get("id") or "")
        records.append({
            "title": paper.get("title") or "N/A",
            "abstract": _openalex_abstract(paper.get("abstract_inverted_index")) or "无摘要",
            "authors": [
                item.get("author", {}).get("display_name", "")
                for item in paper.get("authorships", [])[:5]
                if item.get("author", {}).get("display_name")
            ],
            "year": paper.get("publication_year") or "N/A",
            "published_at": str(paper.get("publication_date") or "")[:10],
            "citation_count": paper.get("cited_by_count") or 0,
            "venue": (location.get("source") or {}).get("display_name", "") or "N/A",
            "doi": normalize_doi(paper.get("doi")),
            "url": location.get("landing_page_url") or paper.get("doi") or record_id,
            "sources": ["openalex"], "source_ids": {"openalex": record_id},
        })
    return records, None, provider_query


def _render_search_results(
    query: str,
    papers: list[dict[str, Any]],
    quality: dict[str, Any],
    *,
    label: str,
    provider_query: str = "",
    failures: list[str] | None = None,
) -> str:
    if not papers:
        return _quality_line(quality)
    lines = [f"📚 **{label}搜索结果** — 查询: 「{query}」"]
    if provider_query:
        lines.append(f"🔎 OpenAlex 检索式: 「{provider_query}」")
    lines.append(_quality_line(quality))
    if failures:
        lines.append("⚠️ 未完成来源：" + "；".join(failures))
    for index, paper in enumerate(papers, 1):
        abstract = str(paper.get("abstract") or "")
        short_abstract = abstract[:220] + "..." if len(abstract) > 220 else abstract
        metadata = (
            f"年份: {paper.get('year') or 'N/A'}  | 引用: {paper.get('citation_count') or 0}"
            f"  | 期刊: {paper.get('venue') or 'N/A'}"
        )
        if paper.get("arxiv_id"):
            metadata += f"  | arXiv: {paper['arxiv_id']}"
        lines.extend([
            f"\n  {index}. **{paper.get('title') or 'N/A'}**  [{' + '.join(paper.get('sources') or [])}]",
            f"     作者: {', '.join((paper.get('authors') or [])[:5]) or 'N/A'}",
            f"     {metadata}",
            f"     相关性: {float(paper.get('relevance_score') or 0):.2f} | 依据: {paper.get('relevance_reason') or '来源排序'}",
            f"     链接: {paper.get('url') or 'N/A'}",
            f"     摘要: {short_abstract}",
        ])
    return "\n".join(lines)


def search_arxiv(query: str, max_results: int = 5) -> str:
    """Search arXiv while retaining a standalone display for explicit selection."""
    papers, failure = _fetch_arxiv_records(query, max_results)
    if failure:
        return f"❌ {failure}"
    accepted, quality = _filter_public_papers(query, papers)
    return _render_search_results(query, accepted, quality, label="arXiv ")


def search_openalex(
    query: str,
    limit: int = 5,
    timeout: int | float | tuple[float, float] = 30,
    api_key: str | None = None,
) -> str:
    """Search OpenAlex; read the raw key from ``OPENALEX_API_KEY`` by default."""
    papers, failure, provider_query = _fetch_openalex_records(query, limit, timeout, api_key)
    if failure:
        return f"❌ {failure}"
    accepted, quality = _filter_public_papers(query, papers)
    return _render_search_results(
        query, accepted, quality, label="OpenAlex ", provider_query=provider_query,
    )


def search_public_papers(query: str, limit: int = 5, source: str = "all") -> str:
    """Merge existing OpenAlex/arXiv results and report provider failures safely."""
    selected = ("openalex", "arxiv") if source == "all" else (source,)
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    total = rejected = 0
    for provider in selected:
        if provider == "openalex":
            fetched, failure, _ = _fetch_openalex_records(query, limit=min(limit * 2, 10))
        elif provider == "arxiv":
            fetched, failure = _fetch_arxiv_records(query, max_results=min(limit * 2, 10))
        else:
            continue
        if failure:
            failures.append(failure)
            continue
        accepted, quality = _filter_public_papers(query, fetched)
        total += quality["total"]
        rejected += quality["rejected"]
        records.extend(accepted)

    merged = deduplicate_paper_records(records)
    merged.sort(
        key=lambda item: (
            -float(item.get("relevance_score") or 0),
            -_year_value(item.get("year")),
            -int(item.get("citation_count") or 0),
            str(item.get("title") or "").casefold(),
        )
    )
    displayed = merged[:limit]
    scores = [float(item.get("relevance_score") or 0) for item in displayed]
    quality = {
        "total": total, "accepted": len(displayed), "rejected": rejected,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "rejection_reason": "多源相关性门控",
    }
    if not displayed and failures:
        return "❌ 所有已选来源均未完成：" + "；".join(failures)
    return _render_search_results(query, displayed, quality, label="多源学术", failures=failures)


def list_downloaded_papers() -> str:
    """List locally downloaded PDFs without accessing network services."""
    from runtime_paths import get_runtime_paths

    pdf_dir = get_runtime_paths().papers_dir
    pdfs = list(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []
    if not pdfs:
        return "📭 尚未下载任何论文。"
    total_size = sum(item.stat().st_size for item in pdfs)
    lines = [f"📂 本地已下载论文（共 {len(pdfs)} 篇，{total_size / 1024 / 1024:.1f} MB）\n"]
    for index, item in enumerate(pdfs, 1):
        modified = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {index}. {item.name}  ({item.stat().st_size / 1024:.0f} KB, {modified})")
    return "\n".join(lines)


def _year_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _xml_text(entry, path: str) -> str:
    node = entry.find(path, ARXIV_NS)
    return " ".join((node.text or "").split()) if node is not None else ""


def _openalex_abstract(inverted_index: dict | None) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    words = [
        (position, token)
        for token, positions in inverted_index.items()
        for position in positions
        if isinstance(position, int)
    ]
    return " ".join(token for _, token in sorted(words))

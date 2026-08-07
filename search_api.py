"""Public academic search adapters used by the Agent tools.

Only OpenAlex and arXiv are exposed to interactive and deep-research flows.
Daily discovery has its own bounded adapters in ``daily_orchestrator.py``.
"""

from __future__ import annotations

import os
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import requests


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


def search_arxiv(query: str, max_results: int = 5) -> str:
    """Search arXiv for temporary searches and deep research."""
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
        return f"❌ arXiv API 请求失败: {exc}"

    entries = root.findall("atom:entry", ARXIV_NS)
    if not entries:
        return "📭 arXiv 未找到相关论文。"

    lines = [f"📚 **arXiv 搜索结果** — 查询: 「{query}」\n"]
    for index, entry in enumerate(entries, 1):
        title = _xml_text(entry, "atom:title") or "N/A"
        abstract = _xml_text(entry, "atom:summary")
        authors = [
            _xml_text(author, "atom:name")
            for author in entry.findall("atom:author", ARXIV_NS)
            if _xml_text(author, "atom:name")
        ]
        link = _xml_text(entry, "atom:id")
        published = _xml_text(entry, "atom:published")[:10]
        short_abstract = abstract[:250] + "..." if len(abstract) > 250 else abstract
        lines.extend([
            f"\n  {index}. **{title}**",
            f"     作者: {', '.join(authors[:5])}{' et al.' if len(authors) > 5 else ''}",
            f"     日期: {published}  |  arXiv: {link.rsplit('/', 1)[-1] if link else ''}",
            f"     链接: {link}",
            f"     摘要: {short_abstract}",
        ])
    return "\n".join(lines)


def search_openalex(
    query: str,
    limit: int = 5,
    timeout: int | float | tuple[float, float] = 30,
    api_key: str | None = None,
) -> str:
    """Search OpenAlex; read the raw key from ``OPENALEX_API_KEY`` by default."""
    params = {
        "search": query,
        "per-page": min(limit, 10),
        "sort": "relevance_score:desc",
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
        papers = response.json().get("results", [])
    except requests.RequestException as exc:
        return f"❌ OpenAlex API 请求失败: {exc}"

    if not papers:
        return "📭 OpenAlex 未找到相关论文。"

    lines = [f"📚 **OpenAlex 搜索结果** — 查询: 「{query}」\n"]
    for index, paper in enumerate(papers, 1):
        location = paper.get("primary_location") or {}
        authors = [
            item.get("author", {}).get("display_name", "")
            for item in paper.get("authorships", [])[:5]
            if item.get("author", {}).get("display_name")
        ]
        abstract = _openalex_abstract(paper.get("abstract_inverted_index")) or "无摘要"
        short_abstract = abstract[:200] + "..." if len(abstract) > 200 else abstract
        venue = (location.get("source") or {}).get("display_name", "") or "N/A"
        link = location.get("landing_page_url") or paper.get("doi") or paper.get("id", "")
        lines.extend([
            f"\n  {index}. **{paper.get('title') or 'N/A'}**",
            f"     作者: {', '.join(authors)}",
            f"     年份: {paper.get('publication_year') or 'N/A'}  |  引用: {paper.get('cited_by_count') or 0}  |  期刊: {venue}",
            f"     链接: {link}",
            f"     摘要: {short_abstract}",
        ])
    return "\n".join(lines)


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

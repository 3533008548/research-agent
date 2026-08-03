"""
🔧 学术工具函数 — 从 research_agent.py 抽出为独立模块

提供：
  - search_arxiv()          arXiv API 搜索
  - search_semantic_scholar()  Semantic Scholar API 搜索
  - list_downloaded_papers()   本地论文缓存列表

这些函数无外部项目依赖，可被 graph_builder.py 和 CLI 安全导入。
"""

import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests

# ── arXiv API 命名空间 ──
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


def search_arxiv(query: str, max_results: int = 5) -> str:
    """通过 arXiv API 搜索论文（免费，无需 API Key）"""
    safe_q = urllib.parse.quote(query)
    url = (
        f"http://export.arxiv.org/api/query"
        f"?search_query=all:{safe_q}"
        f"&start=0&max_results={max_results}"
        f"&sortBy=submittedDate&sortOrder=descending"
    )

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"❌ arXiv API 请求失败: {e}"

    root = ET.fromstring(resp.content)
    entries = root.findall("atom:entry", ARXIV_NS)

    if not entries:
        return "📭 arXiv 未找到相关论文。"

    lines = [f"📚 **arXiv 搜索结果** — 查询: 「{query}」\n"]
    for i, entry in enumerate(entries, 1):
        title = entry.find("atom:title", ARXIV_NS)
        ttl = title.text.strip().replace("\n", " ") if title is not None else "N/A"

        summary = entry.find("atom:summary", ARXIV_NS)
        smry = summary.text.strip().replace("\n", " ") if summary is not None else ""

        authors = [
            a.find("atom:name", ARXIV_NS).text
            for a in entry.findall("atom:author", ARXIV_NS)
            if a.find("atom:name", ARXIV_NS) is not None
        ]

        link = entry.find("atom:id", ARXIV_NS)
        link_text = link.text.strip() if link is not None else ""

        published = entry.find("atom:published", ARXIV_NS)
        pub_date = published.text[:10] if published is not None else ""

        arxiv_id = link_text.split("/")[-1] if link_text else ""
        smry_short = smry[:250] + "..." if len(smry) > 250 else smry

        lines.append(f"\n  {i}. **{ttl}**")
        lines.append(
            f"     作者: {', '.join(authors[:5])}"
            f"{' et al.' if len(authors) > 5 else ''}"
        )
        lines.append(f"     日期: {pub_date}  |  arXiv: {arxiv_id}")
        lines.append(f"     链接: {link_text}")
        lines.append(f"     摘要: {smry_short}")

    return "\n".join(lines)


def search_semantic_scholar(
    query: str, limit: int = 5, timeout: int | float | tuple[float, float] = 30,
) -> str:
    """通过 Semantic Scholar API 搜索论文（免费，含引用数、PDF链接）"""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(limit, 10),
        "fields": "title,authors,year,abstract,citationCount,externalIds,openAccessPdf,url,venue",
    }

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"❌ Semantic Scholar API 请求失败: {e}"

    papers = data.get("data", [])
    if not papers:
        return "📭 Semantic Scholar 未找到相关论文。"

    lines = [f"📚 **Semantic Scholar 搜索结果** — 查询: 「{query}」\n"]
    for i, p in enumerate(papers, 1):
        title = p.get("title", "N/A")
        authors = [a.get("name", "") for a in p.get("authors", [])[:5]]
        year = p.get("year", "N/A")
        citations = p.get("citationCount", 0)
        venue = p.get("venue", "") or ""
        abstract = p.get("abstract") or "无摘要"
        abstract_short = abstract[:200] + "..." if len(abstract) > 200 else abstract

        pdf_info = p.get("openAccessPdf") or {}
        pdf_url = pdf_info.get("url", "") if pdf_info else ""
        url_link = p.get("url", "")

        lines.append(f"\n  {i}. **{title}**")
        lines.append(
            f"     作者: {', '.join(authors)}"
            f"{' et al.' if len(authors) > 5 else ''}"
        )
        lines.append(
            f"     年份: {year}  |  引用: {citations}"
            f"  |  期刊: {venue if venue else 'N/A'}"
        )
        if pdf_url:
            lines.append(f"     📄 PDF: {pdf_url}")
        if url_link:
            lines.append(f"     🔗 链接: {url_link}")
        lines.append(f"     摘要: {abstract_short}")

    return "\n".join(lines)


def list_downloaded_papers() -> str:
    """列出本地已下载的论文 PDF"""
    pdf_dir = Path("data/papers")
    if not pdf_dir.exists():
        return "📂 尚未下载任何论文。"

    pdfs = list(pdf_dir.glob("*.pdf"))
    if not pdfs:
        return "📂 data/papers/ 目录中没有 PDF 文件。"

    total_size = sum(f.stat().st_size for f in pdfs)
    lines = [
        f"📂 本地已下载论文（共 {len(pdfs)} 篇，{total_size / 1024 / 1024:.1f} MB）\n"
    ]
    for i, f in enumerate(pdfs, 1):
        size_kb = f.stat().st_size / 1024
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {i}. {f.name}  ({size_kb:.0f} KB, {mtime})")
    return "\n".join(lines)

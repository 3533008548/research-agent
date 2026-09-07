"""Small IEEE Xplore Metadata API adapter shared by search workflows.

This module deliberately returns metadata and a landing-page URL.  A PDF URL is
exposed separately only when IEEE marks the record as freely available, so the
caller never treats a metadata hit as permission to download full text.
"""

from __future__ import annotations

import re
from typing import Any


IEEE_METADATA_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
# IEEE documents the access type, but only the explicit ``Open Access`` value
# is treated as permission to expose a direct PDF link.  In particular,
# ``Ephemera`` is metadata rather than a blanket full-text permission.
IEEE_OPEN_ACCESS_TYPES = frozenset({"open access"})
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def ieee_search_params(query: str, *, limit: int, api_key: str) -> dict[str, Any]:
    """Build one bounded metadata-search request without logging the key."""
    return {
        "querytext": str(query or "").strip(),
        "max_records": max(1, min(int(limit), 200)),
        "format": "json",
        "apikey": str(api_key or "").strip(),
    }


def ieee_records(payload: object) -> list[dict[str, Any]]:
    """Normalize IEEE's sparse metadata response into the project record shape."""
    articles = payload.get("articles", []) if isinstance(payload, dict) else []
    if not isinstance(articles, list):
        return []
    records: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = _text(article.get("title"))
        if not title:
            continue
        article_number = _text(article.get("article_number"))
        access_type = _text(article.get("accessType"))
        html_url = _text(article.get("html_url") or article.get("abstract_url"))
        if not html_url and article_number:
            html_url = f"https://ieeexplore.ieee.org/document/{article_number}"
        pdf_url = _text(article.get("pdf_url"))
        open_access_pdf_url = (
            pdf_url if access_type.casefold() in IEEE_OPEN_ACCESS_TYPES else ""
        )
        publication_date = _text(article.get("publication_date"))
        publication_year = _text(article.get("publication_year"))
        records.append({
            "title": title,
            "abstract": _text(article.get("abstract")),
            "authors": _authors(article.get("authors")),
            "year": _year(publication_year or publication_date),
            "published_at": publication_date,
            "citation_count": _integer(article.get("citing_paper_count")),
            "venue": _text(article.get("publication_title")),
            "doi": _text(article.get("doi")),
            "url": html_url,
            "access_type": access_type,
            "open_access_pdf_url": open_access_pdf_url,
            "sources": ["ieee"],
            "source_ids": {"ieee": article_number or _text(article.get("doi")) or title},
        })
    return records


def _authors(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("authors") or value.get("author") or []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return []
    names = []
    for author in value:
        if isinstance(author, dict):
            name = _text(author.get("full_name") or author.get("name") or author.get("author"))
        else:
            name = _text(author)
        if name:
            names.append(name)
    return names[:8]


def _year(value: str) -> int | None:
    match = _YEAR.search(value)
    return int(match.group(0)) if match else None


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _text(value: object) -> str:
    return " ".join(str(value or "").split())

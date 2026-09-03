"""Shared normalization and deduplication for academic paper records.

Both interactive search and daily discovery receive partially overlapping
metadata from multiple providers.  Keeping the identity rules here prevents
the two paths from silently drifting apart.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


_TITLE_WORD = re.compile(r"[\w]+", re.UNICODE)
_ARXIV_ID = re.compile(r"(?:arxiv:)?(\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?", re.I)


def normalize_doi(value: object) -> str:
    """Return a comparison-safe DOI, or an empty string when absent."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    return text.rstrip(" .;,)")


def normalize_arxiv_id(value: object) -> str:
    """Extract an arXiv identifier without its mutable version suffix."""
    text = str(value or "").strip().lower()
    match = _ARXIV_ID.search(text)
    return match.group(1).lower() if match else ""


def normalize_title(value: object) -> str:
    """Keep alphanumeric title tokens only; suitable for conservative matching."""
    return " ".join(_TITLE_WORD.findall(str(value or "").casefold()))


def title_similarity(left: object, right: object) -> float:
    """Compare normalized titles without treating punctuation as a distinction."""
    first, second = normalize_title(left), normalize_title(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    return SequenceMatcher(None, first, second).ratio()


def same_paper(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Match records using stable identifiers before a conservative title fallback."""
    left_doi = normalize_doi(left.get("doi"))
    right_doi = normalize_doi(right.get("doi"))
    if left_doi and left_doi == right_doi:
        return True

    left_arxiv = normalize_arxiv_id(left.get("arxiv_id") or left.get("url"))
    right_arxiv = normalize_arxiv_id(right.get("arxiv_id") or right.get("url"))
    if left_arxiv and left_arxiv == right_arxiv:
        return True

    left_ids = left.get("source_ids") or {}
    right_ids = right.get("source_ids") or {}
    for source, source_id in right_ids.items():
        if source_id and left_ids.get(source) == source_id:
            return True
    return title_similarity(left.get("title"), right.get("title")) >= 0.90


def merge_paper_records(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge complementary provider fields into ``target`` in place."""
    target["sources"] = sorted(set(target.get("sources") or []) | set(source.get("sources") or []))
    target["keywords"] = sorted(set(target.get("keywords") or []) | set(source.get("keywords") or []))
    target.setdefault("source_ids", {}).update(source.get("source_ids") or {})
    for key in ("abstract", "authors", "published_at", "venue", "doi", "url", "year", "arxiv_id"):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
    if len(str(source.get("abstract") or "")) > len(str(target.get("abstract") or "")):
        target["abstract"] = source["abstract"]
    target["citation_count"] = max(
        int(target.get("citation_count") or 0), int(source.get("citation_count") or 0),
    )
    target["already_indexed"] = bool(target.get("already_indexed") or source.get("already_indexed"))
    target["relevance_score"] = max(
        float(target.get("relevance_score") or 0), float(source.get("relevance_score") or 0),
    )
    reasons = [
        str(value) for value in [target.get("relevance_reason"), source.get("relevance_reason")]
        if value
    ]
    if reasons:
        target["relevance_reason"] = "；".join(dict.fromkeys(reasons))[:240]


def deduplicate_paper_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return non-empty records with duplicate metadata merged deterministically."""
    merged: list[dict[str, Any]] = []
    for original in records:
        if not str(original.get("title") or "").strip():
            continue
        record = dict(original)
        record["sources"] = list(record.get("sources") or [])
        record["keywords"] = list(record.get("keywords") or [])
        record["source_ids"] = dict(record.get("source_ids") or {})
        existing = next((item for item in merged if same_paper(item, record)), None)
        if existing is None:
            merged.append(record)
        else:
            merge_paper_records(existing, record)
    return merged

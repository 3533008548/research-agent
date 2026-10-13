"""BM25 lexical retrieval — pure scoring, no vector-store coupling.

Extracted from ``paper_store.py`` during the five-dimension refactor.  The point
is testability: BM25 is the stage that decides which chunks the reranker even
sees, so its correctness matters more than its size, yet it could not be tested
without constructing a ``PaperStore`` (ChromaDB + embedder + data directory) and
hand-injecting ``_collection``.

Two shapes of scoring, one formula
----------------------------------
- :func:`build_index` builds postings once, then a query only touches the terms
  it asks for.  Measured ~160 ms per request when every chunk was re-tokenized
  on each call, which dominated hybrid latency once the encoder got fast.
- :func:`scan_score` is the full-corpus fallback for collections past the
  interactive budget, where building an index would cost more than it saves.

Both must produce **identical scores for the same candidates**.  The subtle part
is that BM25's document frequency and average length are computed over the
*filtered* candidate set, not the whole corpus — so scoping a query to one paper
changes the scores of that paper's chunks.  Both implementations here preserve
that, because the pre-index implementation did, and changing it would silently
reorder results.

Storage is injected, never imported
------------------------------------
Callers pass ``(document, metadata)`` pairs.  Nothing in this module knows that
ChromaDB exists, which is what lets the tests run with a plain list.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable, Iterator

# BM25 parameters. k1 controls term-frequency saturation, b controls length
# normalisation strength, and scale only affects the output magnitude.
BM25_K1 = 1.5
BM25_B = 0.75
BM25_SCALE = 2.5

# Latin words are matched with a hyphen/underscore tail so identifiers such as
# "cross_entropy" survive as one term.  CJK has no spaces, so it is handled as
# character bigrams below instead of word tokens.
_LATIN_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{1,}")
_CJK_CHAR = re.compile(r"[一-鿿]")

# Keys copied onto every lexical candidate.  Kept explicit rather than dumping
# the metadata dict, so retrieval results have the same shape regardless of what
# the store happens to store.
_ENTRY_FIELDS = (
    ("section", "未标注"),
    ("title", "Unknown"),
    ("paper_id", ""),
    ("chunk_index", 0),
    ("char_start", 0),
    ("char_end", 0),
    ("page", -1),
    ("element_id", ""),
    ("element_kind", "text"),
)


def tokenize(text: str) -> list[str]:
    """Split text into the token stream the local BM25 index uses.

    Latin script goes through a regex; CJK has no word delimiters, so adjacent
    character bigrams approximate words without pulling in a segmenter.
    """
    latin_terms = _LATIN_TOKEN.findall(text.lower())
    cjk = "".join(_CJK_CHAR.findall(text))
    return latin_terms + [cjk[i:i + 2] for i in range(max(len(cjk) - 1, 0))]


def query_terms(query_text: str) -> list[str]:
    """Deduplicated retrieval terms for a query, order preserved.

    Deduplication matters for scoring: a repeated term would otherwise be
    counted twice in the score sum, so the same query text would rank
    differently depending on how often it repeated a word.
    """
    return list(dict.fromkeys(tokenize(query_text)))


def _entry_from(document: str, metadata: dict | None) -> dict:
    meta = metadata or {}
    entry = {"text": document or "", "retrieval": "keyword"}
    for key, default in _ENTRY_FIELDS:
        entry[key] = meta.get(key, default)
    # page_end falls back to page rather than to -1, because a single-page chunk
    # is the common case and callers render "p.N" from it directly.
    entry["page_end"] = meta.get("page_end", meta.get("page", -1))
    return entry


def build_index(
    documents: Iterable[tuple[str, dict | None]],
    *,
    tokenize_fn: Callable[[str], list[str]] = tokenize,
) -> dict:
    """Build the postings map: ``term -> {position: term frequency}``.

    Also returns the flattened entries and their token lengths, which the scorer
    needs and which would otherwise require a second pass over the corpus.
    """
    entries: list[dict] = []
    lengths: list[int] = []
    postings: dict[str, dict[int, int]] = {}
    for document, metadata in documents:
        tokens = tokenize_fn(document or "")
        position = len(entries)
        lengths.append(len(tokens))
        entries.append(_entry_from(document, metadata))
        frequencies: dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        for term, count in frequencies.items():
            postings.setdefault(term, {})[position] = count
    return {"entries": entries, "lengths": lengths, "postings": postings}


def allowed_positions(
    entries: list[dict],
    paper_ids: Iterable[str] | None,
    section: str | None,
) -> set[int]:
    """Positions matching the scope.  An empty scope means "everything".

    Returning a set of positions (rather than filtering a list) keeps the
    membership test in the scoring loop O(1).
    """
    if not paper_ids and not section:
        return set(range(len(entries)))
    wanted = set(paper_ids or ())
    return {
        position
        for position, entry in enumerate(entries)
        if (not wanted or entry["paper_id"] in wanted)
        and (not section or entry["section"] == section)
    }


def score_index(
    index: dict,
    terms: list[str],
    top_k: int,
    paper_ids: Iterable[str] | None = None,
    section: str | None = None,
) -> list[dict]:
    """Score only the chunks the query terms actually occur in."""
    entries = index["entries"]
    lengths = index["lengths"]
    postings = index["postings"]

    allowed = allowed_positions(entries, paper_ids, section)
    if not allowed:
        return []

    # BM25 statistics are scoped to the candidate set, not the whole corpus —
    # this matches the pre-index implementation, so scores are unchanged.
    document_count = len(allowed)
    average_length = sum(lengths[position] for position in allowed) / document_count

    scores: dict[int, float] = {}
    for term in terms:
        hits = postings.get(term)
        if not hits:
            continue
        document_frequency = (
            len(hits) if len(allowed) == len(entries) else len(hits.keys() & allowed)
        )
        idf = math.log(
            1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        for position, frequency in hits.items():
            if position not in allowed:
                continue
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * lengths[position] / average_length
            )
            scores[position] = (
                scores.get(position, 0.0) + idf * frequency * BM25_SCALE / denominator
            )

    ranked = []
    for position, score in scores.items():
        value = round(score, 6)
        if value <= 0:
            continue
        ranked.append((position, value))
    ranked.sort(
        key=lambda item: (
            -item[1], entries[item[0]]["title"], entries[item[0]]["chunk_index"],
        )
    )

    results = []
    for position, value in ranked[:top_k]:
        item = dict(entries[position])
        item["keyword_score"] = value
        results.append(item)
    return results


def scan_score(
    documents: Iterable[tuple[str, dict | None]],
    terms: list[str],
    top_k: int,
    paper_ids: Iterable[str] | None = None,
    section: str | None = None,
    *,
    tokenize_fn: Callable[[str], list[str]] = tokenize,
) -> list[dict]:
    """Full-corpus scoring without an index, for oversized collections.

    Deliberately not reusing :func:`build_index`: past the interactive budget
    the corpus is large enough that materialising postings would cost more than
    it saves, so this streams candidates instead.
    """
    wanted = set(paper_ids or ())
    candidates: list[dict] = []
    tokenized: list[list[str]] = []
    for document, metadata in documents:
        meta = metadata or {}
        if wanted and meta.get("paper_id") not in wanted:
            continue
        if section and meta.get("section") != section:
            continue
        candidates.append(_entry_from(document, metadata))
        tokenized.append(tokenize_fn(document or ""))

    if not candidates:
        return []

    document_count = len(tokenized)
    average_length = sum(len(tokens) for tokens in tokenized) / document_count
    document_frequency = {
        term: sum(term in set(tokens) for tokens in tokenized)
        for term in terms
    }
    for item, tokens in zip(candidates, tokenized):
        length = len(tokens)
        score = 0.0
        for term in terms:
            frequency = tokens.count(term)
            if not frequency:
                continue
            idf = math.log(
                1 + (document_count - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * length / average_length
            )
            score += idf * frequency * BM25_SCALE / denominator
        item["keyword_score"] = round(score, 6)

    candidates = [item for item in candidates if item["keyword_score"] > 0]
    return sorted(
        candidates,
        key=lambda item: (-item["keyword_score"], item["title"], item["chunk_index"]),
    )[:top_k]


def iter_documents(collection: Any) -> Iterator[tuple[str, dict | None]]:
    """Read ``(document, metadata)`` pairs out of a Chroma collection.

    This is the *only* place the lexical module touches the vector store, and
    it is a thin adapter kept here rather than in the caller so that every path
    into BM25 gets the same document shape.
    """
    raw = collection.get(include=["documents", "metadatas"])
    return iter(zip(raw.get("documents") or [], raw.get("metadatas") or []))
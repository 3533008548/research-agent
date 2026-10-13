"""Equivalence check: papers/lexical.py vs the pre-refactor BM25 implementation.

Same approach as the chunking check — run both versions over the same corpus
and query set and compare every score and every ranking position.  BM25 touches
recall quality directly (the reranker only sees what lexical hands it), so a
silent difference here would be invisible in unit tests but visible in R@1.

Usage:
    python scripts/verify_lexical_equivalence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from papers import lexical as new_mod  # noqa: E402

sys.path.insert(0, str(ROOT / "tests" / "baseline"))

DOCS: list[tuple[str, dict]] = [
    ("Attention is all you need for sequence transduction tasks.", {
        "paper_id": "p1", "title": "Attention Paper", "section": "Method",
        "chunk_index": 0, "char_start": 0, "char_end": 60,
    }),
    ("The transformer uses multi-head self attention without recurrence.", {
        "paper_id": "p1", "title": "Attention Paper", "section": "Method",
        "chunk_index": 1, "char_start": 60, "char_end": 125,
    }),
    ("Diffusion models generate samples by iteratively denoising noise.", {
        "paper_id": "p2", "title": "Diffusion Paper", "section": "Abstract",
        "chunk_index": 0, "char_start": 0, "char_end": 66,
    }),
    ("我们提出了一种基于注意力的序列建模方法。注意力机制无需循环。", {
        "paper_id": "p2", "title": "Diffusion Paper", "section": "引言",
        "chunk_index": 1, "char_start": 66, "char_end": 100,
    }),
    ("Evaluation metrics include FID and Inception Score for image quality.", {
        "paper_id": "p3", "title": "Metrics Paper", "section": "Results",
        "chunk_index": 0, "char_start": 0, "char_end": 76,
        "page": 7, "page_end": 7,
    }),
    ("采样过程包含前向扩散与反向去噪两个阶段。", {
        "paper_id": "p3", "title": "Metrics Paper", "section": "方法",
        "chunk_index": 1, "char_start": 76, "char_end": 95,
        "page": 8,
    }),
    ("Short chunk.", {
        "paper_id": "p4", "title": "Tiny", "section": "Method",
        "chunk_index": 0, "char_start": 0, "char_end": 12,
    }),
]

QUERIES = [
    "attention mechanism",
    "diffusion sampling noise",
    "attention 注意力",
    "FID metrics",
    "no such term anywhere",
    "",
    "paper",
]

SCOPES = [
    (None, None, 10),
    (None, None, 3),
    (["p1"], None, 10),
    (["p1", "p3"], None, 10),
    (None, "Method", 10),
    (["p2"], "引言", 10),
    (["p4"], None, 10),
    (["nonexistent"], None, 10),
    (None, "Nonexistent Section", 10),
]

# The pre-refactor BM25, transcribed from paper_store.py at commit 29d1418~1.
# Kept here rather than imported so this file has no hidden dependency.
import math  # noqa: E402
import re  # noqa: E402

BM25_K1, BM25_B, BM25_SCALE = 1.5, 0.75, 2.5


def old_tokens(text):
    latin_terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}", text.lower())
    cjk = "".join(re.findall(r"[一-鿿]", text))
    return latin_terms + [cjk[i:i + 2] for i in range(max(len(cjk) - 1, 0))]


def old_query_terms(query_text):
    return list(dict.fromkeys(old_tokens(query_text)))


def old_build(documents):
    entries, lengths, postings = [], [], {}
    for doc, meta in documents:
        meta = meta or {}
        tokens = old_tokens(doc or "")
        position = len(entries)
        lengths.append(len(tokens))
        entries.append({
            "text": doc or "",
            "section": meta.get("section", "未标注"),
            "title": meta.get("title", "Unknown"),
            "paper_id": meta.get("paper_id", ""),
            "chunk_index": meta.get("chunk_index", 0),
            "char_start": meta.get("char_start", 0),
            "char_end": meta.get("char_end", 0),
            "page": meta.get("page", -1),
            "page_end": meta.get("page_end", meta.get("page", -1)),
            "element_id": meta.get("element_id", ""),
            "element_kind": meta.get("element_kind", "text"),
            "retrieval": "keyword",
        })
        frequencies = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        for term, count in frequencies.items():
            postings.setdefault(term, {})[position] = count
    return {"entries": entries, "lengths": lengths, "postings": postings}


def old_score_index(index, terms, top_k, paper_ids, section):
    entries, lengths, postings = index["entries"], index["lengths"], index["postings"]
    if paper_ids or section:
        allowed = {
            p for p, e in enumerate(entries)
            if (not paper_ids or e["paper_id"] in paper_ids)
            and (not section or e["section"] == section)
        }
    else:
        allowed = set(range(len(entries)))
    if not allowed:
        return []
    document_count = len(allowed)
    average_length = sum(lengths[p] for p in allowed) / document_count
    scores = {}
    for term in terms:
        hits = postings.get(term)
        if not hits:
            continue
        df = len(hits) if len(allowed) == len(entries) else len(hits.keys() & allowed)
        idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
        for position, frequency in hits.items():
            if position not in allowed:
                continue
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * lengths[position] / average_length
            )
            scores[position] = scores.get(position, 0.0) + idf * frequency * BM25_SCALE / denominator
    ranked = []
    for position, score in scores.items():
        value = round(score, 6)
        if value <= 0:
            continue
        ranked.append((position, value))
    ranked.sort(key=lambda item: (-item[1], entries[item[0]]["title"], entries[item[0]]["chunk_index"]))
    results = []
    for position, value in ranked[:top_k]:
        item = dict(entries[position])
        item["keyword_score"] = value
        results.append(item)
    return results


def old_scan(documents, terms, top_k, paper_ids, section):
    candidates = []
    for doc, meta in documents:
        meta = meta or {}
        if paper_ids and meta.get("paper_id") not in paper_ids:
            continue
        if section and meta.get("section") != section:
            continue
        candidates.append({
            "text": doc or "",
            "section": meta.get("section", "未标注"),
            "title": meta.get("title", "Unknown"),
            "paper_id": meta.get("paper_id", ""),
            "chunk_index": meta.get("chunk_index", 0),
            "char_start": meta.get("char_start", 0),
            "char_end": meta.get("char_end", 0),
            "page": meta.get("page", -1),
            "page_end": meta.get("page_end", meta.get("page", -1)),
            "element_id": meta.get("element_id", ""),
            "element_kind": meta.get("element_kind", "text"),
            "retrieval": "keyword",
        })
    if not candidates:
        return []
    tokenized = [old_tokens(item["text"]) for item in candidates]
    document_count = len(tokenized)
    average_length = sum(len(t) for t in tokenized) / document_count
    df = {term: sum(term in set(t) for t in tokenized) for term in terms}
    for item, tokens in zip(candidates, tokenized):
        length = len(tokens)
        score = 0.0
        for term in terms:
            frequency = tokens.count(term)
            if not frequency:
                continue
            idf = math.log(1 + (document_count - df[term] + 0.5) / (df[term] + 0.5))
            denominator = frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / average_length)
            score += idf * frequency * BM25_SCALE / denominator
        item["keyword_score"] = round(score, 6)
    candidates = [item for item in candidates if item["keyword_score"] > 0]
    return sorted(candidates, key=lambda i: (-i["keyword_score"], i["title"], i["chunk_index"]))[:top_k]


def compare(label, expected, actual, failures):
    if expected != actual:
        failures.append(label)
        if len(failures) <= 3:
            print(f"  MISMATCH {label}")
            print(f"    old: {expected}")
            print(f"    new: {actual}")


def main() -> int:
    failures: list[str] = []

    for doc, _meta in DOCS:
        if old_tokens(doc) != new_mod.tokenize(doc):
            compare(f"tokenize({doc[:20]!r})", old_tokens(doc), new_mod.tokenize(doc), failures)

    for q in QUERIES:
        if old_query_terms(q) != new_mod.query_terms(q):
            compare(f"query_terms({q!r})", old_query_terms(q), new_mod.query_terms(q), failures)

    old_index = old_build(DOCS)
    new_index = new_mod.build_index(DOCS)

    if old_index["entries"] != new_index["entries"]:
        failures.append("index entries differ")
    if old_index["lengths"] != new_index["lengths"]:
        failures.append("index lengths differ")
    if old_index["postings"] != new_index["postings"]:
        failures.append("index postings differ")

    checked = 0
    for q in QUERIES:
        old_t, new_t = old_query_terms(q), new_mod.query_terms(q)
        for paper_ids, section, top_k in SCOPES:
            label = f"score_index({q!r}, papers={paper_ids}, section={section}, k={top_k})"
            compare(
                label,
                old_score_index(old_index, old_t, top_k, paper_ids, section),
                new_mod.score_index(new_index, new_t, top_k, paper_ids, section),
                failures,
            )
            label = f"scan({q!r}, papers={paper_ids}, section={section}, k={top_k})"
            compare(
                label,
                old_scan(DOCS, old_t, top_k, paper_ids, section),
                new_mod.scan_score(DOCS, new_t, top_k, paper_ids, section),
                failures,
            )
            checked += 2

    # Both scoring paths must agree with each other, not just with the old code:
    # the index path and the scan path exist for different corpus sizes but must
    # rank identically for the same candidates.
    for q in QUERIES:
        if not q:
            continue
        terms = new_mod.query_terms(q)
        a = new_mod.score_index(new_index, terms, 10)
        b = new_mod.scan_score(DOCS, terms, 10)
        if [r.get("keyword_score") for r in a] != [r.get("keyword_score") for r in b]:
            failures.append(f"index vs scan disagree for {q!r}")

    if failures:
        print(f"EQUIVALENCE FAILED: {len(failures)} mismatch(es)")
        return 1

    print(f"Equivalence check passed ({checked + len(QUERIES) * 2 + 3} comparisons).")
    print("  - index path vs scan path agree on every query")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""Reproducible, passage-level evaluation for the local RAG retriever.

This evaluator deliberately measures retrieval rather than answer quality.
Each case has one or more human-verified source passages.  A hit requires the
same paper, chunk index, and anchor text, which prevents a title-only match
from being reported as evidence recall.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
DEFAULT_KS = (1, 3, 5)


class RagEvalError(ValueError):
    """Raised when a retrieval-evaluation manifest is incomplete or invalid."""


@dataclass(frozen=True)
class PassageLabel:
    """A human-verified passage that can answer one retrieval query."""

    paper_title: str
    chunk_index: int
    text_contains: str


@dataclass(frozen=True)
class RetrievalCase:
    """A query with one or more acceptable source passages."""

    case_id: str
    query: str
    labels: tuple[PassageLabel, ...]
    note: str = ""


@dataclass(frozen=True)
class RetrievalResponse:
    """Normalized output from a production or test retrieval callable."""

    results: list[dict[str, Any]]
    fallback_note: str | None = None


Retriever = Callable[[str, int], RetrievalResponse | list[dict[str, Any]]]


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _positive_int(value: object, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RagEvalError(f"{field} 必须是正整数") from exc
    if parsed < 1:
        raise RagEvalError(f"{field} 必须是正整数")
    return parsed


def _non_negative_int(value: object, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RagEvalError(f"{field} 必须是非负整数") from exc
    if parsed < 0:
        raise RagEvalError(f"{field} 必须是非负整数")
    return parsed


def parse_ks(raw_ks: Iterable[int] | str | None = None) -> tuple[int, ...]:
    """Normalize a comma-separated CLI value or iterable such as ``(1, 3, 5)``."""
    if raw_ks is None:
        values: list[object] = list(DEFAULT_KS)
    elif isinstance(raw_ks, str):
        values = [part.strip() for part in raw_ks.split(",") if part.strip()]
    else:
        values = list(raw_ks)
    if not values:
        raise RagEvalError("至少提供一个 k 值")
    return tuple(sorted({_positive_int(value, "k") for value in values}))


def _parse_label(raw: object, case_id: str, index: int) -> PassageLabel:
    if not isinstance(raw, dict):
        raise RagEvalError(f"用例 {case_id} 的 labels[{index}] 必须是对象")
    title = str(raw.get("paper_title") or "").strip()
    anchor = str(raw.get("text_contains") or "").strip()
    if not title or not anchor:
        raise RagEvalError(
            f"用例 {case_id} 的 labels[{index}] 需要 paper_title 和 text_contains"
        )
    return PassageLabel(
        paper_title=title,
        chunk_index=_non_negative_int(raw.get("chunk_index"), f"用例 {case_id} 的 chunk_index"),
        text_contains=anchor,
    )


def load_cases(path: str | Path) -> list[RetrievalCase]:
    """Load a checked, user-authored ground-truth manifest without indexing data."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RagEvalError(f"未找到 RAG 评测用例：{source}") from exc
    except json.JSONDecodeError as exc:
        raise RagEvalError(f"RAG 评测用例不是有效 JSON：{source}") from exc
    if not isinstance(raw, dict):
        raise RagEvalError("RAG 评测文件顶层必须是对象")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RagEvalError(f"仅支持 schema_version={SCHEMA_VERSION}")
    entries = raw.get("cases")
    if not isinstance(entries, list) or not entries:
        raise RagEvalError("RAG 评测文件必须包含至少一个 cases 条目")

    cases: list[RetrievalCase] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise RagEvalError(f"cases[{index}] 必须是对象")
        case_id = str(entry.get("id") or "").strip()
        query = str(entry.get("query") or "").strip()
        if not case_id or not query:
            raise RagEvalError(f"cases[{index}] 需要 id 和 query")
        if case_id in seen_ids:
            raise RagEvalError(f"存在重复的评测用例 ID：{case_id}")
        seen_ids.add(case_id)
        labels = entry.get("labels")
        if not isinstance(labels, list) or not labels:
            raise RagEvalError(f"用例 {case_id} 至少需要一个人工金标 labels 条目")
        cases.append(
            RetrievalCase(
                case_id=case_id,
                query=query,
                labels=tuple(_parse_label(label, case_id, label_index) for label_index, label in enumerate(labels, start=1)),
                note=str(entry.get("note") or "").strip(),
            )
        )
    return cases


def _matches_paper(result: dict[str, Any], labels: tuple[PassageLabel, ...]) -> bool:
    title = _normalized_text(result.get("title"))
    return any(title == _normalized_text(label.paper_title) for label in labels)


def _matches_passage(result: dict[str, Any], labels: tuple[PassageLabel, ...]) -> bool:
    title = _normalized_text(result.get("title"))
    text = _normalized_text(result.get("text"))
    try:
        chunk_index = int(result.get("chunk_index"))
    except (TypeError, ValueError):
        return False
    return any(
        title == _normalized_text(label.paper_title)
        and chunk_index == label.chunk_index
        and _normalized_text(label.text_contains) in text
        for label in labels
    )


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def _rank_at_or_before(results: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> int | None:
    for rank, result in enumerate(results, start=1):
        if predicate(result):
            return rank
    return None


def _serialize_case(case: RetrievalCase) -> dict[str, Any]:
    return {
        "id": case.case_id,
        "query": case.query,
        "note": case.note,
        "labels": [asdict(label) for label in case.labels],
    }


def evaluate_cases(
    cases: Iterable[RetrievalCase],
    retrieve: Retriever,
    *,
    ks: Iterable[int] | str | None = None,
    mode: str = "timed_hybrid",
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a retriever against human-labelled document and passage evidence.

    ``paper_recall_at_k`` is intentionally reported separately from strict
    ``passage_recall_at_k``.  The latter is the metric to use when deciding
    whether an answer has a grounded evidence block, while the former helps
    diagnose whether a miss originated in corpus selection or passage ranking.
    """
    normalized_cases = list(cases)
    if not normalized_cases:
        raise RagEvalError("无法评测空用例集")
    normalized_ks = parse_ks(ks)
    maximum_k = max(normalized_ks)
    evaluated: list[dict[str, Any]] = []
    latency_ms: list[float] = []

    for case in normalized_cases:
        started = time.perf_counter()
        received = retrieve(case.query, maximum_k)
        elapsed_ms = round((time.perf_counter() - started) * 1_000, 3)
        response = received if isinstance(received, RetrievalResponse) else RetrievalResponse(list(received or []))
        results = [dict(item) for item in response.results[:maximum_k] if isinstance(item, dict)]
        paper_rank = _rank_at_or_before(results, lambda result: _matches_paper(result, case.labels))
        passage_rank = _rank_at_or_before(results, lambda result: _matches_passage(result, case.labels))
        latency_ms.append(elapsed_ms)
        evaluated.append(
            {
                **_serialize_case(case),
                "latency_ms": elapsed_ms,
                "fallback_used": bool(response.fallback_note),
                "fallback_note": response.fallback_note or "",
                "first_paper_rank": paper_rank,
                "first_passage_rank": passage_rank,
                "results": [
                    {
                        "rank": rank,
                        "paper_id": str(result.get("paper_id") or ""),
                        "title": str(result.get("title") or ""),
                        "chunk_index": result.get("chunk_index"),
                        "retrieval": str(result.get("retrieval") or "semantic"),
                        "paper_match": _matches_paper(result, case.labels),
                        "passage_match": _matches_passage(result, case.labels),
                    }
                    for rank, result in enumerate(results, start=1)
                ],
            }
        )

    total = len(evaluated)
    passage_recall = {
        str(k): round(sum(item["first_passage_rank"] is not None and item["first_passage_rank"] <= k for item in evaluated) / total, 4)
        for k in normalized_ks
    }
    paper_recall = {
        str(k): round(sum(item["first_paper_rank"] is not None and item["first_paper_rank"] <= k for item in evaluated) / total, 4)
        for k in normalized_ks
    }
    reciprocal_ranks = [1.0 / item["first_passage_rank"] if item["first_passage_rank"] else 0.0 for item in evaluated]
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "corpus": corpus or {},
        "metrics": {
            "case_count": total,
            "paper_recall_at_k": paper_recall,
            "passage_recall_at_k": passage_recall,
            "mrr": round(statistics.fmean(reciprocal_ranks), 4),
            "fallback_case_count": sum(item["fallback_used"] for item in evaluated),
            "latency_ms": {
                "mean": round(statistics.fmean(latency_ms), 3),
                "p50": round(statistics.median(latency_ms), 3),
                "p95": round(_percentile_95(latency_ms), 3),
                "max": round(max(latency_ms), 3),
            },
        },
        "cases": evaluated,
    }
    return report


def evaluate_paper_store(
    store: Any,
    cases: Iterable[RetrievalCase],
    *,
    ks: Iterable[int] | str | None = None,
    mode: str = "timed_hybrid",
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Run the exact local retriever path used by ``query_papers``."""
    if mode not in {"timed_hybrid", "hybrid", "keyword"}:
        raise RagEvalError("mode 仅支持 timed_hybrid、hybrid 或 keyword")

    def _retrieve(query: str, top_k: int) -> RetrievalResponse:
        if mode == "keyword":
            return RetrievalResponse(store.query_lexical(query, top_k=top_k))
        if mode == "hybrid":
            return RetrievalResponse(store.query_hybrid(query, top_k=top_k))
        results, pending = store.query_with_timeout(
            query,
            top_k=top_k,
            timeout_seconds=timeout_seconds,
        )
        return RetrievalResponse(list(results or []), pending)

    corpus = {
        "paper_count": int(getattr(store, "paper_count", 0) or 0),
        "chunk_count": int(getattr(store, "chunk_count", 0) or 0),
    }
    return evaluate_cases(cases, _retrieve, ks=ks, mode=mode, corpus=corpus)


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    """Write a report deliberately outside the versioned evaluation fixtures."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def summary_lines(report: dict[str, Any]) -> list[str]:
    """Return short CLI-safe summary lines without exposing retrieved passages."""
    metrics = report["metrics"]
    passage = metrics["passage_recall_at_k"]
    paper = metrics["paper_recall_at_k"]
    recall_text = ", ".join(f"R@{k}={passage[k] * 100:.1f}%" for k in sorted(passage, key=int))
    paper_text = ", ".join(f"PaperR@{k}={paper[k] * 100:.1f}%" for k in sorted(paper, key=int))
    latency = metrics["latency_ms"]
    misses = [item["id"] for item in report["cases"] if item["first_passage_rank"] is None]
    lines = [
        f"用例数: {metrics['case_count']} | 模式: {report['mode']}",
        f"段落级召回: {recall_text} | MRR={metrics['mrr']:.4f}",
        f"论文级召回: {paper_text}",
        f"检索延迟: P50={latency['p50']:.1f}ms, P95={latency['p95']:.1f}ms | 降级={metrics['fallback_case_count']}",
    ]
    if misses:
        lines.append("段落级未命中: " + ", ".join(misses))
    return lines

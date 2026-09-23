"""Source-bounded evidence structures used by the deep-research workflow.

The module deliberately does not turn abstracts or retrieved chunks into new
scientific claims.  It keeps source text and anchors intact, and only extracts
surface-level *clues* (for layout and review) from words that are present in
the supplied source.  Semantic synthesis remains the model's job and must
cite the originating evidence ID.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


_FACET_PATTERNS = {
    "method": re.compile(
        r"\b(propose|proposed|introduce|method|approach|algorithm|model|framework|design|"
        r"architecture|protocol|scheme)\b|提出|方法|算法|模型|框架|设计|机制|协议",
        re.IGNORECASE,
    ),
    "finding": re.compile(
        r"\b(result|results|experiment|evaluation|performance|improve|outperform|"
        r"achiev|show|demonstrat|accuracy|latency|throughput)\b|结果|实验|评估|性能|"
        r"提升|优于|表明|准确率|时延|吞吐",
        re.IGNORECASE,
    ),
    "condition": re.compile(
        r"\b(under|when|setting|scenario|condition|workload|traffic|dataset|"
        r"topology|environment|assumption)\b|条件|场景|负载|流量|数据集|拓扑|环境|假设|在.*?下",
        re.IGNORECASE,
    ),
    "limitation": re.compile(
        r"\b(limit|limitation|limited|restrict|only|fail|failure|unable|however|"
        r"whereas|contrast|not\s+work|challenge|drawback)\b|局限|限制|仅|不适用|失败|"
        r"难以|无法|然而|但是|反例|冲突|边界|挑战|缺点",
        re.IGNORECASE,
    ),
}
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?;；.])\s+|\n+")

# ---------------------------------------------------------------------------
# Source anchors (P0-2)
#
# A citation is only auditable when it points at a position *inside* a source.
# ``kind`` is a closed enum borrowed from the ARS deep-research suite: an
# anchor that cannot be resolved to one of these values must be emitted as
# ``none`` rather than disguised as a plausible-looking locator.  Emitting
# ``none`` does not bypass the gate -- it *triggers* it: weak anchors are
# counted, surfaced in the matrix and reported to the synthesizer as线索-only.
# ---------------------------------------------------------------------------
ANCHOR_KINDS = ("page", "section", "paragraph", "quote", "none")
_PRECISE_ANCHOR_KINDS = ("page", "section", "paragraph", "quote")

_PAGE_RANGE = re.compile(r"^p\.?\s*(\d+)(?:\s*[–\-~]\s*(\d+))?$")
_SECTION_SUFFIX = "章节"
# 占位标签不是定位信息：把它们当 section/element 会把弱定位伪装成精确锚点。
_GENERIC_LABELS = {"", "未标注", "未知", "正文", "text", "chunk"}

# Two-layer citation emission (P0-3): visible text carries ``[E1]``; the hidden
# HTML comment carries the machine-readable ref and the in-source anchor.
_CITATION_REF = re.compile(r"<!--\s*ref:\s*([A-Za-z0-9_:\-]+)\s*-->")
_CITATION_ANCHOR = re.compile(
    r"<!--\s*anchor:\s*(page|section|paragraph|quote|none)\s*(?::\s*([^>]*?))?\s*-->"
)


def parse_local_locator(parts: list[str]) -> dict[str, Any]:
    """Classify the ``title · p.N · label · 章节`` locator into a closed-enum anchor.

    ``query_papers`` renders ``[{title} · p.{start}–{end} · {element} · {section}章节]``.
    The most precise resolvable position wins: page over paragraph over section.
    Anything unresolvable becomes ``none`` -- never a guessed page number.
    """
    page: int | None = None
    page_end: int | None = None
    section = ""
    element = ""
    for raw in parts:
        part = _normalise_text(str(raw))
        if not part:
            continue
        page_match = _PAGE_RANGE.match(part)
        if page_match:
            page = int(page_match.group(1))
            tail = page_match.group(2)
            page_end = int(tail) if tail else page
            continue
        if part.endswith(_SECTION_SUFFIX):
            candidate = part[: -len(_SECTION_SUFFIX)].strip()
            if candidate.casefold() not in _GENERIC_LABELS:
                section = candidate
            continue
        if not element and part.casefold() not in _GENERIC_LABELS:
            element = part
    if page is not None:
        kind = "page"
    elif element:
        kind = "paragraph"
    elif section:
        kind = "section"
    else:
        kind = "none"
    return {
        "kind": kind,
        "page": page,
        "page_end": page_end,
        "section": section,
        "element": element,
    }


def anchor_is_precise(anchor: Any) -> bool:
    """True when an anchor resolves to a position inside the source."""
    if not isinstance(anchor, dict):
        return False
    return str(anchor.get("kind") or "none") in _PRECISE_ANCHOR_KINDS


def split_tool_evidence(content: str, *, researcher: str, source_type: str) -> list[dict[str, Any]]:
    """Split a tool response into source-specific cards when its format permits.

    ``search_papers`` and ``query_papers`` render one numbered record per
    paper/chunk.  Keeping that boundary matters: a conclusion citing one card
    must never silently combine the title of one paper with another's abstract.
    Unknown tool formats intentionally fall back to a single card.
    """
    if source_type == "search_papers":
        cards = _split_public_search(content, researcher)
        if cards:
            return cards
    if source_type == "query_papers":
        cards = _split_local_query(content, researcher)
        if cards:
            return cards
    return []


def attach_evidence_units(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a source-bounded, reviewable unit to every numbered evidence card."""
    enriched: list[dict[str, Any]] = []
    for index, raw in enumerate(evidence, 1):
        card = dict(raw)
        evidence_id = str(card.get("id") or f"E{index}")
        excerpt = _normalise_text(str(card.get("excerpt") or ""))[:900]
        anchor = card.get("source_anchor")
        if not isinstance(anchor, dict):
            anchor = {}
        if not anchor_is_precise(anchor):
            # "缺失 ≠ 可解析"。伪装成看起来可信的定位符比承认 none 更危险。
            anchor = dict(anchor)
            anchor.setdefault("kind", "none")
            anchor.setdefault("scope", "tool_result")
            anchor.setdefault(
                "label", str(card.get("source") or card.get("source_type") or "未知来源"),
            )
            anchor["anchor_note"] = (
                "未能解析到论文内部定位；该条只能作为检索线索，不得作为精确引用来源。"
            )
            card["source_anchor"] = anchor
        facets = [name for name, pattern in _FACET_PATTERNS.items() if pattern.search(excerpt)]
        unit = {
            "id": f"U{index}",
            "evidence_id": evidence_id,
            "source_type": str(card.get("source_type") or "tool"),
            "source": str(card.get("source") or "未知来源"),
            "source_anchor": dict(anchor),
            "observed_text": excerpt,
            "facets": facets or ["unclassified"],
            # These are quoted source snippets, not inferred conditions or limits.
            "condition_clues": _matching_sentences(excerpt, _FACET_PATTERNS["condition"]),
            "boundary_clues": _matching_sentences(excerpt, _FACET_PATTERNS["limitation"]),
            "interpretation_note": "字段由来源文本中的关键词与定位信息生成；仅作审阅线索，不构成新的科学结论。",
        }
        card["evidence_unit"] = unit
        enriched.append(card)
    return enriched


def build_evidence_analysis(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a conservative comparison matrix and a boundary-evidence ledger."""
    rows: list[dict[str, Any]] = []
    boundary_items: list[dict[str, Any]] = []
    facet_counts: Counter[str] = Counter()
    for card in evidence:
        unit = card.get("evidence_unit")
        if not isinstance(unit, dict):
            continue
        facets = [str(item) for item in unit.get("facets") or []]
        facet_counts.update(facets)
        anchor = unit.get("source_anchor")
        row = {
            "evidence_id": str(card.get("id") or unit.get("evidence_id") or ""),
            "source": str(card.get("source") or "未知来源"),
            "anchor": _anchor_label(anchor),
            "anchor_kind": str((anchor or {}).get("kind") or "none") if isinstance(anchor, dict) else "none",
            "anchor_precise": anchor_is_precise(anchor),
            "method": "method" in facets,
            "finding": "finding" in facets,
            "condition": "condition" in facets,
            "limitation": "limitation" in facets,
        }
        rows.append(row)
        clues = [str(item) for item in unit.get("boundary_clues") or []]
        if clues:
            boundary_items.append({
                "evidence_id": row["evidence_id"],
                "source": row["source"],
                "clues": clues[:2],
            })

    missing = []
    labels = {
        "method": "方法",
        "finding": "结果",
        "condition": "适用条件",
        "limitation": "局限/反证线索",
    }
    for facet, label in labels.items():
        if not facet_counts.get(facet):
            missing.append(f"已收集片段中未发现明确的{label}线索；综合时不得补写为已证实。")
    return {
        "matrix": rows,
        "boundary_evidence": boundary_items,
        "coverage": dict(facet_counts),
        "gaps": missing,
    }


def format_evidence_matrix(analysis: dict[str, Any]) -> str:
    """Render a compact source-comparison matrix without fabricating content."""
    rows = analysis.get("matrix") if isinstance(analysis, dict) else []
    if not isinstance(rows, list) or not rows:
        return "- 未形成可比较的结构化证据单元。"
    lines = [
        "| 来源 | 证据 | 定位 | 定位精度 | 方法 | 结果 | 条件 | 局限/反证线索 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        source = _table_text(str(row.get("source") or "未知来源"), 70)
        anchor = _table_text(str(row.get("anchor") or ""), 42)
        source_cell = source if not anchor else f"{source}<br>{anchor}"
        kind = str(row.get("anchor_kind") or "none")
        precision = "精确" if row.get("anchor_precise") else "弱"
        marks = ["✓" if bool(row.get(key)) else "—" for key in ("method", "finding", "condition", "limitation")]
        lines.append(
            f"| {source_cell} | [{row.get('evidence_id') or '?'}] | {kind} | {precision} | "
            f"{' | '.join(marks)} |"
        )
    return "\n".join(lines)


def format_boundary_ledger(analysis: dict[str, Any]) -> str:
    """Render only source-text boundary clues; absence is reported explicitly."""
    items = analysis.get("boundary_evidence") if isinstance(analysis, dict) else []
    if not isinstance(items, list) or not items:
        return (
            "- 当前已收集的来源片段未出现明确的局限或反例措辞。"
            "这不代表不存在反证；应继续以失败模式、反例和适用条件为关键词检索。"
        )
    lines = []
    for item in items:
        quoted = "；".join(f"“{_table_text(str(clue), 180)}”" for clue in item.get("clues") or [])
        lines.append(f"- [{item.get('evidence_id')}] {item.get('source')}: {quoted}")
    return "\n".join(lines)


def compact_analysis_for_prompt(analysis: dict[str, Any]) -> str:
    """Give the synthesizer auditable layout cues, not a second unsupported summary."""
    matrix = format_evidence_matrix(analysis)
    ledger = format_boundary_ledger(analysis)
    gaps = analysis.get("gaps") if isinstance(analysis, dict) else []
    gap_text = "\n".join(f"- {item}" for item in gaps or []) or "- 无额外缺口提示。"
    return (
        "结构化证据矩阵（✓ 仅表示来源片段包含相关关键词，不代表研究结论已成立）：\n"
        f"{matrix}\n\n反证与边界线索（均为来源原文片段）：\n{ledger}\n\n证据缺口：\n{gap_text}\n\n"
        "定位精度规则：「定位精度 = 弱」的证据只能作为检索线索——可以引用，但不得给出页码级或"
        "逐字级定位，也不得作为量化数值的唯一依据；不要把弱定位提升为精确定位。"
    )


def analysis_metrics(analysis: dict[str, Any]) -> dict[str, int]:
    """Payload-free metrics suitable for a persisted operational trace."""
    rows = analysis.get("matrix") or []
    return {
        "structured_units": len(rows),
        "precise_anchor_units": sum(1 for row in rows if row.get("anchor_precise")),
        "weak_anchor_units": sum(1 for row in rows if not row.get("anchor_precise")),
        "boundary_signal_units": len(analysis.get("boundary_evidence") or []),
        "coverage_method": int((analysis.get("coverage") or {}).get("method", 0)),
        "coverage_finding": int((analysis.get("coverage") or {}).get("finding", 0)),
        "coverage_condition": int((analysis.get("coverage") or {}).get("condition", 0)),
        "coverage_limitation": int((analysis.get("coverage") or {}).get("limitation", 0)),
    }


def _split_public_search(content: str, researcher: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"^\s*(?P<number>\d+)\.\s+\*\*(?P<title>.+?)\*\*\s+\[(?P<providers>[^\]]*)\]"
        r"(?P<body>.*?)(?=^\s*\d+\.\s+\*\*|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    cards = []
    quality = _public_quality(content)
    for match in pattern.finditer(content):
        title = _normalise_text(match.group("title"))[:240]
        body = _normalise_text(match.group("body"))[:900]
        url = _field_from_body(match.group("body"), "链接")
        year = _metadata_value(match.group("body"), "年份")
        score = _metadata_value(match.group("body"), "相关性")
        cards.append({
            "id": "",
            "researcher": researcher,
            "source_type": "search_papers",
            "source": title or "公开论文检索",
            "excerpt": f"{title}。{body}"[:900],
            "uncertainty": "公开目录元数据与摘要，不等价于通读全文；方法细节和结果需回到原文核验。",
            "source_anchor": {
                # 目录/摘要级结果没有论文内部位置；如实标 none，不伪造成页码。
                "kind": "none",
                "scope": "public_paper_metadata",
                "anchor_note": "仅检索目录与摘要级信息，未进入全文；不得据此给出页码级或逐字级定位。",
                "title": title,
                "providers": _normalise_text(match.group("providers")),
                "url": url,
                "year": year,
                "retrieval_score": score,
            },
            **({"relevance": quality} if quality else {}),
        })
    return cards


def _split_local_query(content: str, researcher: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"^\s*(?P<number>\d+)\.\s+\[(?P<label>[^\]]+)\]\s*.*?\n\s*(?P<text>.+?)"
        r"(?=^\s*\d+\.\s+\[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    cards = []
    for match in pattern.finditer(content):
        label = _normalise_text(match.group("label"))
        parts = [part.strip() for part in label.split(" · ") if part.strip()]
        title = parts[0] if parts else label
        text = _normalise_text(match.group("text"))[:900]
        cards.append({
            "id": "",
            "researcher": researcher,
            "source_type": "query_papers",
            "source": title[:240] or "本地 RAG 检索",
            "excerpt": text,
            "uncertainty": "本地索引片段可能省略上下文；应通过页码、章节或相邻文本回到原文核验。",
            "source_anchor": _local_anchor(title, parts),
        })
    return cards


def _local_anchor(title: str, parts: list[str]) -> dict[str, Any]:
    """Build a closed-enum anchor from the parsed local-retrieval locator."""
    parsed = parse_local_locator(parts[1:])
    anchor: dict[str, Any] = {
        "scope": "local_paper_chunk",
        "title": title[:240],
        "locator": " · ".join(parts[1:])[:240],
    }
    anchor.update(parsed)
    if not anchor_is_precise(anchor):
        anchor["anchor_note"] = "本地片段未解析到页码或章节；只能作为线索，不得作为精确引用来源。"
    return anchor


def _public_quality(content: str) -> dict[str, Any] | None:
    match = re.search(
        r"检索质量：保留 (\d+)/(\d+) 条 \| 相关性评分: ([0-9.]+)-([0-9.]+) \| "
        r"已拒绝 (\d+) 条（([^）]*)）",
        content,
    )
    if not match:
        return None
    return {
        "accepted": int(match.group(1)),
        "total": int(match.group(2)),
        "score_min": float(match.group(3)),
        "score_max": float(match.group(4)),
        "rejected": int(match.group(5)),
        "rejection_reason": match.group(6)[:300],
    }


def _field_from_body(body: str, label: str) -> str:
    match = re.search(rf"^\s*{re.escape(label)}:\s*(.+?)\s*$", body, re.MULTILINE)
    return _normalise_text(match.group(1))[:500] if match else ""


def _metadata_value(body: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}:\s*([^|\n]+)", body)
    return _normalise_text(match.group(1))[:120] if match else ""


def _matching_sentences(text: str, pattern: re.Pattern[str]) -> list[str]:
    candidates = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    return [part[:260] for part in candidates if pattern.search(part)][:2]


def _anchor_label(anchor: Any) -> str:
    if not isinstance(anchor, dict):
        return ""
    for key in ("locator", "url", "providers"):
        value = _normalise_text(str(anchor.get(key) or ""))
        if value:
            return value[:160]
    position = _position_label(anchor)
    return position or _normalise_text(str(anchor.get("label") or ""))[:160]


def _position_label(anchor: dict[str, Any]) -> str:
    """Render the resolved in-source position, or '' when nothing resolved."""
    parts: list[str] = []
    page = anchor.get("page")
    page_end = anchor.get("page_end")
    if isinstance(page, int):
        if isinstance(page_end, int) and page_end > page:
            parts.append(f"p.{page}–{page_end}")
        else:
            parts.append(f"p.{page}")
    for key in ("element", "section"):
        value = _normalise_text(str(anchor.get(key) or ""))
        if value and value not in parts:
            parts.append(value)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Two-layer citation emission (P0-3)
# ---------------------------------------------------------------------------


def extract_citation_markers(text: str) -> list[dict[str, Any]]:
    """Parse the hidden ``<!--ref:..--><!--anchor:kind:value-->`` citation layer.

    The visible layer is the ``[E#]`` marker readers see; the hidden layer makes
    "conclusion -> evidence -> position inside the source" mechanically checkable.
    """
    if not text:
        return []
    refs = [
        {"index": m.start(), "ref": m.group(1)}
        for m in _CITATION_REF.finditer(text)
    ]
    anchors = [
        {
            "index": m.start(),
            "anchor_kind": m.group(1),
            "anchor_value": _normalise_text(m.group(2) or ""),
        }
        for m in _CITATION_ANCHOR.finditer(text)
    ]
    markers: list[dict[str, Any]] = []
    for item in refs:
        following = next((a for a in anchors if a["index"] >= item["index"]), None)
        markers.append({
            "evidence_id": item["ref"],
            "anchor_kind": (following or {}).get("anchor_kind", "none"),
            "anchor_value": (following or {}).get("anchor_value", ""),
        })
    return markers


def strip_citation_markers(text: str) -> str:
    """Remove the hidden citation layer before the report is shown to a reader."""
    if not text:
        return text
    cleaned = _CITATION_ANCHOR.sub("", text)
    return _CITATION_REF.sub("", cleaned)


def audit_citations(report: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare emitted citation markers against the evidence actually supplied."""
    known_ids = {str(item.get("id") or "") for item in evidence if item.get("id")}
    anchors_by_id = {
        str(item.get("id") or ""): (item.get("evidence_unit") or {}).get("source_anchor")
        for item in evidence
        if isinstance(item.get("evidence_unit"), dict)
    }
    markers = extract_citation_markers(report)
    unknown: list[str] = []
    weak: list[str] = []
    upgraded: list[str] = []
    for marker in markers:
        ref = marker["evidence_id"]
        if ref not in known_ids:
            unknown.append(ref)
            continue
        if marker["anchor_kind"] == "none":
            weak.append(ref)
            continue
        source_anchor = anchors_by_id.get(ref)
        if isinstance(source_anchor, dict) and not anchor_is_precise(source_anchor):
            # 声称有精确锚点，但底层证据其实没有解析出位置——属于定位升级。
            upgraded.append(ref)
    unique_refs = sorted({marker["evidence_id"] for marker in markers})
    return {
        "emitted_markers": len(markers),
        "unique_refs": unique_refs,
        "covered_evidence_ids": sorted(ref for ref in unique_refs if ref in known_ids),
        "unknown_refs": sorted(set(unknown)),
        "weak_anchor_refs": sorted(set(weak)),
        "anchor_upgraded_refs": sorted(set(upgraded)),
        "evidence_without_marker": sorted(known_ids - set(unique_refs)),
    }


def _normalise_text(value: str) -> str:
    return " ".join(value.split())


def _table_text(value: str, limit: int) -> str:
    return _normalise_text(value).replace("|", "\\|")[:limit]

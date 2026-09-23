"""Small, auditable paper-to-paper relation store.

This is intentionally not a graph database.  It records only relations a user
has confirmed (with a page or chunk anchor) and provides one-hop, modest
retrieval hints to the local paper store.  The relation is a navigation signal,
not proof of a scientific claim.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Iterable

from runtime_paths import RuntimePaths


_RELATION_ID = re.compile(r"paper-rel-[a-f0-9]{12}\Z")
_LOCK = RLock()
_RELATION_TYPES = {
    "method_similar": {"label": "方法相似", "weight": 0.004, "symmetric": True},
    "method_improves": {"label": "方法改进", "weight": 0.006, "symmetric": False},
    "experiment_comparable": {"label": "实验可比", "weight": 0.004, "symmetric": True},
    "result_conflicts": {"label": "结果冲突", "weight": 0.005, "symmetric": True},
    "explicit_citation": {"label": "明确引用", "weight": 0.006, "symmetric": False},
}


def relation_type_label(relation_type: str) -> str:
    """Return a Chinese display label without treating an unknown type as valid."""
    return str(_RELATION_TYPES.get(str(relation_type), {}).get("label") or "未知关系")


class PaperRelationStore:
    """Persist a concise set of one-hop paper relations in user runtime data."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.paths.ensure_initialized()
        self.path = paths.paper_relations_file

    def list(self, paper_id: str = "") -> list[dict[str, Any]]:
        """Return relation records, optionally limited to one paper, newest first."""
        normalized = str(paper_id or "").strip()
        with _LOCK:
            relations = self._read().get("relations") or []
            selected = [
                dict(item) for item in relations
                if not normalized or normalized in {
                    str(item.get("source_paper_id") or ""),
                    str(item.get("target_paper_id") or ""),
                }
            ]
        return sorted(selected, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    def upsert(
        self,
        *,
        source_paper: dict[str, Any],
        target_paper: dict[str, Any],
        relation_type: str,
        note: str,
        evidence: Iterable[dict[str, Any]],
        relation_id: str = "",
    ) -> tuple[dict[str, Any], str]:
        """Create or update an explicitly confirmed, evidence-anchored relation."""
        source_id = _paper_id(source_paper)
        target_id = _paper_id(target_paper)
        if not source_id or not target_id:
            raise ValueError("论文关系必须关联两篇已索引论文")
        if source_id == target_id:
            raise ValueError("论文关系的两端不能是同一篇论文")
        kind = str(relation_type or "").strip()
        if kind not in _RELATION_TYPES:
            raise ValueError("不支持的论文关系类型")
        note = " ".join(str(note or "").split())
        if not note:
            raise ValueError("论文关系需要一条可审阅的说明")
        if len(note) > 800:
            raise ValueError("论文关系说明不能超过 800 个字符")
        cleaned_evidence = _clean_evidence(evidence, source_id, target_id)
        now = _now()

        with _LOCK:
            payload = self._read()
            relations = list(payload.get("relations") or [])
            requested_id = str(relation_id or "").strip()
            index = None
            if requested_id:
                if not _RELATION_ID.fullmatch(requested_id):
                    raise ValueError("论文关系 ID 格式无效")
                index = next(
                    (i for i, item in enumerate(relations) if item.get("relation_id") == requested_id),
                    None,
                )
                if index is None:
                    raise ValueError("要更新的论文关系不存在")
            else:
                # Repeating the same ordered relation updates it instead of
                # silently accumulating duplicate retrieval signals.
                index = next(
                    (
                        i for i, item in enumerate(relations)
                        if item.get("source_paper_id") == source_id
                        and item.get("target_paper_id") == target_id
                        and item.get("relation_type") == kind
                    ),
                    None,
                )
            action = "updated" if index is not None else "created"
            existing = relations[index] if index is not None else {}
            record = {
                "relation_id": str(existing.get("relation_id") or f"paper-rel-{secrets.token_hex(6)}"),
                "source_paper_id": source_id,
                "source_title": _paper_title(source_paper),
                "target_paper_id": target_id,
                "target_title": _paper_title(target_paper),
                "relation_type": kind,
                "note": note,
                "evidence": cleaned_evidence,
                "created_at": str(existing.get("created_at") or now),
                "updated_at": now,
            }
            if index is None:
                relations.append(record)
            else:
                relations[index] = record
            self._write({"version": 1, "updated_at": now, "relations": relations})
        return dict(record), action

    def delete(self, relation_id: str) -> bool:
        """Delete one relation by ID.  The caller is responsible for confirmation."""
        normalized = str(relation_id or "").strip()
        if not _RELATION_ID.fullmatch(normalized):
            raise ValueError("论文关系 ID 格式无效")
        with _LOCK:
            payload = self._read()
            relations = list(payload.get("relations") or [])
            retained = [item for item in relations if item.get("relation_id") != normalized]
            if len(retained) == len(relations):
                return False
            self._write({"version": 1, "updated_at": _now(), "relations": retained})
            return True

    def remove_paper(self, paper_id: str) -> int:
        """Remove dangling edges when their indexed paper is deleted."""
        normalized = str(paper_id or "").strip()
        if not normalized:
            return 0
        with _LOCK:
            payload = self._read()
            relations = list(payload.get("relations") or [])
            retained = [
                item for item in relations
                if normalized not in {
                    str(item.get("source_paper_id") or ""),
                    str(item.get("target_paper_id") or ""),
                }
            ]
            removed = len(relations) - len(retained)
            if removed:
                self._write({"version": 1, "updated_at": _now(), "relations": retained})
            return removed

    def counts(self, paper_ids: Iterable[str]) -> dict[str, int]:
        """Return a relation count for each requested paper ID."""
        requested = {str(paper_id or "").strip() for paper_id in paper_ids}
        requested.discard("")
        counts = {paper_id: 0 for paper_id in requested}
        if not counts:
            return counts
        for relation in self.list():
            for paper_id in (relation.get("source_paper_id"), relation.get("target_paper_id")):
                if paper_id in counts:
                    counts[paper_id] += 1
        return counts

    def related_paper_boosts(self, seed_paper_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Return one-hop retrieval hints from the already relevant seed papers.

        Directed relations retain their meaning in storage.  Retrieval may also
        traverse one in reverse at a 20% discount so a cited baseline can still
        surface its documented improvement.  Hints are capped by max weight;
        they never accumulate into a graph-walk score.
        """
        seeds = {str(paper_id or "").strip() for paper_id in seed_paper_ids}
        seeds.discard("")
        if not seeds:
            return {}
        hints: dict[str, dict[str, Any]] = {}
        for relation in self.list():
            source_id = str(relation.get("source_paper_id") or "")
            target_id = str(relation.get("target_paper_id") or "")
            config = _RELATION_TYPES.get(str(relation.get("relation_type") or ""))
            if not source_id or not target_id or config is None:
                continue
            linked: list[tuple[str, str, float]] = []
            if source_id in seeds:
                linked.append((source_id, target_id, float(config["weight"])))
            if target_id in seeds:
                reverse_weight = float(config["weight"]) if config["symmetric"] else float(config["weight"]) * 0.8
                linked.append((target_id, source_id, reverse_weight))
            for seed_id, related_id, weight in linked:
                current = hints.setdefault(related_id, {
                    "boost": 0.0,
                    "relation_ids": [],
                    "relation_types": [],
                    "seed_paper_ids": [],
                })
                current["boost"] = max(float(current["boost"]), round(weight, 6))
                if relation["relation_id"] not in current["relation_ids"]:
                    current["relation_ids"].append(relation["relation_id"])
                if relation["relation_type"] not in current["relation_types"]:
                    current["relation_types"].append(relation["relation_type"])
                if seed_id not in current["seed_paper_ids"]:
                    current["seed_paper_ids"].append(seed_id)
        return hints

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "updated_at": "", "relations": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法读取论文关系数据；请从运行时备份恢复") from exc
        if not isinstance(value, dict) or not isinstance(value.get("relations", []), list):
            raise ValueError("论文关系数据格式无效；请从运行时备份恢复")
        return value

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _paper_id(paper: dict[str, Any]) -> str:
    return str(paper.get("paper_id") or "").strip()


def _paper_title(paper: dict[str, Any]) -> str:
    return " ".join(str(paper.get("title") or "未知论文").split())[:300]


def _clean_evidence(
    evidence: Iterable[dict[str, Any]],
    source_id: str,
    target_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise ValueError("论文关系至少需要一条页码或片段锚点证据")
    if len(evidence) > 8:
        raise ValueError("一条论文关系最多保存 8 条证据")
    cleaned = []
    for raw in evidence:
        if not isinstance(raw, dict):
            raise ValueError("论文关系证据格式无效")
        paper_id = str(raw.get("paper_id") or "").strip()
        if paper_id not in {source_id, target_id}:
            raise ValueError("关系证据必须定位到关系两端的论文")
        page = _optional_int(raw.get("page"), minimum=1)
        chunk_index = _optional_int(raw.get("chunk_index"), minimum=0)
        if page is None and chunk_index is None:
            raise ValueError("每条关系证据都需要 page 或 chunk_index 锚点")
        note = " ".join(str(raw.get("note") or "").split())
        if not note:
            raise ValueError("每条关系证据都需要简短说明")
        if len(note) > 500:
            raise ValueError("关系证据说明不能超过 500 个字符")
        cleaned.append({
            "paper_id": paper_id,
            "title": " ".join(str(raw.get("title") or "").split())[:300],
            "page": page,
            "chunk_index": chunk_index,
            "note": note,
        })
    return cleaned


def _optional_int(value: Any, *, minimum: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("论文关系证据的页码或片段序号必须是整数") from exc
    if parsed < minimum:
        raise ValueError("论文关系证据的页码或片段序号超出范围")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

"""Small, durable working memory for conversational references.

This module deliberately does *not* try to turn every chat message into a
long-term fact.  It keeps a bounded, session-scoped record of the entities and
ordered choices that a user can refer to on a following turn.  That state
survives transcript compaction, while the original transcript remains the
source of truth for detailed content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


REFERENCE_STATE_VERSION = 1
MAX_ENTITIES = 40
MAX_CANDIDATE_SETS = 8
MAX_ITEMS_PER_SET = 12
MAX_LABEL_CHARS = 180

_PAPER_TOKEN = re.compile(
    r"(?:论文|paper|文章)\s*(?:《(?P<quoted>[^》\n]{1,160})》|(?P<token>[A-Za-z][A-Za-z0-9_.:/+\-]{0,80}))",
    re.IGNORECASE,
)
_QUOTED_PAPER = re.compile(r"《(?P<title>[^》\n]{1,160})》")
_DIRECTION_HEADING = re.compile(
    r"(?:^|\r?\n)[ \t]*(?:[-*][ \t]*)?(?:【|\[)?[ \t]*"
    r"方向[ \t]*(?P<ordinal>[0-9一二三四五六七八九十]+)[ \t]*(?:】|\])?"
    r"[ \t]*(?:[:：\-—][ \t]*)?(?P<label>[^\r\n]{1,180})",
    re.IGNORECASE,
)
_ORDINAL_REFERENCE = re.compile(
    r"第\s*(?P<ordinal>[0-9]+|[一二三四五六七八九十])\s*"
    r"(?:个|项|条|篇|种)?\s*(?P<kind>方向|方案|思路|路线|论文|文章|paper|选项)",
    re.IGNORECASE,
)
_THIS_REFERENCE = re.compile(
    r"(?:这个|这条|上述|前面那个|刚才那个)\s*"
    r"(?P<kind>方向|方案|思路|路线|论文|文章|paper)?"
    r"|(?:该|此)\s*(?P<explicit_kind>方向|方案|思路|路线|论文|文章|paper)",
    re.IGNORECASE,
)
_CORRECTION_MARKER = re.compile(
    r"(?:记错|弄错|更正|纠正|改为|应(?:该)?是|其实是|不是)", re.IGNORECASE
)
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class ReferenceResolution:
    """The deterministic part of a reference interpretation for this turn."""

    status: str
    user_text: str = ""
    target_kind: str = ""
    target_label: str = ""
    source: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "user_text": self.user_text,
            "target_kind": self.target_kind,
            "target_label": self.target_label,
            "source": self.source,
            "reason": self.reason,
        }


def empty_reference_state() -> dict[str, Any]:
    return {
        "version": REFERENCE_STATE_VERSION,
        "next_entity_id": 1,
        "next_candidate_set_id": 1,
        "entities": [],
        "candidate_sets": [],
        "focus": {"paper_id": "", "direction_id": ""},
        "corrections": [],
        "last_resolution": {},
    }


def normalise_reference_state(value: Any) -> dict[str, Any]:
    """Return a bounded, forward-compatible reference state.

    Session state is local data, so a malformed old row must never block an
    otherwise valid chat turn.  Unknown future fields are intentionally ignored
    rather than interpreted as references.
    """

    base = empty_reference_state()
    if not isinstance(value, dict):
        return base
    try:
        base["next_entity_id"] = max(1, int(value.get("next_entity_id", 1)))
        base["next_candidate_set_id"] = max(1, int(value.get("next_candidate_set_id", 1)))
    except (TypeError, ValueError):
        pass

    entities: list[dict[str, str]] = []
    for item in value.get("entities", []):
        if not isinstance(item, dict):
            continue
        entity_id = _clean(item.get("id"), 80)
        kind = _kind(item.get("kind"))
        label = _clean(item.get("label"), MAX_LABEL_CHARS)
        if entity_id and kind and label:
            entities.append({"id": entity_id, "kind": kind, "label": label})
    base["entities"] = entities[-MAX_ENTITIES:]

    entity_ids = {item["id"] for item in base["entities"]}
    candidate_sets: list[dict[str, Any]] = []
    for item in value.get("candidate_sets", []):
        if not isinstance(item, dict):
            continue
        set_id = _clean(item.get("id"), 80)
        kind = _kind(item.get("kind"))
        source = _clean(item.get("source"), 32) or "unknown"
        raw_item_ids = item.get("item_ids", [])
        item_ids = [
            _clean(entity_id, 80) for entity_id in raw_item_ids
            if _clean(entity_id, 80) in entity_ids
        ][:MAX_ITEMS_PER_SET]
        if set_id and kind and item_ids:
            candidate_sets.append({
                "id": set_id,
                "kind": kind,
                "source": source,
                "item_ids": item_ids,
            })
    base["candidate_sets"] = candidate_sets[-MAX_CANDIDATE_SETS:]

    focus = value.get("focus", {})
    if isinstance(focus, dict):
        for key in ("paper_id", "direction_id"):
            entity_id = _clean(focus.get(key), 80)
            if entity_id in entity_ids:
                base["focus"][key] = entity_id

    corrections: list[dict[str, str]] = []
    for item in value.get("corrections", []):
        if not isinstance(item, dict):
            continue
        old = _clean(item.get("old"), MAX_LABEL_CHARS)
        new = _clean(item.get("new"), MAX_LABEL_CHARS)
        if old and new:
            corrections.append({"old": old, "new": new})
    base["corrections"] = corrections[-12:]

    resolution = value.get("last_resolution", {})
    if isinstance(resolution, dict):
        base["last_resolution"] = {
            key: _clean(resolution.get(key), MAX_LABEL_CHARS)
            for key in ("status", "user_text", "target_kind", "target_label", "source", "reason")
            if _clean(resolution.get(key), MAX_LABEL_CHARS)
        }
    return base


def observe_user_message(
    state: dict[str, Any] | None, user_text: str,
) -> tuple[dict[str, Any], ReferenceResolution]:
    """Record explicit user-side references and resolve this turn when possible."""

    updated = normalise_reference_state(state)
    text = _clean(user_text, 8_000)
    paper_ids = [_ensure_entity(updated, "paper", label) for label in _extract_papers(text)]
    paper_ids = _dedupe(paper_ids)
    if len(paper_ids) >= 2:
        _add_candidate_set(updated, "paper", paper_ids, source="user")
    if paper_ids:
        previous = _entity_by_id(updated, updated["focus"].get("paper_id", ""))
        selected = paper_ids[-1] if _CORRECTION_MARKER.search(text) else paper_ids[0]
        current = _entity_by_id(updated, selected)
        updated["focus"]["paper_id"] = selected
        if _CORRECTION_MARKER.search(text) and previous and current and previous["id"] != current["id"]:
            updated["corrections"].append({"old": previous["label"], "new": current["label"]})
            updated["corrections"] = updated["corrections"][-12:]

    resolution = _resolve_reference(updated, text)
    updated["last_resolution"] = resolution.as_dict()
    return updated, resolution


def observe_assistant_message(state: dict[str, Any] | None, assistant_text: str) -> dict[str, Any]:
    """Record explicitly-labelled direction choices from the visible reply.

    A system instruction asks the model to use ``【方向 N】`` headings when it
    offers multiple options.  Parsing only that explicit, user-visible form
    avoids inventing a numbered mapping from ordinary prose.
    """

    updated = normalise_reference_state(state)
    directions = _extract_direction_headings(_clean_multiline(assistant_text, 20_000))
    if len(directions) >= 2:
        ids = [_ensure_entity(updated, "direction", label) for _ordinal, label in directions]
        _add_candidate_set(updated, "direction", _dedupe(ids), source="assistant")
    elif len(directions) == 1:
        entity_id = _ensure_entity(updated, "direction", directions[0][1])
        updated["focus"]["direction_id"] = entity_id
    return updated


def render_reference_context(
    state: dict[str, Any] | None, resolution: ReferenceResolution | None = None,
) -> str:
    """Render bounded volatile prompt context, never a hidden source of facts."""

    current = normalise_reference_state(state)
    if not current["entities"]:
        return ""
    lines = ["[会话工作记忆：指代解析]"]
    paper = _entity_by_id(current, current["focus"].get("paper_id", ""))
    direction = _entity_by_id(current, current["focus"].get("direction_id", ""))
    if paper:
        lines.append(f"当前论文焦点：{paper['label']}")
    if direction:
        lines.append(f"当前方向焦点：{direction['label']}")

    for candidate_set in current["candidate_sets"][-3:]:
        items = [
            _entity_by_id(current, entity_id)
            for entity_id in candidate_set["item_ids"]
        ]
        labels = [item["label"] for item in items if item]
        if labels:
            title = "方向候选" if candidate_set["kind"] == "direction" else "论文候选"
            rendered = "；".join(f"{index}. {label}" for index, label in enumerate(labels, 1))
            lines.append(f"{title}（来源：{candidate_set['source']}）：{rendered}")
    if current["corrections"]:
        correction = current["corrections"][-1]
        lines.append(f"最近更正：{correction['old']} → {correction['new']}")

    current_resolution = resolution or _resolution_from_dict(current.get("last_resolution", {}))
    if current_resolution.status == "resolved":
        lines.append(
            f"本轮已解析：{current_resolution.user_text} → {current_resolution.target_label}"
            f"（{current_resolution.source}）。不要改写为其他候选。"
        )
    elif current_resolution.status == "clarify":
        lines.append(
            "本轮指代没有唯一映射："
            f"{current_resolution.reason}。先用一句话请用户澄清，不要自行选择。"
        )
    lines.append(
        "候选编号仅在其最近候选组中有效。若提出多个研究方向，请使用"
        "【方向 1】、【方向 2】等清晰标题。"
    )
    return "\n".join(lines)


def _resolve_reference(state: dict[str, Any], text: str) -> ReferenceResolution:
    ordinal_match = _ORDINAL_REFERENCE.search(text)
    if ordinal_match:
        ordinal = _ordinal_value(ordinal_match.group("ordinal"))
        kind = _kind(ordinal_match.group("kind"))
        if ordinal is None:
            return ReferenceResolution("clarify", text, reason="编号无法识别")
        candidate_set = _latest_candidate_set(state, kind)
        source = "最近候选组"
        # A user comparing multiple papers often calls each paper a "direction"
        # on the following turn.  Use that fallback only when there is no newer
        # explicit direction set; explicit headings always take precedence.
        if candidate_set is None and kind == "direction":
            candidate_set = _latest_candidate_set(state, "paper")
            source = "用户列出的论文候选"
        if candidate_set is None:
            return ReferenceResolution(
                "clarify", text, reason="没有可对应的已编号候选项",
            )
        item_ids = candidate_set["item_ids"]
        if ordinal < 1 or ordinal > len(item_ids):
            return ReferenceResolution(
                "clarify", text, reason=f"候选项只有 {len(item_ids)} 个",
            )
        entity = _entity_by_id(state, item_ids[ordinal - 1])
        if not entity:
            return ReferenceResolution("clarify", text, reason="候选项已失效")
        _set_focus(state, entity)
        return ReferenceResolution(
            "resolved", text, entity["kind"], entity["label"], source,
        )

    this_match = _THIS_REFERENCE.search(text)
    if not this_match:
        return ReferenceResolution("none", text)
    kind = _kind(this_match.group("kind") or this_match.group("explicit_kind")) or "direction"
    if kind == "direction":
        direction = _entity_by_id(state, state["focus"].get("direction_id", ""))
        if direction:
            return ReferenceResolution("resolved", text, "direction", direction["label"], "当前方向焦点")
        latest_directions = _latest_candidate_set(state, "direction")
        if latest_directions:
            if len(latest_directions["item_ids"]) == 1:
                entity = _entity_by_id(state, latest_directions["item_ids"][0])
                if entity:
                    _set_focus(state, entity)
                    return ReferenceResolution("resolved", text, "direction", entity["label"], "唯一方向候选")
            return ReferenceResolution("clarify", text, reason="当前存在多个方向候选")
        paper = _entity_by_id(state, state["focus"].get("paper_id", ""))
        if paper:
            return ReferenceResolution(
                "resolved", text, "paper", paper["label"], "当前论文焦点",
            )
        return ReferenceResolution("clarify", text, reason="没有当前论文或方向焦点")

    entity = _entity_by_id(state, state["focus"].get("paper_id", ""))
    if entity:
        return ReferenceResolution("resolved", text, "paper", entity["label"], "当前论文焦点")
    return ReferenceResolution("clarify", text, reason="没有当前论文焦点")


def _extract_papers(text: str) -> list[str]:
    values: list[str] = []
    for match in _PAPER_TOKEN.finditer(text):
        quoted = match.group("quoted")
        token = match.group("token")
        label = f"论文《{quoted.strip()}》" if quoted else f"论文{(token or '').strip()}"
        if label != "论文":
            values.append(label)
    if not values:
        values.extend(f"论文《{match.group('title').strip()}》" for match in _QUOTED_PAPER.finditer(text))
    return _dedupe(values)


def _extract_direction_headings(text: str) -> list[tuple[int, str]]:
    values: list[tuple[int, str]] = []
    for match in _DIRECTION_HEADING.finditer(text):
        ordinal = _ordinal_value(match.group("ordinal"))
        label = _clean(match.group("label"), MAX_LABEL_CHARS)
        if ordinal is not None and label:
            values.append((ordinal, label))
    # Heading order is the source of truth.  Requiring 1..N avoids accepting a
    # prose fragment such as "方向 3：" as a complete numbered menu.
    expected = list(range(1, len(values) + 1))
    return values if [ordinal for ordinal, _label in values] == expected else []


def _ensure_entity(state: dict[str, Any], kind: str, label: str) -> str:
    clean_kind = _kind(kind)
    clean_label = _clean(label, MAX_LABEL_CHARS)
    for entity in state["entities"]:
        if entity["kind"] == clean_kind and _same_label(entity["label"], clean_label):
            return entity["id"]
    entity_id = f"{clean_kind}-{state['next_entity_id']}"
    state["next_entity_id"] += 1
    state["entities"].append({"id": entity_id, "kind": clean_kind, "label": clean_label})
    state["entities"] = state["entities"][-MAX_ENTITIES:]
    return entity_id


def _add_candidate_set(state: dict[str, Any], kind: str, item_ids: list[str], *, source: str) -> None:
    if not item_ids:
        return
    candidate_set = {
        "id": f"{kind}-set-{state['next_candidate_set_id']}",
        "kind": kind,
        "source": _clean(source, 32) or "unknown",
        "item_ids": item_ids[:MAX_ITEMS_PER_SET],
    }
    state["next_candidate_set_id"] += 1
    state["candidate_sets"].append(candidate_set)
    state["candidate_sets"] = state["candidate_sets"][-MAX_CANDIDATE_SETS:]


def _latest_candidate_set(state: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for candidate_set in reversed(state["candidate_sets"]):
        if candidate_set["kind"] == kind:
            return candidate_set
    return None


def _set_focus(state: dict[str, Any], entity: dict[str, str]) -> None:
    if entity["kind"] == "direction":
        state["focus"]["direction_id"] = entity["id"]
    elif entity["kind"] == "paper":
        state["focus"]["paper_id"] = entity["id"]


def _entity_by_id(state: dict[str, Any], entity_id: str) -> dict[str, str] | None:
    return next((item for item in state["entities"] if item["id"] == entity_id), None)


def _resolution_from_dict(value: Any) -> ReferenceResolution:
    if not isinstance(value, dict):
        return ReferenceResolution("none")
    return ReferenceResolution(
        status=_clean(value.get("status"), 32) or "none",
        user_text=_clean(value.get("user_text"), MAX_LABEL_CHARS),
        target_kind=_clean(value.get("target_kind"), 32),
        target_label=_clean(value.get("target_label"), MAX_LABEL_CHARS),
        source=_clean(value.get("source"), MAX_LABEL_CHARS),
        reason=_clean(value.get("reason"), MAX_LABEL_CHARS),
    )


def _ordinal_value(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return _CHINESE_NUMBERS.get(value)


def _kind(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"论文", "文章", "paper", "papers"}:
        return "paper"
    if normalized in {"方向", "方案", "思路", "路线", "direction", "directions"}:
        return "direction"
    return ""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_multiline(value: Any, limit: int) -> str:
    """Normalise whitespace while preserving heading boundaries."""
    lines = [" ".join(line.split()) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def _same_label(left: str, right: str) -> bool:
    return "".join(left.casefold().split()) == "".join(right.casefold().split())


def _dedupe(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = "".join(value.casefold().split())
        if key and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique

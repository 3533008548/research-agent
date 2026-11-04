"""用户画像的 Markdown 持久化管理器。"""

import re
import threading
import uuid
from pathlib import Path
from datetime import datetime

from runtime_paths import get_runtime_paths


class ProfileManager:
    """读写 ``APP_DATA_DIR/primary/profile.md``。"""

    SECTIONS = ("研究方向", "偏好设置", "活跃问题", "已读论文")
    ACTIONS = (
        "add_direction", "add_question", "add_paper", "set_preference",
        "remove_entry", "replace_entry",
    )

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else get_runtime_paths().profile_path
        self._lock = threading.RLock()
        self._ensure_exists()

    def _ensure_exists(self):
        with self._lock:
            if self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write(
                "# 用户画像\n\n"
                "## 研究方向\n\n"
                "## 偏好设置\n"
                "- 模型: deepseek-flash\n"
                "- 论文源: OpenAlex（通用）+ arXiv（预印本）\n"
                "- 语言: 中文\n\n"
                "## 活跃问题\n\n"
                "## 已读论文\n\n"
                "*此文件由 Agent 自动维护，你也可以手动编辑。*\n",
                encoding="utf-8",
            )

    def _write(self, text: str, *, encoding: str = "utf-8") -> None:
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(text, encoding=encoding)
            temp_path.replace(self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    def read(self) -> str:
        with self._lock:
            return self.path.read_text(encoding="utf-8")

    def summary(self) -> str:
        """生成注入系统提示词的画像摘要。"""
        sections = {"研究方向": [], "偏好设置": [], "活跃问题": []}
        current = None
        for line in self.read().split("\n"):
            if line.startswith("## 研究方向"):
                current = "研究方向"
            elif line.startswith("## 偏好设置"):
                current = "偏好设置"
            elif line.startswith("## 活跃问题"):
                current = "活跃问题"
            elif line.startswith("##"):
                current = None
            elif current and line.strip() and not line.startswith("#"):
                sections[current].append(line.strip())

        parts = []
        if sections["研究方向"]:
            parts.append(f"研究方向: {', '.join(sections['研究方向'])}")
        if sections["偏好设置"]:
            parts.append(f"偏好: {'; '.join(sections['偏好设置'])}")
        if sections["活跃问题"]:
            parts.append(f"活跃问题: {', '.join(sections['活跃问题'][:3])}")
        return " | ".join(parts) if parts else ""

    def to_context(self) -> str:
        return "[用户画像 — 以下信息用于个性化回答]\n" + self.read() + "\n[画像结束]"

    @staticmethod
    def _single_line(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 必须是非空文本")
        if len(value.splitlines()) != 1 or "\r" in value or "\n" in value:
            raise ValueError(f"{field} 必须是单行文本，不能包含换行")
        return value.strip()

    @classmethod
    def _section_bounds(cls, lines: list[str], section: str) -> tuple[int, int]:
        if section not in cls.SECTIONS:
            raise ValueError(f"未知画像章节: {section}；可选: {', '.join(cls.SECTIONS)}")
        starts = [i for i, line in enumerate(lines) if line.strip() == f"## {section}"]
        if len(starts) != 1:
            raise ValueError(f"画像章节「{section}」缺失或重复，请先修复章节标题")
        start = starts[0] + 1
        end = next(
            (i for i in range(start, len(lines)) if re.match(r"^#{1,6}(?:\s|$)", lines[i].strip())),
            len(lines),
        )
        return start, end

    @staticmethod
    def _entry_text(line: str) -> str | None:
        # Only top-level list items are managed; preserve hand-written notes.
        return line[2:].strip() if line.startswith("- ") else None

    def read_section(self, section: str) -> list[str]:
        """返回条目原文（不含列表前缀），供精确删除和修正使用。"""
        with self._lock:
            lines = self.read().split("\n")
            start, end = self._section_bounds(lines, section)
            return [text for line in lines[start:end] if (text := self._entry_text(line))]

    def _save_lines(self, before: list[str], after: list[str]) -> bool:
        if before == after:
            return False
        text = "\n".join(after)
        stamp = f"*最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
        text, count = re.subn(r"(?m)^\*最后更新:[^\n]*\*$", lambda _: stamp, text)
        if not count:
            text += f"\n\n{stamp}"
        self._write(text)
        return True

    def _add_entry(self, section: str, content: str) -> bool:
        content = self._single_line(content, "content")
        with self._lock:
            lines = self.read().split("\n")
            start, end = self._section_bounds(lines, section)
            matches = [i for i in range(start, end) if self._entry_text(lines[i]) == content]
            if matches:
                # Collapse legacy duplicate copies of this item as well.
                duplicates = set(matches[1:])
                updated = [line for i, line in enumerate(lines) if i not in duplicates]
            else:
                updated = lines[:start] + [f"- {content}"] + lines[start:]
            return self._save_lines(lines, updated)

    def add_research_direction(self, direction: str) -> bool:
        return self._add_entry("研究方向", direction)

    def add_active_question(self, question: str) -> bool:
        return self._add_entry("活跃问题", question)

    def add_paper(self, title: str, summary: str) -> bool:
        title = self._single_line(title, "论文标题")
        summary = self._single_line(summary, "论文摘要")
        return self._add_entry("已读论文", f"{title}: {summary}")

    @classmethod
    def _preference_pair(cls, content: str) -> tuple[str, str]:
        key, separator, value = content.partition(": ")
        if not separator:
            raise ValueError("偏好格式必须为「键: 值」（英文冒号后有空格）")
        if ":" in key:
            raise ValueError("偏好键不能包含英文冒号")
        return cls._single_line(key, "偏好键"), cls._single_line(value, "偏好值")

    def set_preference(self, key: str, value: str) -> bool:
        key = self._single_line(key, "偏好键")
        value = self._single_line(value, "偏好值")
        if ":" in key:
            raise ValueError("偏好键不能包含英文冒号")
        with self._lock:
            lines = self.read().split("\n")
            start, end = self._section_bounds(lines, "偏好设置")
            matches = [
                i for i in range(start, end)
                if (entry := self._entry_text(lines[i])) is not None
                and entry.partition(":")[0].strip() == key and ":" in entry
            ]
            updated = list(lines)
            if matches:
                updated[matches[0]] = f"- {key}: {value}"
                duplicates = set(matches[1:])
                updated = [line for i, line in enumerate(updated) if i not in duplicates]
            else:
                updated.insert(start, f"- {key}: {value}")
            return self._save_lines(lines, updated)

    def remove_entry(self, section: str, content: str) -> bool:
        return self._change_entry(section, content, replacement=None)

    def replace_entry(self, section: str, content: str, replacement: str) -> bool:
        replacement = self._single_line(replacement, "replacement")
        if section == "偏好设置":
            self._preference_pair(replacement)
        return self._change_entry(section, content, replacement=replacement)

    def _change_entry(self, section: str, content: str, replacement: str | None) -> bool:
        content = self._single_line(content, "content")
        with self._lock:
            lines = self.read().split("\n")
            start, end = self._section_bounds(lines, section)
            matches = [i for i in range(start, end) if self._entry_text(lines[i]) == content]
            if not matches:
                raise ValueError("未找到精确匹配的画像条目；请先 read_profile 读取该章节原文")
            removed = set(matches)
            preference_key = (
                self._preference_pair(replacement)[0]
                if replacement is not None and section == "偏好设置" else None
            )
            if replacement is not None:
                for i in range(start, end):
                    entry = self._entry_text(lines[i])
                    if i in removed or entry is None:
                        continue
                    if entry == replacement:
                        removed.add(i)
                    elif preference_key is not None and entry.partition(":")[0].strip() == preference_key:
                        raise ValueError("新偏好键已有其他值，请先读取并明确要修正的条目")
            updated = []
            for i, line in enumerate(lines):
                if i == matches[0] and replacement is not None:
                    updated.append(f"- {replacement}")
                if i not in removed:
                    updated.append(line)
            return self._save_lines(lines, updated)

    def update_from_agent(
        self, action: str, content: str, *, section: str = "", replacement: str = "",
    ) -> bool:
        """Agent 更新入口；返回是否实际变更，未知操作或无匹配项均报错。"""
        if action not in self.ACTIONS:
            raise ValueError(f"未知画像操作: {action}；可选: {', '.join(self.ACTIONS)}")
        content = self._single_line(content, "content")
        if action not in ("remove_entry", "replace_entry") and (section or replacement):
            raise ValueError("section/replacement 仅用于 remove_entry 或 replace_entry")
        if action == "remove_entry" and replacement:
            raise ValueError("remove_entry 不接受 replacement")
        if action == "add_direction":
            return self.add_research_direction(content)
        elif action == "add_question":
            return self.add_active_question(content)
        elif action == "add_paper":
            title, separator, summary = content.partition(": ")
            if not separator:
                raise ValueError("论文格式必须为「标题: 摘要」（英文冒号后有空格）")
            return self.add_paper(title, summary)
        elif action == "set_preference":
            return self.set_preference(*self._preference_pair(content))
        elif action == "remove_entry":
            return self.remove_entry(section, content)
        return self.replace_entry(section, content, replacement)

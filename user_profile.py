"""用户画像的 Markdown 持久化管理器。"""

import re
import threading
import uuid
from pathlib import Path
from datetime import datetime

from runtime_paths import get_runtime_paths


class ProfileManager:
    """读写 ``APP_DATA_DIR/primary/profile.md``。"""

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
                "- 模型: deepseek-v4-flash\n"
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

    def _update_section(self, section: str, content: str, append: bool = True):
        with self._lock:
            lines = self.read().split("\n")
            new_lines = []
            in_section = False
            done = False
            for line in lines:
                if line.startswith(f"## {section}"):
                    in_section = True
                    new_lines.extend((line, content))
                    if not append:
                        done = True
                elif in_section and line.startswith("##"):
                    in_section = False
                    if append and not done:
                        new_lines.append(content)
                        done = True
                    new_lines.append(line)
                elif in_section and (line.strip() == "" or not append):
                    continue
                else:
                    new_lines.append(line)
            if in_section and append and not done:
                new_lines.append(content)
            text = "\n".join(new_lines)
            text = re.sub(r'\*最后更新.*\*', f'*最后更新: {datetime.now().strftime("%Y-%m-%d %H:%M")}*', text)
            if "*最后更新" not in text:
                text += f"\n\n*最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
            self._write(text)

    def add_research_direction(self, direction: str):
        self._update_section("研究方向", f"- {direction}")

    def add_active_question(self, question: str):
        self._update_section("活跃问题", f"- {question}")

    def add_paper(self, title: str, summary: str):
        self._update_section("已读论文", f"- {title}: {summary}")

    def set_preference(self, key: str, value: str):
        with self._lock:
            lines = self.read().split("\n")
            new_lines = []
            found = False
            for line in lines:
                if line.strip().startswith(f"- {key}:"):
                    new_lines.append(f"- {key}: {value}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                for index, line in enumerate(new_lines):
                    if line.startswith("## 偏好设置"):
                        new_lines.insert(index + 1, f"- {key}: {value}")
                        break
            self._write("\n".join(new_lines))

    def update_from_agent(self, action: str, content: str):
        """Agent 调用的统一更新接口。"""
        if action == "add_direction":
            self.add_research_direction(content)
        elif action == "add_question":
            self.add_active_question(content)
        elif action == "add_paper":
            title, _, summary = content.partition(": ")
            self.add_paper(title, summary)
        elif action == "set_preference":
            key, _, value = content.partition(": ")
            self.set_preference(key.strip(), value.strip())

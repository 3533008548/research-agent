"""
👤 用户画像 — Markdown 持久化偏好与研究方向

文件: APP_DATA_DIR/primary/profile.md（人 + Agent 共维护）

结构:
  # 用户画像
  ## 研究方向
  ## 偏好设置
  ## 活跃问题
  ## 已读论文

用法:
  pm = ProfileManager()
  pm.add_research_direction("TSN调度中的强化学习")
  pm.set_preference("模型", "deepseek-v4-flash")
  pm.add_active_question("L_total 是否考虑延迟约束？")
  pm.add_paper("Attention Is All You Need", "Transformer, 注意力机制")
  summary = pm.summary()  # 注入系统提示词的摘要
"""

import os
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

from runtime_paths import get_runtime_paths


class ProfileManager:
    """用户画像管理器 — 读写 profile.md"""

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else get_runtime_paths().profile_path
        self._ensure_exists()

    def _ensure_exists(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
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

    # ── 读取 ──

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def summary(self) -> str:
        """生成注入系统提示词的画像摘要"""
        text = self.read()
        lines = text.split("\n")
        # 提取 ## 研究方向和 ## 偏好设置之间的关键行
        sections = {"研究方向": [], "偏好设置": [], "活跃问题": []}
        current = None
        for line in lines:
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
            parts.append(f"活跃问题: {'; '.join(sections['活跃问题'][:3])}")
        return " | ".join(parts) if parts else ""

    def to_context(self) -> str:
        """生成注入对话上下文的完整画像文本"""
        return (
            "[用户画像 — 以下信息用于个性化回答]\n"
            + self.read()
            + "\n[画像结束]"
        )

    # ── 写入 ──

    def _update_section(self, section: str, content: str, append: bool = True):
        """更新特定 ## 章节"""
        text = self.read()
        lines = text.split("\n")
        new_lines = []
        in_section = False
        done = False
        for line in lines:
            if line.startswith(f"## {section}"):
                in_section = True
                new_lines.append(line)
                if append:
                    new_lines.append(content)
                else:
                    new_lines.append(content)
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
        # 更新时间戳
        text = "\n".join(new_lines)
        text = re.sub(r'\*最后更新.*\*', f'*最后更新: {datetime.now().strftime("%Y-%m-%d %H:%M")}*', text)
        if '*最后更新' not in text:
            text += f"\n\n*最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
        self.path.write_text(text, encoding="utf-8")

    def add_research_direction(self, direction: str):
        self._update_section("研究方向", f"- {direction}")

    def add_active_question(self, question: str):
        self._update_section("活跃问题", f"- {question}")

    def add_paper(self, title: str, summary: str):
        entry = f"- {title}: {summary}"
        self._update_section("已读论文", entry)

    def set_preference(self, key: str, value: str):
        text = self.read()
        lines = text.split("\n")
        new_lines = []
        found = False
        for line in lines:
            if line.strip().startswith(f"- {key}:"):
                new_lines.append(f"- {key}: {value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            # 插入到偏好设置节
            for i, line in enumerate(new_lines):
                if line.startswith("## 偏好设置"):
                    new_lines.insert(i + 1, f"- {key}: {value}")
                    break
        self.path.write_text("\n".join(new_lines), encoding="utf-8")

    def update_from_agent(self, action: str, content: str):
        """Agent 调用的统一更新接口"""
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
        elif action == "summary":
            pass  # read-only

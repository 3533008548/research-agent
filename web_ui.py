"""
🌐 科研助手 Web UI — 极简聊天界面

基于 Gradio ChatInterface，支持：
  - 斜杠命令: /model /tokens /indexed /new /help
  - 笔记命令: /note topics /note add /note list /note del
  - ChromaDB RAG 论文检索
  - 公式实时 MathJax 渲染
  - 状态栏实时更新 + 弹出笔记面板

启动:
  python web_ui.py
  python web_ui.py -m deepseek-v4-pro --port 8080
"""

import argparse
import os
import re
import sys
from pathlib import Path

# ── Windows GBK 兼容 ──
if sys.platform == "win32":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import gradio as gr
from dotenv import load_dotenv

for env_path in [".env", "../cli-chatbot/.env", str(Path.home() / ".env")]:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        break
load_dotenv()

from research_agent import ResearchAgent
from notes import NoteStore


def build_ui():
    parser = argparse.ArgumentParser(description="科研助手 Web UI")
    parser.add_argument(
        "-m", "--model",
        default=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        help="模型名称",
    )
    parser.add_argument("--port", type=int, default=7860, help="端口号")
    parser.add_argument("--no-rag", action="store_true", help="禁用 RAG")
    args = parser.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("❌ 未设置 DEEPSEEK_API_KEY")
        sys.exit(1)

    print(f"🤖 模型: {args.model}")
    agent = ResearchAgent(model=args.model, enable_rag=not args.no_rag)
    notes = NoteStore()
    current_topic = {"name": "默认"}  # 当前选中话题

    # ── 帮助文本 ──
    HELP_TEXT = """
**命令列表**

| 命令 | 说明 |
|------|------|
| `/model` | 模型、Token、RAG 状态 |
| `/tokens` | Token 消耗详细统计 |
| `/indexed` | 已索引论文列表 |
| `/new` | 开始新对话 |
| `/note topics` | 列出所有笔记话题 |
| `/note topic <名>` | 切换当前话题 |
| `/note add <内容>` | 添加笔记 |
| `/note list` | 列出当前话题笔记 |
| `/note del <编号>` | 删除笔记 |
| `/note link <id> <论文名>` | 笔记关联论文 |
| `/help` | 显示此帮助 |
    """.strip()

    # ═══ 聊天函数 ═══

    def chat_fn(message: str, history: list[list[str]]) -> str:
        msg = message.strip()

        # ── 基础命令 ──
        if msg == "/help":
            return HELP_TEXT

        if msg == "/model":
            tu = agent.token_usage
            rag = (
                f"{agent.paper_store.paper_count} 篇论文"
                if agent.paper_store else "未启用"
            )
            return (
                f"**模型**: {agent.model}\n\n"
                f"**Tokens**: {tu['total']:,} ({tu['calls']} 次调用)\n\n"
                f"**RAG**: {rag}"
            )

        if msg == "/tokens":
            tu = agent.token_usage
            price = {"deepseek-chat": (0.27, 1.10), "deepseek-reasoner": (0.55, 2.19)}
            p = price.get(agent.model, (0.27, 1.10))
            cost = (tu["prompt"] / 1e6 * p[0]) + (tu["completion"] / 1e6 * p[1])
            return (
                f"**Token 消耗统计**\n\n"
                f"| 指标 | 数值 |\n|------|------|\n"
                f"| API 调用 | {tu['calls']} 次 |\n"
                f"| 输入 tokens | {tu['prompt']:,} |\n"
                f"| 输出 tokens | {tu['completion']:,} |\n"
                f"| 合计 | {tu['total']:,} |\n"
                f"| 估算费用 | ${cost:.4f} |"
            )

        if msg == "/indexed":
            if not agent.paper_store:
                return "RAG 未启用。"
            papers = agent.paper_store.list_papers()
            if not papers:
                return "📭 尚未索引论文。"
            lines = [f"**已索引论文** ({len(papers)} 篇)\n"]
            for i, p in enumerate(papers, 1):
                lines.append(f"{i}. {p['title']} ({p['chunks']} chunks)")
            return "\n".join(lines)

        if msg in ("/new", "/reset"):
            agent.reset()
            return "✅ 已开始新对话。"

        # ── 笔记命令 ──
        if msg.startswith("/note"):
            return _handle_note_cmd(msg)

        # ── 正常对话 ──
        # 注入话题上下文：关联当前话题的论文和笔记
        ctx = _build_context()
        reply = agent.step(msg, context=ctx if ctx else None)
        reply = re.sub(
            r'(?<!\$)\$([^\$]*[\\_^][^\$]*)\$(?!\$)',
            r'$$\1$$',
            reply,
        )
        return reply

    def _handle_note_cmd(msg: str) -> str:
        parts = msg[5:].strip()  # 去掉 "/note"
        if not parts:
            return "用法:\n- `/note topics` 查看话题\n- `/note topic <名>` 切换话题\n- `/note add <内容>` 添加笔记\n- `/note list` 列出笔记\n- `/note del <编号>` 删除笔记"

        if parts == "topics":
            topics = notes.list_topics()
            if not topics:
                return "📭 还没有笔记话题。"
            lines = [f"**笔记话题** ({len(topics)})\n"]
            for t in topics:
                lines.append(f"- {t['name']}（{t['count']} 条笔记）")
            return "\n".join(lines)

        if parts.startswith("topic "):
            name = parts[6:].strip()
            current_topic["name"] = name
            notes.ensure_topic(name)
            return f"✅ 当前笔记话题: **{name}**"

        if parts.startswith("add "):
            content = parts[4:].strip()
            if not content:
                return "用法: `/note add <笔记内容>`"
            tid = notes.add(current_topic["name"], content)
            return f"✅ 笔记 #{tid} 已保存到话题「{current_topic['name']}」"

        if parts == "list":
            all_notes = notes.list_notes(current_topic["name"])
            if not all_notes:
                return f"📭 话题「{current_topic['name']}」还没有笔记。"
            lines = [f"**{current_topic['name']}** 的笔记：\n"]
            for n in all_notes:
                preview = n['content'].replace('\n', ' ')[:80]
                lines.append(f"#{n['id']}  {preview}")
            return "\n".join(lines)

        if parts.startswith("del "):
            nid = parts[4:].strip()
            if not nid.isdigit():
                return "用法: `/note del <编号>`"
            ok = notes.delete(int(nid))
            return f"🗑️ 笔记 #{nid} 已删除。" if ok else f"❌ 未找到笔记 #{nid}"

        if parts.startswith("link "):
            args_link = parts[5:].strip().split(" ", 1)
            if len(args_link) < 2 or not args_link[0].isdigit():
                return "用法: `/note link <笔记编号> <论文标题>`"
            nid = int(args_link[0])
            paper = args_link[1].strip()
            note = notes.get(nid)
            if not note:
                return f"❌ 未找到笔记 #{nid}"
            notes.set_paper(nid, paper)
            return f"✅ 笔记 #{nid} 已关联论文「{paper}」"

        return f"❌ 未知命令: /note {parts}\n输入 `/note` 查看用法。"

    # ═══ 上下文构建（打通三库关联） ═══

    def _build_context() -> str | None:
        """构建当前话题的上下文，注入 Agent"""
        topic = current_topic["name"]
        if topic == "默认":
            return None

        parts = [f"[当前研究话题: {topic}]\n"]

        # 关联论文
        if agent.paper_store:
            papers = agent.paper_store.list_papers()
            if papers:
                names = [p["title"] for p in papers[:5]]
                parts.append(f"已索引论文 ({len(papers)} 篇): " + ", ".join(names))

        # 关联笔记
        topic_notes = notes.list_notes(topic)
        if topic_notes:
            recent = topic_notes[-3:]  # 最近 3 条
            parts.append("\n最近笔记:")
            for n in recent:
                preview = n["content"].replace("\n", " ")[:100]
                parts.append(f"  - {preview}")

        return "\n".join(parts)

    def refresh_status(history):
        tu = agent.token_usage
        rag = f"{agent.paper_store.paper_count} 篇" if agent.paper_store else "关"
        last = tu.get("last_prompt", 0)
        pct = min(round(last / 65536 * 100), 99) if last else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        return (
            f"**模型**: {agent.model} | "
            f"**上下文**: `{bar}` {pct}% ({last:,}/65K) | "
            f"**Tokens**: {tu['total']:,} | "
            f"**话题**: {current_topic['name']} | "
            f"**RAG**: {rag}"
        )

    # ═══ 笔记面板函数 ═══

    def note_topic_list():
        topics = notes.list_topics()
        return gr.update(choices=[t["name"] for t in topics], value=current_topic["name"])

    def note_save(topic_name: str, content: str):
        if not content.strip():
            return gr.update(), note_list_html(topic_name)
        notes.add(topic_name, content.strip())
        return gr.update(value=""), note_list_html(topic_name)

    def note_list_html(topic_name: str = None) -> str:
        t = topic_name or current_topic["name"]
        all_notes = notes.list_notes(t)
        if not all_notes:
            return f"<p style='color:#999;'>📭 话题「{t}」还没有笔记</p>"
        html = "<div style='max-height:300px;overflow-y:auto;'>"
        for n in all_notes:
            content = n["content"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html += (
                f"<div style='border-bottom:1px solid #eee;padding:6px 0;'>"
                f"<small style='color:#999;'>{n['created_at'][:16]}</small>"
                f"<p style='margin:2px 0;white-space:pre-wrap;'>{content}</p>"
                f"</div>"
            )
        html += "</div>"
        return html

    def note_topic_change(topic_name: str):
        current_topic["name"] = topic_name
        return note_list_html(topic_name)

    # ═══ 构建界面 ═══

    with gr.Blocks(title="🔬 Research Assistant") as demo:

        gr.HTML(
            '<div class="main-header">'
            '<h1>🔬 Research Assistant</h1>'
            '<p style="color:#888;font-size:0.9rem;">'
            '搜索论文 · 阅读 PDF · RAG 检索 · 分析方向'
            '</p></div>'
        )

        status = gr.Markdown(refresh_status([]), elem_classes=["status-bar"])

        chat = gr.ChatInterface(
            fn=chat_fn,
            chatbot=gr.Chatbot(height=450, render_markdown=True),
            textbox=gr.Textbox(
                placeholder="输入问题或命令... /help 查看帮助",
                container=False,
                scale=7,
                submit_btn="发送",
                stop_btn="停止",
            ),
            title=None,
            description=None,
            examples=[
                "搜索 diffusion model 在网络调度中的最新论文",
                "对比已读论文中的损失函数设计",
                "/note add 研究方向总结：当前主流方法分为三类...",
            ],
            cache_examples=False,
        )

        chat.chatbot.change(
            fn=refresh_status,
            inputs=[chat.chatbot],
            outputs=[status],
        )

        # ── 弹出笔记面板 ──
        with gr.Accordion("📝 笔记", open=False):
            with gr.Row():
                topic_dd = gr.Dropdown(
                    label="话题",
                    choices=[t["name"] for t in notes.list_topics()],
                    value=current_topic["name"],
                    allow_custom_value=True,
                    scale=2,
                )
                note_input = gr.Textbox(
                    label="新笔记",
                    placeholder="输入笔记内容（支持长文本）...",
                    lines=3,
                    scale=5,
                )
                note_save_btn = gr.Button("保存", scale=1, variant="primary")

            note_display = gr.HTML(
                value=note_list_html(current_topic["name"]),
                elem_classes=["note-panel"],
            )

            # 事件绑定
            note_save_btn.click(
                fn=note_save,
                inputs=[topic_dd, note_input],
                outputs=[note_input, note_display],
            )
            topic_dd.change(
                fn=note_topic_change,
                inputs=[topic_dd],
                outputs=[note_display],
            )
            # 页面加载时刷新话题列表
            demo.load(fn=note_topic_list, outputs=[topic_dd])

    demo.launch(
        server_port=args.port,
        share=False,
        show_error=True,
        theme=gr.themes.Soft(),
        css="""
        .main-header { text-align: center; padding: 1rem 0; }
        .main-header h1 { font-size: 1.5rem; font-weight: 600; }
        .status-bar { padding: 0.5rem 1rem; font-size: 0.8rem; color: #666; }
        .note-panel { padding: 0.5rem 0; }
        footer { display: none !important; }
        """,
    )


if __name__ == "__main__":
    build_ui()

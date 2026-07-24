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

    def chat_fn(message: str, history: list[list[str]]):
        msg = message.strip()

        # ── 基础命令 ──
        if msg == "/help":
            yield HELP_TEXT; return

        if msg == "/model":
            tu = agent.token_usage
            rag = f"{agent.paper_store.paper_count} 篇论文" if agent.paper_store else "未启用"
            yield f"**模型**: {agent.model}\n\n**Tokens**: {tu['total']:,} ({tu['calls']} 次调用)\n\n**RAG**: {rag}"
            return

        if msg == "/tokens":
            tu = agent.token_usage
            accum = tu.get("cost", 0)
            last = tu.get("last_round_cost", 0)
            cached = tu.get("cached", 0)
            lines = [
                f"**Token 消耗统计**\n", f"| 指标 | 数值 |\n|------|------|",
                f"| API 调用 | {tu['calls']} 次 |", f"| 输入 tokens | {tu['prompt']:,} |",
            ]
            if cached:
                lines.append(f"| 缓存命中 | {cached:,} (¥0.02/1M) |")
            lines.extend([
                f"| 输出 tokens | {tu['completion']:,} |", f"| 合计 | {tu['total']:,} |",
                f"| 本轮费用 | ¥{last:.4f} |", f"| 累计费用 | ¥{accum:.4f} |",
            ])
            yield "\n".join(lines); return

        if msg == "/indexed":
            if not agent.paper_store:
                yield "RAG 未启用。"; return
            papers = agent.paper_store.list_papers()
            if not papers:
                yield "📭 尚未索引论文。"; return
            lines = [f"**已索引论文** ({len(papers)} 篇)\n"]
            for i, p in enumerate(papers, 1):
                lines.append(f"{i}. {p['title']} ({p['chunks']} chunks)")
            yield "\n".join(lines); return

        if msg in ("/new", "/reset"):
            agent.reset()
            yield "✅ 已开始新对话。"; return

        if msg == "/profile":
            yield agent.profile.read(); return

        if msg.startswith("/note"):
            yield _handle_note_cmd(msg); return

        # 正常对话（流式）
        ctx = _build_context()
        import queue, threading
        q = queue.Queue()
        final = []
        cancelled = [False]

        def _run():
            r = agent.step(msg, context=ctx if ctx else None,
                           on_token=lambda t: q.put(t))
            final.append(r)
            q.put(None)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        partial = ""
        try:
            while True:
                try:
                    token = q.get(timeout=0.1)
                except queue.Empty:
                    yield partial  # keep-alive, no change
                    continue
                if token is None:
                    break
                partial += token
                yield partial
        except GeneratorExit:
            cancelled[0] = True
            q.put(None)  # unblock thread
            return
        yield re.sub(r'(?<!\$)\$([^\$]*[\\_^][^\$]*)\$(?!\$)', r'$$\1$$', final[0])

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
        limit = tu.get("context_limit", 131072)
        limit_k = f"{limit//1000000}M" if limit >= 1000000 else f"{limit//1000}K"
        pct = min(round(last / limit * 100), 99) if last else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        return (
            f"**模型**: {agent.model} | "
            f"**上下文**: `{bar}` {pct}% ({last:,}/{limit_k}) | "
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

    # ═══ 论文卡片 ───

    def paper_cards_html():
        if not agent.paper_store:
            return "<p style='color:#999;padding:1rem;'>RAG 未启用</p>"
        papers = agent.paper_store.list_papers()
        if not papers:
            return "<p style='color:#999;padding:1rem;'>暂无索引论文。在对话中输入 read_pdf 添加。</p>"
        html = '<div style="display:grid;gap:10px;padding:0.5rem;">'
        for p in papers:
            html += (
                f'<div style="border:1px solid #e0e0e0;border-radius:8px;padding:12px;background:#fafafa;">'
                f'<strong style="font-size:0.95rem;">{p["title"][:80]}</strong>'
                f'<div style="color:#888;font-size:0.8rem;margin:4px 0;">'
                f'{p["chunks"]} chunks | {p.get("indexed_at", "")[:16]}'
                f'</div></div>'
            )
        html += '</div>'
        return html

    # ═══ 设置函数 ───

    def load_config():
        import yaml
        try:
            with open("config.yaml", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:
            return {}

    def save_config(model, rag_enabled, max_pages):
        import yaml
        cfg = load_config()
        cfg["model"] = model
        cfg["rag"]["enabled"] = rag_enabled
        cfg["pdf"]["max_pages"] = int(max_pages)
        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        return "✅ 已保存。重启生效。"

    # ═══ 构建界面 ═══

    with gr.Blocks(title="🔬 Research Assistant") as demo:

        gr.HTML(
            '<div class="main-header">'
            '<h1>🔬 Research Assistant</h1>'
            '</div>'
        )

        status = gr.Markdown(refresh_status([]), elem_classes=["status-bar"])

        with gr.Tabs():
            # ── Tab 1: 对话 ──
            with gr.Tab("💬 对话"):
                chat = gr.ChatInterface(
                    fn=chat_fn,
                    chatbot=gr.Chatbot(height=420, render_markdown=True),
                    textbox=gr.Textbox(
                        placeholder="输入问题或命令... /help 查看帮助",
                        container=False, scale=7,
                        submit_btn="发送", stop_btn="停止",
                    ),
                    title=None, description=None,
                    examples=[
                        "搜索 diffusion model 在网络调度中的最新论文",
                        "对比已读论文中的损失函数设计",
                    ],
                    cache_examples=False,
                )
                chat.chatbot.change(
                    fn=refresh_status, inputs=[chat.chatbot], outputs=[status],
                )

            # ── Tab 2: 论文库 ──
            with gr.Tab("📚 论文库"):
                gr.Markdown("### 已索引论文")
                paper_list = gr.HTML(value=paper_cards_html())
                with gr.Row():
                    refresh_papers_btn = gr.Button("🔄 刷新", scale=1)
                refresh_papers_btn.click(
                    fn=lambda: paper_cards_html(), outputs=[paper_list],
                )

            # ── Tab 3: 笔记 ──
            with gr.Tab("📝 笔记"):
                with gr.Row():
                    topic_dd = gr.Dropdown(
                        label="话题", scale=2, allow_custom_value=True,
                        choices=[t["name"] for t in notes.list_topics()],
                        value=current_topic["name"],
                    )
                    note_input = gr.Textbox(
                        label="新笔记", placeholder="输入笔记内容...",
                        lines=2, scale=5,
                    )
                    note_save_btn = gr.Button("保存", scale=1, variant="primary")
                note_display = gr.HTML(value=note_list_html(current_topic["name"]))
                note_save_btn.click(
                    fn=note_save, inputs=[topic_dd, note_input],
                    outputs=[note_input, note_display],
                )
                topic_dd.change(
                    fn=note_topic_change, inputs=[topic_dd],
                    outputs=[note_display],
                )
                demo.load(fn=note_topic_list, outputs=[topic_dd])

            # ── Tab 4: 设置 ──
            with gr.Tab("⚙ 设置"):
                cfg = load_config()
                model_dd = gr.Dropdown(
                    label="模型", value=cfg.get("model", agent.model),
                    choices=["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
                    allow_custom_value=True,
                )
                rag_toggle = gr.Checkbox(
                    label="启用 RAG 论文库",
                    value=cfg.get("rag", {}).get("enabled", True),
                )
                max_pages_slider = gr.Slider(
                    label="PDF 最大页数", minimum=5, maximum=100, step=5,
                    value=cfg.get("pdf", {}).get("max_pages", 15),
                )
                save_cfg_btn = gr.Button("💾 保存配置", variant="primary")
                cfg_msg = gr.Markdown("")
                save_cfg_btn.click(
                    fn=save_config,
                    inputs=[model_dd, rag_toggle, max_pages_slider],
                    outputs=[cfg_msg],
                )

    demo.launch(
        server_port=args.port,
        share=False, show_error=True,
        theme=gr.themes.Soft(),
        css="""
        .main-header { text-align: center; padding: 0.5rem 0; }
        .main-header h1 { font-size: 1.3rem; font-weight: 600; }
        .status-bar { padding: 0.3rem 1rem; font-size: 0.75rem; color: #666; }
        footer { display: none !important; }
        """,
    )


if __name__ == "__main__":
    build_ui()

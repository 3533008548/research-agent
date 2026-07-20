"""
🌐 科研助手 Web UI — 极简聊天界面

基于 Gradio ChatInterface，支持：
  - 斜杠命令: /model /tokens /indexed /new /help
  - ChromaDB RAG 论文检索
  - 公式实时 MathJax 渲染
  - 状态栏实时更新

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

    # ── 命令帮助 ──
    HELP_TEXT = """
**浏览器端命令**（在输入框直接输入）：
- `/model` — 显示当前模型和状态
- `/tokens` — 显示 Token 消耗和费用
- `/indexed` — 列出已索引的论文
- `/new` — 开始新对话
- `/help` — 显示此帮助
    """.strip()

    # ── 聊天函数 ──
    def chat_fn(message: str, history: list[list[str]]) -> str:
        msg = message.strip()

        # ── 命令拦截 ──
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
            price = {
                "deepseek-chat": (0.27, 1.10),
                "deepseek-reasoner": (0.55, 2.19),
            }
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
                return "📭 尚未索引论文。read_pdf 会自动索引。"
            lines = [f"**已索引论文** ({len(papers)} 篇)\n"]
            for i, p in enumerate(papers, 1):
                lines.append(f"{i}. {p['title']} ({p['chunks']} chunks)")
            return "\n".join(lines)

        if msg in ("/new", "/reset"):
            agent.reset()
            return "✅ 已开始新对话。"

        # ── 正常对话 ──
        reply = agent.step(msg)
        # 行内公式 → 块公式（只转换含 LaTeX 命令、上下标的，单字母如 $g$ 不动）
        reply = re.sub(
            r'(?<!\$)\$([^\$]*[\\_^][^\$]*)\$(?!\$)',
            r'$$\1$$',
            reply,
        )
        return reply

    # ── 状态栏刷新函数 ──
    def refresh_status(history):
        tu = agent.token_usage
        rag = (
            f"{agent.paper_store.paper_count} 篇"
            if agent.paper_store else "关"
        )
        return (
            f"**模型**: {agent.model} | "
            f"**Tokens**: {tu['total']:,} | "
            f"**RAG**: {rag}"
        )

    # ── 构建界面 ──
    with gr.Blocks(title="🔬 Research Assistant") as demo:

        gr.HTML(
            '<div class="main-header">'
            '<h1>🔬 Research Assistant</h1>'
            '<p style="color:#888;font-size:0.9rem;">'
            '搜索论文 · 阅读 PDF · RAG 检索 · 分析方向 · 输入 /help 查看命令'
            '</p></div>'
        )

        status = gr.Markdown(refresh_status([]), elem_classes=["status-bar"])

        chat = gr.ChatInterface(
            fn=chat_fn,
            chatbot=gr.Chatbot(height=520, render_markdown=True),
            textbox=gr.Textbox(
                placeholder="输入研究方向或问题... /help 查看命令",
                container=False,
                scale=7,
            ),
            title=None,
            description=None,
            examples=[
                "搜索 diffusion model 在网络调度中的最新论文",
                "对比已读论文中的损失函数设计",
                "帮我分析这个研究方向的技术趋势",
            ],
            cache_examples=False,
        )

        # 每次对话后刷新状态栏
        chat.chatbot.change(
            fn=refresh_status,
            inputs=[chat.chatbot],
            outputs=[status],
        )

    demo.launch(
        server_port=args.port,
        share=False,
        show_error=True,
        theme=gr.themes.Soft(),
        css="""
        .main-header { text-align: center; padding: 1rem 0; }
        .main-header h1 { font-size: 1.5rem; font-weight: 600; }
        .status-bar { padding: 0.5rem 1rem; font-size: 0.8rem; color: #666; }
        footer { display: none !important; }
        """,
    )


if __name__ == "__main__":
    build_ui()

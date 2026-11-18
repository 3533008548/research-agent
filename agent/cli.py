"""Interactive research assistant CLI."""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from agent.service import ResearchAgent
from integrations.public_search import list_downloaded_papers


def print_banner():
    print(r"""
╔══════════════════════════════════════════════════════╗
║     🔬  Research Assistant  科研助手 Agent           ║
║                                                      ║
║  技术: LangGraph + DeepSeek + RAG + pdfplumber       ║
║  功能: 搜索论文 · 阅读PDF · RAG检索 · 分析方向       ║
║                                                      ║
║  输入研究方向开始探索！                                ║
║  /help 查看命令  |  /quit 退出                       ║
╚══════════════════════════════════════════════════════╝
""")


def print_help():
    msg = """
📖 **命令列表**

  /help            显示此帮助
  /new             开始新对话（旧对话保留在运行时数据目录）
  /papers          列出已下载论文
  /indexed         列出已索引论文（RAG库）
  /model           显示当前模型和 RAG 状态
  /model-set <name> 切换模型（重新构建 Agent）
  /tokens          显示 Token 消耗统计
  /quit            退出

💡 **典型工作流**

  >>> "帮我搜索扩散模型在TSN调度的论文"
  >>> "精读第1篇"                      ← 自动下载+索引
  >>> "这篇的损失函数是什么？"         ← RAG 精准检索
  >>> "这3篇的共同趋势是什么？"        ← 跨论文分析

📦 **数据存储**
  runtime/primary/papers/  PDF 缓存
  runtime/derived/chroma/  RAG 向量库（可重建）
  runtime/primary/db/      对话、记忆和每日检索数据
"""
    print(msg)


def main():
    parser = argparse.ArgumentParser(description="科研助手 Agent")
    parser.add_argument("-m", "--model", default=None, help="模型名称")
    parser.add_argument("--no-rag", action="store_true", help="禁用 RAG")
    parser.add_argument("--debug", action="store_true", help="DEBUG 日志")
    parser.add_argument("--data-dir", default=None, help="运行时数据目录（默认: APP_DATA_DIR 或 runtime）")
    args = parser.parse_args()

    from infra.config import Config
    from infra.logger import setup_logging
    setup_logging(debug=args.debug)

    cli = {
        "model": args.model,
        "rag_enabled": False if args.no_rag else None,
        "ui_debug": True if args.debug else None,
        "data_dir": args.data_dir,
    }
    cfg = Config.load({k: v for k, v in cli.items() if v is not None})

    Path(cfg.papers_dir).mkdir(parents=True, exist_ok=True)

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("❌ 未设置 DEEPSEEK_API_KEY")
        print("   请在 .env 文件中添加 DEEPSEEK_API_KEY=sk-****")
        print("   或: $env:DEEPSEEK_API_KEY='sk-****'")
        sys.exit(1)

    print(f"🤖 使用模型: {cfg.model}", file=sys.stderr)

    try:
        agent = ResearchAgent(cfg=cfg)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print_banner()
    first_turn = True

    while True:
        try:
            if first_turn:
                user_input = input("\n💡 你想研究什么方向？\n  >>> ").strip()
                first_turn = False
            else:
                user_input = input("\n  >>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n👋 再见！")
            break

        if not user_input:
            continue

        # 命令
        if user_input.lower() in ("/quit", "/exit", "/q"):
            print("\n👋 再见！科研顺利！")
            break

        if user_input.lower() == "/help":
            print_help()
            continue

        if user_input.lower() in ("/new", "/reset"):
            agent.reset()
            first_turn = True
            continue

        if user_input.lower() == "/papers":
            print(f"\n{list_downloaded_papers()}")
            continue

        if user_input.lower() == "/indexed":
            if agent.paper_store:
                papers = agent.paper_store.list_papers()
                if not papers:
                    print("\n📭 尚未索引论文。read_pdf 会自动索引。")
                else:
                    print(
                        f"\n📚 已索引论文（{len(papers)} 篇, "
                        f"{agent.paper_store.chunk_count} 块）\n"
                    )
                    for i, p in enumerate(papers, 1):
                        print(f"  {i}. {p['title']} ({p['chunks']} chunks)")
            else:
                print("\n❌ RAG 未启用。")
            continue

        if user_input.lower() == "/model":
            rag_status = (
                f"已启用 ({agent.paper_store.paper_count} 篇论文)"
                if agent.paper_store
                else "未启用"
            )
            print(
                f"\n🤖 模型: {agent.model}"
                f"  |  Tokens: {agent.token_usage['total']:,}"
                f"  |  RAG: {rag_status}"
            )
            continue

        if user_input.lower().startswith("/model-set "):
            new_model = user_input[11:].strip()
            if not new_model:
                print("\n用法: /model-set <模型名>，如 /model-set deepseek-chat")
                continue
            old_model = agent.model
            try:
                # 保留当前配置（密钥、RAG、数据目录等），仅替换模型。
                agent = ResearchAgent(cfg=replace(agent.cfg, model=new_model))
                print(f"\n✅ 已切换模型: {old_model} → {new_model}")
            except Exception as e:
                print(f"\n❌ 切换失败: {e}")
            continue

        if user_input.lower() == "/tokens":
            tu = agent.token_usage
            price = {
                "deepseek-chat": (0.27, 1.10),        # $/百万 token (输入, 输出)
                "deepseek-reasoner": (0.55, 2.19),
            }
            p = price.get(agent.model, (0.27, 1.10))
            cost = (tu["prompt"] / 1e6 * p[0]) + (tu["completion"] / 1e6 * p[1])
            print(
                f"\n📊 Token 消耗统计\n"
                f"   API 调用: {tu['calls']} 次\n"
                f"   输入 tokens: {tu['prompt']:,}\n"
                f"   输出 tokens: {tu['completion']:,}\n"
                f"   合计 tokens: {tu['total']:,} / 64K 上限\n"
                f"   估算费用: ${cost:.4f}"
            )
            continue

        # 对话
        try:
            agent.chat(user_input)
        except KeyboardInterrupt:
            print("\n\n👋 再见！")
            break
        except Exception as e:
            print(f"\n❌ 错误: {type(e).__name__}: {e}")
            print("   输入 /new 重新开始")

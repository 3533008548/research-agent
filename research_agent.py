"""
╔══════════════════════════════════════════════════════════════════╗
║          🔬 科研助手 Agent — Research Assistant                  ║
║                                                                  ║
║  技术栈: LangGraph + DeepSeek API + ChromaDB RAG + pdfplumber    ║
║  功能:   论文搜索 → PDF 阅读 → RAG检索 → 方向分析 → 科研建议    ║
║                                                                  ║
║  用法:   python research_agent.py                                ║
║  依赖:   pip install -r requirements.txt                        ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys

# ── Windows GBK 终端兼容：强制 stderr 使用 UTF-8 ──
if sys.platform == "win32":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import argparse
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests

# ── 环境变量 ──
try:
    from dotenv import load_dotenv

    for env_path in [
        ".env",
        "../cli-chatbot/.env",
        str(Path.home() / ".env"),
    ]:
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
except ImportError:
    pass

# ── 项目模块 ──
from graph_builder import build_graph
from tools import list_downloaded_papers


# ═══════════════════════════════════════════════════════════════
#  ResearchAgent — LangGraph 包装器
# ═══════════════════════════════════════════════════════════════

class ResearchAgent:
    """
    科研助手 Agent — 包装 LangGraph 编译图。

    使用方式:
      agent = ResearchAgent(api_key="sk-xx")
      agent.chat("帮我搜索扩散模型论文")
      result = agent.step("精读第1篇")
      agent.reset()  # 新会话
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        enable_rag: bool = True,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError(
                "❌ 未找到 DEEPSEEK_API_KEY！\n"
                "   请在 .env 文件中设置 DEEPSEEK_API_KEY=sk-****\n"
                "   或通过环境变量 DEEPSEEK_API_KEY 传入"
            )

        self.model = model
        self.enable_rag = enable_rag
        self._paper_store = None

        # Token 统计（可变 dict，由 graph 闭包更新）
        self.token_usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}

        # 初始化 RAG 论文库
        if enable_rag:
            try:
                from paper_store import PaperStore

                print("      📚 初始化论文向量库...", file=sys.stderr, flush=True)
                self._paper_store = PaperStore(persist_dir="./chroma_data")
                print(
                    f"      ✅ 已加载 {self._paper_store.paper_count} 篇论文, "
                    f"{self._paper_store.chunk_count} 个块",
                    file=sys.stderr,
                )
            except ImportError as e:
                print(
                    f"      ⚠️ RAG 未启用（依赖缺失: {e}）",
                    file=sys.stderr,
                )

        # 构建 LangGraph（SQLite 持久化对话历史）
        self._app = build_graph(
            api_key=self.api_key,
            model=self.model,
            paper_store=self._paper_store,
            token_usage=self.token_usage,
            checkpoint_db="checkpoint.db",
        )

        # 会话配置：固定 thread_id 实现跨重启恢复
        self._thread_id = "research-main"
        self._config = {"configurable": {"thread_id": self._thread_id}}

        # 检查是否有历史对话
        if Path("checkpoint.db").exists():
            size = Path("checkpoint.db").stat().st_size
            print(f"      💾 对话历史: checkpoint.db ({size / 1024:.0f} KB)", file=sys.stderr)

    # ── 公开 API ──

    def step(self, user_input: str) -> str:
        """单轮推理：输入用户消息，返回 Agent 回复文本"""
        before = self.token_usage["total"]
        state = {"messages": [{"role": "user", "content": user_input}]}

        try:
            result = self._app.invoke(state, config=self._config)
            messages = result.get("messages", [])
            if not messages:
                return "⚠️ Agent 未返回任何消息。"

            last = messages[-1]
            return last.get("content", "") or ""

        except requests.RequestException as e:
            return f"❌ 网络请求失败: {e}\n   请检查网络和 API Key。"
        except Exception as e:
            return f"❌ Agent 错误: {type(e).__name__}: {e}"

    def chat(self, user_input: str):
        """打印格式化回复"""
        before = self.token_usage["total"]
        print(f"\n{'=' * 60}")
        print(f"  🔬 科研助手 | {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'=' * 60}")
        reply = self.step(user_input)
        delta = self.token_usage["total"] - before
        print(f"\n{reply}")
        if delta > 0:
            print(
                f"\n  📊 本轮: +{delta} tokens | "
                f"累计: {self.token_usage['total']} tokens "
                f"({self.token_usage['calls']} 次调用)"
            )

    def reset(self):
        """重置会话（新 thread_id，旧对话永久保留在 checkpoint.db 中）"""
        self._thread_id = f"session-{uuid.uuid4().hex[:8]}"
        self._config = {"configurable": {"thread_id": self._thread_id}}
        print(f"\n🔄 已开始新对话 (thread: {self._thread_id})。", file=sys.stderr)

    @property
    def paper_store(self):
        """PaperStore 实例（可能为 None）"""
        return self._paper_store

    # ── 内部 ──

    @property
    def thread_id(self) -> str:
        return self._thread_id


# ═══════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════

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
  /new             开始新对话（旧对话保留在 checkpoint.db）
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
  data/papers/     PDF 缓存
  chroma_data/     RAG 向量库（持久化）
  checkpoint.db    对话历史（SQLite，重启不丢）
"""
    print(msg)


def main():
    parser = argparse.ArgumentParser(
        description=" 科研助手 Agent — LangGraph + DeepSeek + RAG"
    )
    parser.add_argument(
        "-m", "--model",
        default=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        help="模型名称 (默认: deepseek-chat, 也支持 deepseek-v4-pro、flash 等)",
    )
    parser.add_argument(
        "--no-rag",
        action="store_true",
        help="禁用 RAG 论文向量库",
    )
    args = parser.parse_args()

    model = args.model
    enable_rag = not args.no_rag

    # 确保数据目录
    Path("data/papers").mkdir(parents=True, exist_ok=True)

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("❌ 未设置 DEEPSEEK_API_KEY")
        print("   请在 .env 文件中添加 DEEPSEEK_API_KEY=sk-****")
        print("   或: $env:DEEPSEEK_API_KEY='sk-****'")
        sys.exit(1)

    print(f"🤖 使用模型: {model}", file=sys.stderr)

    try:
        agent = ResearchAgent(model=model, enable_rag=enable_rag)
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
                agent = ResearchAgent(
                    model=new_model,
                    api_key=agent.api_key,
                    enable_rag=agent.paper_store is not None,
                )
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


if __name__ == "__main__":
    main()

"""
🧠 LangGraph Agent 图结构 — 科研助手推理引擎

图结构（ReAct 循环）:
  START → [llm] ── tool_calls? ──→ [tools] ──→ [llm]（循环）
               ── no             ──→ END

特性:
  - MemorySaver 持久化对话历史（基于线程 thread_id）
  - 工具函数内聚：搜索 (tools.py) + PDF (pdf_reader.py) + RAG (paper_store.py)
  - PDF 阅读后自动索引到 ChromaDB
"""

import json
import sys
from pathlib import Path
from typing import Annotated, TypedDict

import requests

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3


# ═══════════════════════════════════════════════════════════════
#  State 定义
# ═══════════════════════════════════════════════════════════════

class AgentState(TypedDict):
    """
    LangGraph 共享状态。

    messages:  对话历史（Annotated[list, add] 自动追加）
    metadata:  元数据字典（预留，如论文阅读进度）
    """
    messages: Annotated[list[dict], lambda a, b: a + b]  # operator.add
    metadata: dict


# ═══════════════════════════════════════════════════════════════
#  系统提示词
# ═══════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = (
    "你是一位顶级的科研助手（Research Assistant AI），专精于帮助研究人员进行文献调研和方向分析。\n\n"
    "## ⚠️ 信息获取优先级（必须严格遵守）\n"
    "你拥有一座**本地论文库**（通过 query_papers 访问），存储所有已读论文的全文索引。\n"
    "1. **本地优先** — 收到问题后，先想一想本地论文库里有没有相关内容。如有，用 query_papers 检索\n"
    "2. **外部补充** — 仅当本地内容不足以回答时，才用 search_papers 搜索新论文\n"
    "3. **精读后检索** — read_pdf 后论文已自动索引到本地库，追问细节时用 query_papers 精准定位，不要重新阅读\n"
    "4. **明确告知来源** — 回答时主动说明信息来自「你已读的论文」还是「新搜索的结果」\n\n"
    "## 核心能力\n"
    "1. **本地检索** — query_papers 在你已索引的论文库中精准定位方法细节、实验数据、公式\n"
    "2. **论文搜索** — search_papers 从 Semantic Scholar / arXiv 搜索新论文\n"
    "3. **PDF阅读** — read_pdf 下载并阅读论文全文（自动索引到本地库）\n"
    "4. **方向分析** — 综合多篇论文，总结技术趋势、对比方法优劣、提出见解\n"
    "5. **科研建议** — 基于文献调研给出研究选题、实验设计等建议\n\n"
    "## 工作方法\n"
    "- **先查后搜**：任何问题都先 query_papers → 不够再 search_papers\n"
    "- **搜索阶段**：search_papers 搜索新论文，返回结果后询问用户想精读哪篇\n"
    "- **精读阶段**：read_pdf 下载并提取论文内容，自动索引后追问直接用 query_papers\n"
    "- **分析阶段**：综合多篇论文做技术对比、趋势分析\n\n"
    "## 行为准则\n"
    "- 用**中文**回答，论文标题和专有术语保留英文\n"
    "- 解读论文时覆盖：解决了什么问题 → 方法核心思想 → 实验设置与结果 → 局限性\n"
    "- 分析研究方向时给出清晰的对比表和渐进式研究路线\n"
    "- 每次回复末尾主动建议下一步动作\n\n"
    "开始吧！请用户告诉你想研究什么方向。"
)


# ═══════════════════════════════════════════════════════════════
#  工具 Schema 定义
# ═══════════════════════════════════════════════════════════════

def _build_tool_schemas() -> list[dict]:
    """返回 Function Calling 工具定义列表"""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_papers",
                "description": (
                    "搜索学术论文。支持两个数据源：semantic_scholar（推荐，含引用数和PDF链接）"
                    "和 arxiv（覆盖面更全）。默认使用 semantic_scholar。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词，如 'diffusion model time-sensitive networking'",
                        },
                        "source": {
                            "type": "string",
                            "enum": ["semantic_scholar", "arxiv"],
                            "description": "数据源，默认 semantic_scholar",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回结果数（1-10），默认 5",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_pdf",
                "description": (
                    "下载论文PDF并提取文本内容。支持 arXiv 链接、PDF直链和本地路径。"
                    "读取完成后会自动索引到本地论文库，后续可用 query_papers 精准检索。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url_or_path": {
                            "type": "string",
                            "description": "PDF 的 URL 或本地文件路径",
                        },
                        "max_pages": {
                            "type": "integer",
                            "description": "最多读取的页数，默认 15",
                        },
                    },
                    "required": ["url_or_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_papers",
                "description": (
                    "在所有已索引的论文中语义检索特定内容。适合精读后查询方法细节、"
                    "损失函数、实验数据等。返回最相关的论文段落。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "查询内容，如 '损失函数设计' 或 '用的什么数据集'",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回最相关的段落数，默认 3",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_papers",
                "description": "列出本地已下载缓存的论文PDF文件",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_indexed_papers",
                "description": "查看所有已索引的论文（RAG 向量库中的论文列表）",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_paper",
                "description": (
                    "从 RAG 论文库中删除一篇已索引的论文。"
                    "当你发现论文被重复索引或用户明确要求删除时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id_or_title": {
                            "type": "string",
                            "description": "论文的标题或 paper_id（可用 list_indexed_papers 查看）",
                        },
                    },
                    "required": ["paper_id_or_title"],
                },
            },
        },
    ]


# ═══════════════════════════════════════════════════════════════
#  图构建函数
# ═══════════════════════════════════════════════════════════════

def build_graph(
    api_key: str,
    api_url: str = "https://api.deepseek.com/chat/completions",
    model: str = "deepseek-chat",
    paper_store = None,  # Optional[PaperStore]
    token_usage: dict = None,  # 外部传入的可变 dict，用于累计 token 消耗
    checkpoint_db: str = "checkpoint.db",  # SQLite 持久化路径
) -> callable:
    """
    构建并编译 LangGraph ReAct Agent。

    参数:
      api_key:      DeepSeek API Key
      api_url:      API 端点
      model:        模型名称
      paper_store:  PaperStore 实例（若传入则启用 RAG 功能）

    返回:
      编译后的 LangGraph app (CompiledGraph)
    """
    tool_schemas = _build_tool_schemas()

    # ═══ LLM 节点 ═══

    def llm_node(state: AgentState) -> dict:
        """调用 DeepSeek API 进行推理"""
        messages = list(state.get("messages", []))

        # 若无 system prompt 则插入
        if messages and messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": _SYSTEM_PROMPT})

        payload = {
            "model": model,
            "messages": messages,
            "tools": tool_schemas,
            "tool_choice": "auto",
            "stream": False,
            "temperature": 0.7,
        }

        resp = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )

        if resp.status_code == 429:
            return {"messages": [{"role": "assistant", "content": "⚠️ API 限流，请稍后再试"}]}
        resp.raise_for_status()

        data = resp.json()
        msg = data["choices"][0]["message"]

        # ── Token 统计 ──
        if token_usage is not None and "usage" in data:
            usage = data["usage"]
            prompt_tk = usage.get("prompt_tokens", 0)
            compl_tk = usage.get("completion_tokens", 0)
            total_tk = usage.get("total_tokens", prompt_tk + compl_tk)
            token_usage["prompt"] += prompt_tk
            token_usage["completion"] += compl_tk
            token_usage["total"] += total_tk
            token_usage["calls"] += 1
            print(
                f"      📊 tokens: +{total_tk} "
                f"(P:{prompt_tk} C:{compl_tk}) "
                f"累计: {token_usage['total']}",
                file=sys.stderr,
            )

        assistant_msg = {
            "role": "assistant",
            "content": msg.get("content") or None,
        }

        if msg.get("tool_calls"):
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in msg["tool_calls"]
            ]

        return {"messages": [assistant_msg]}

    # ═══ 路由 ═══

    def router(state: AgentState) -> str:
        """条件路由：最后一条消息有 tool_calls → tools，否则 → END"""
        messages = state.get("messages", [])
        if not messages:
            return END
        last = messages[-1]
        if last.get("tool_calls"):
            return "tools"
        return END

    # ═══ 工具执行节点 ═══

    def tool_node(state: AgentState) -> dict:
        """执行工具调用，返回 tool result 消息列表"""
        messages = state.get("messages", [])
        tool_calls = messages[-1].get("tool_calls", [])

        tool_msgs = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            print(
                f"      🔧 {name}({json.dumps(args, ensure_ascii=False)})",
                file=sys.stderr,
            )

            result = _execute_tool(name, args, paper_store)

            # Token 截断
            if isinstance(result, str) and len(result) > 12000:
                result = result[:12000] + "\n\n...（截断至 12000 字符）"

            tool_msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        return {"messages": tool_msgs}

    # ═══ 构图 ═══

    graph = StateGraph(AgentState)
    graph.add_node("llm", llm_node)
    graph.add_node("tools", tool_node)

    graph.set_entry_point("llm")
    graph.add_conditional_edges("llm", router, {"tools": "tools", END: END})
    graph.add_edge("tools", "llm")

    conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))


# ═══════════════════════════════════════════════════════════════
#  工具执行逻辑
# ═══════════════════════════════════════════════════════════════

def _execute_tool(name: str, args: dict, paper_store=None) -> str:
    """
    执行具体工具调用。

    所有工具实现集中在此处，按需导入：
      - tools.py          搜索 + 列表
      - pdf_reader.py     增强型 PDF 提取
      - paper_store.py     RAG 检索（通过 paper_store 实例）
    """
    from tools import search_arxiv, search_semantic_scholar, list_downloaded_papers
    from pdf_reader import read_pdf_enhanced

    # ── search_papers ──
    if name == "search_papers":
        query = args.get("query", "")
        source = args.get("source", "semantic_scholar")
        limit = min(args.get("limit", 5), 10)
        if source == "arxiv":
            return search_arxiv(query, max_results=limit)
        return search_semantic_scholar(query, limit=limit)

    # ── read_pdf (下载 + 提取 + 自动索引) ──
    if name == "read_pdf":
        url_or_path = args.get("url_or_path", "")
        max_pages = args.get("max_pages", 15)

        pdf_dir = Path("data/papers")
        pdf_dir.mkdir(parents=True, exist_ok=True)

        # ── URL 解析 & 下载 ──
        if url_or_path.startswith(("http://", "https://")):
            filename = url_or_path.split("/")[-1].split("?")[0]
            if not filename.endswith(".pdf"):
                if "arxiv.org/abs/" in url_or_path:
                    arxiv_id = url_or_path.split("/abs/")[-1].split("v")[0]
                    url_or_path = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                    filename = f"{arxiv_id}.pdf"
                elif "arxiv.org/pdf/" in url_or_path:
                    filename = url_or_path.split("/pdf/")[-1].split("?")[0]
                    if not filename.endswith(".pdf"):
                        filename += ".pdf"
                else:
                    filename = f"paper_{hash(url_or_path) & 0xFFFFFFFF:08x}.pdf"

            pdf_path = pdf_dir / filename
            if not pdf_path.exists():
                print(f"      📥 正在下载 PDF...", file=sys.stderr)
                try:
                    resp = requests.get(
                        url_or_path,
                        timeout=60,
                        headers={"User-Agent": "Mozilla/5.0 (ResearchAssistant/1.0)"},
                    )
                    resp.raise_for_status()
                    pdf_path.write_bytes(resp.content)
                    print(
                        f"      ✅ 下载完成 ({len(resp.content) / 1024:.0f} KB)",
                        file=sys.stderr,
                    )
                except requests.RequestException as e:
                    return f"❌ PDF 下载失败: {e}"
            else:
                print(f"      📂 使用缓存: {pdf_path.name}", file=sys.stderr)
        else:
            pdf_path = Path(url_or_path)
            if not pdf_path.exists():
                # fallback: 尝试 data/papers/ 目录（匹配 list_papers 输出）
                alt = pdf_dir / pdf_path.name
                if alt.exists():
                    pdf_path = alt
                else:
                    return f"❌ 本地文件不存在: {url_or_path}\n   已尝试: data/papers/{pdf_path.name}"

        # ── 增强型文本提取 ──
        try:
            result = read_pdf_enhanced(str(pdf_path), max_pages=max_pages, max_chars=50000)
        except ImportError as e:
            return f"❌ {e}"
        except Exception as e:
            return f"❌ PDF 解析失败: {type(e).__name__}: {e}"

        # ── 自动索引到 RAG ──
        if paper_store and result and not result.startswith("❌"):
            try:
                title = pdf_path.stem.replace("_", " ")
                # 去重：先删除同名论文的旧索引
                for p in paper_store.list_papers():
                    if p["title"] == title:
                        paper_store.delete_paper(p["paper_id"])
                        print(f"      🗑️ 已删除旧索引: {title}", file=sys.stderr)
                        break
                paper_store.index_paper(result, title=title)
                print(f"      📚 已索引到论文库", file=sys.stderr)
                result += (
                    "\n\n（📚 已自动索引。后续可用 query_papers 精准检索论文细节。）"
                )
            except Exception as e:
                print(f"      ⚠️ RAG 索引失败: {e}", file=sys.stderr)

        return result

    # ── query_papers (RAG 检索) ──
    if name == "query_papers":
        if not paper_store:
            return "❌ RAG 功能未启用。请确保 PaperStore 已初始化。"
        query = args.get("query", "")
        top_k = min(args.get("top_k", 3), 5)
        results = paper_store.query(query, top_k=top_k)
        if not results:
            return "📭 未找到相关内容。请先阅读并索引论文（read_pdf 会自动索引）。"

        lines = [f"📚 检索结果 — 「{query}」（共 {len(results)} 条）\n"]
        for i, r in enumerate(results, 1):
            lines.append(
                f"  {i}. [{r['title']}]  相似度距离: {r['distance']}\n"
                f"     {r['text']}"
            )
        return "\n".join(lines)

    # ── list_papers ──
    if name == "list_papers":
        return list_downloaded_papers()

    # ── list_indexed_papers ──
    if name == "list_indexed_papers":
        if not paper_store:
            return "❌ RAG 功能未启用。"
        papers = paper_store.list_papers()
        if not papers:
            return "📭 尚未索引论文。read_pdf 会自动索引。"
        lines = [f"📚 已索引论文（{len(papers)} 篇, {paper_store.chunk_count} 个块）\n"]
        for i, p in enumerate(papers, 1):
            lines.append(f"  {i}. {p['title']} ({p['chunks']} chunks)")
        return "\n".join(lines)

    # ── delete_paper ──
    if name == "delete_paper":
        if not paper_store:
            return "❌ RAG 功能未启用。"
        pid_or_title = args.get("paper_id_or_title", "")
        if not pid_or_title:
            return "❌ 请指定要删除的论文标题或 paper_id。"
        papers = paper_store.list_papers()
        found = None
        for p in papers:
            if pid_or_title in p["paper_id"] or pid_or_title.lower() in p["title"].lower():
                found = p
                break
        if not found:
            return f"❌ 未找到论文: {pid_or_title}\n   可用 /indexed 或 list_indexed_papers 查看已索引论文。"
        count = paper_store.delete_paper(found["paper_id"])
        return f"🗑️ 已删除: {found['title']} ({count} 个块)"

    # ── 未知工具 ──
    return f"❌ 未知工具: {name}"

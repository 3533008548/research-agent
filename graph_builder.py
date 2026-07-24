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
    "## 🧠 思考框架（四阶段）\n\n"
    "### 阶段1 — 评估\n"
    "收到问题后，先判断：用户问什么？本地论文库有没有相关内容？\n"
    "→ 有 → 用 query_papers 检索\n"
    "→ 无 → 用 search_papers 搜索\n"
    "→ 不确定 → 先 query_papers 确认无，再 search_papers\n\n"
    "### 阶段2 — 执行\n"
    "调用工具获取信息。追踪每条信息的来源：\n"
    "[已读] = 来自本地论文库  [搜索] = 来自网络搜索  [推测] = 基于领域知识\n\n"
    "### 阶段3 — 合成\n"
    "综合信息给出回答。严格要求：\n"
    "- 公式必须标注来源：[已读·论文标题] 或 [推测]\n"
    "- 实验数据标注论文和图表编号\n"
    "- 不确定的结论前加「推测」标记\n\n"
    "### 阶段4 — 自检\n"
    "回答前自查：公式引用是否具体？来源是否区分清楚？不确定的内容是否标注？\n\n"
    "## ⚠️ 信息获取优先级\n"
    "1. **本地优先** — 先用 query_papers 检索已索引论文\n"
    "2. **外部补充** — 本地不足时才 search_papers\n"
    "3. **精读后检索** — read_pdf 后追问细节用 query_papers\n"
    "4. **明确来源** — 回答中用 [已读][搜索][推测] 标记每条信息\n\n"
    "## 核心能力\n"
    "1. query_papers — 本地论文库精准检索（方法细节、公式、实验数据）\n"
    "2. search_papers — Semantic Scholar / arXiv 搜索新论文\n"
    "3. read_pdf — 下载并阅读论文（自动索引，读完后生成摘要卡片）\n"
    "4. describe_image — 用视觉模型理解论文中的架构图、流程图、实验图\n"
    "4. 方向分析 — 综合多篇论文，对比技术趋势\n"
    "5. 科研建议 — 基于文献调研给出选题建议\n\n"
    "## 工作方法\n"
    "- 先查后搜：任何问题先 query_papers → 不够再 search_papers\n"
    "- read_pdf 后必须生成**摘要卡片**（问题、方法、公式、实验、局限）\n"
    "- 公式用 $$...$$（块）或 [已读]标记来源\n"
    "- 每次回复末尾建议下一步\n\n"
    "## 输出范例\n\n"
    "### 范例1：精读论文后\n"
    "> 📋 **摘要卡片 — Intelligent Traffic Scheduling...**\n"
    "> 1. 核心问题: 提出基于DRL的TSN调度算法 [已读]\n"
    "> 2. 方法思路: DQN + 优先级队列 [已读]\n"
    "> 3. 关键公式: R = w₁·delay + w₂·throughput [已读·公式3]\n"
    "> 4. 实验结论: 延迟降低23% [已读·Table 2]\n"
    "> 5. 局限性: 仅仿真验证 [已读]\n\n"
    "### 范例2：回答论文细节\n"
    "> Q: 损失函数是什么？\n"
    "> A: 根据本地方案，该论文使用MSE作为调度损失 [已读·query_papers结果]\n"
    "> 公式(10) L = Σ(t_actual - t_target)² [已读·formula 10]\n"
    "> 对比文献[搜索]也常用Huber Loss，但该论文选择MSE因为...\n\n"
    "开始吧！"
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
                    "读取完成后会自动索引到本地论文库，同时提取图片供 describe_image 工具分析。"
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
                "name": "describe_image",
                "description": (
                    "用视觉模型描述论文中的图片（架构图、流程图、实验结果图等）。"
                    "当用户问到'图X是什么'或需要理解图表内容时使用。"
                    "传入图片文件路径（Marker 提取的图片在 data/papers/images/ 下）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": "图片文件的本地路径",
                        },
                    },
                    "required": ["image_path"],
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
        {
            "type": "function",
            "function": {
                "name": "update_profile",
                "description": (
                    "更新用户画像。当你发现用户的新研究方向、偏好、活跃问题时自动调用。"
                    "action: add_direction/add_question/add_paper/set_preference"
                    "content: 对应的内容文本"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add_direction", "add_question", "add_paper", "set_preference"],
                            "description": "操作类型",
                        },
                        "content": {
                            "type": "string",
                            "description": "内容。add_direction: 研究方向名; add_question: 问题; add_paper: 标题: 一句话总结; set_preference: 键: 值",
                        },
                    },
                    "required": ["action", "content"],
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
    glm_api_key: str = "",  # GLM-4V 视觉模型 API Key
    stream_callback = None,  # 可选：流式输出回调 fn(token: str)
    profile_manager = None,  # ProfileManager 实例
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

        # 清理不完整的 tool_calls——修复 checkpoint 中残留的孤儿 tool_call
        valid_ids = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        for i in range(len(messages)):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                cleaned = [tc for tc in m["tool_calls"] if tc.get("id") in valid_ids]
                if cleaned:
                    messages[i] = dict(m, tool_calls=cleaned)
                else:
                    messages[i] = dict(m)
                    del messages[i]["tool_calls"]

        if messages and messages[0].get("role") != "system":
            profile_text = ""
            if profile_manager:
                profile_text = profile_manager.summary()
            prompt = _SYSTEM_PROMPT
            if profile_text:
                prompt = f"[用户画像] {profile_text}\n\n{prompt}"
            messages.insert(0, {"role": "system", "content": prompt})

        # 从 metadata 注入 verify 反馈（用户不可见）
        verify_fb = state.get("metadata", {}).get("verify_feedback", "")
        if verify_fb:
            messages.append({"role": "system", "content": verify_fb})

        payload = {
            "model": model,
            "messages": messages,
            "tools": tool_schemas,
            "tool_choice": "auto",
            "stream": stream_callback is not None,
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
        if resp.status_code >= 400:
            body = resp.text[:500] if resp.text else "(empty)"
            print(f"      ❌ API {resp.status_code}: {body}", file=sys.stderr)
        resp.raise_for_status()

        # ── 流式模式 ──
        if stream_callback:
            import json as _json
            full_text = ""
            tool_calls_acc = {}
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = _json.loads(data_str)
                except Exception:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                token = delta.get("content", "")
                if token:
                    full_text += token
                    stream_callback(token)
                # 累积 tool_calls
                for tc in delta.get("tool_calls", []):
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": tc.get("id", ""), "function": {"name": "", "arguments": ""}}
                    if tc.get("id"):
                        tool_calls_acc[idx]["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        tool_calls_acc[idx]["function"]["name"] += tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        tool_calls_acc[idx]["function"]["arguments"] += tc["function"]["arguments"]
                # 最后一条 chunk 含 usage
                if token_usage and "usage" in chunk:
                    u = chunk["usage"]
                    token_usage["prompt"] += u.get("prompt_tokens", 0)
                    token_usage["completion"] += u.get("completion_tokens", 0)
                    token_usage["total"] += u.get("total_tokens", 0)
                    token_usage["calls"] += 1
                    token_usage["last_prompt"] = u.get("prompt_tokens", 0)
                    # 缓存检测 + 费用
                    cached = (u.get("prompt_tokens_details", {}).get("cached_tokens")
                              or u.get("prompt_cache_hit_tokens", 0))
                    token_usage["cached"] = token_usage.get("cached", 0) + cached
                    missed = u.get("prompt_tokens", 0) - cached
                    cost = missed / 1e6 * 1 + cached / 1e6 * 0.02 + u.get("completion_tokens", 0) / 1e6 * 2
                    token_usage["cost"] = token_usage.get("cost", 0) + cost
                    token_usage["last_round_cost"] = cost
            # 构建 assistant 消息
            assistant_msg = {"role": "assistant", "content": full_text or None}
            if tool_calls_acc:
                assistant_msg["tool_calls"] = [
                    {"id": v["id"], "type": "function", "function": v["function"]}
                    for v in sorted(tool_calls_acc.values(), key=lambda x: x.get("id", ""))
                ]
            return {"messages": [assistant_msg]}

        # ── 非流式模式 ──
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
            token_usage["last_prompt"] = prompt_tk  # 当前上下文用量
            # 检测缓存命中
            cached_tk = (
                usage.get("prompt_tokens_details", {}).get("cached_tokens")
                or usage.get("prompt_cache_hit_tokens")
                or usage.get("cache_read_input_tokens", 0)
            )
            token_usage["cached"] = token_usage.get("cached", 0) + cached_tk
            # 估算费用：缓存命中 ¥0.02/1M, 未命中 ¥1/1M, 输出 ¥2/1M
            missed_tk = prompt_tk - cached_tk
            cost = missed_tk / 1e6 * 1 + cached_tk / 1e6 * 0.02 + compl_tk / 1e6 * 2
            token_usage["cost"] = token_usage.get("cost", 0) + cost
            token_usage["last_round_cost"] = cost
            cache_info = f" cache:{cached_tk}" if cached_tk else ""
            print(
                f"      📊 tokens: +{total_tk} "
                f"(P:{prompt_tk}{cache_info} C:{compl_tk}) "
                f"¥{cost:.4f} | 累计: {token_usage['total']}",
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
        """条件路由：tool_calls → tools，最终回答 → verify"""
        messages = state.get("messages", [])
        if not messages:
            return END
        last = messages[-1]
        if last.get("tool_calls"):
            return "tools"
        return "verify"

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

            result = _execute_tool(name, args, paper_store, glm_api_key, profile_manager)

            if isinstance(result, str) and len(result) > 12000:
                result = result[:12000] + "\n\n...（截断至 12000 字符）"

            tool_msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        return {"messages": tool_msgs}

    # ═══ 验证节点 ═══

    def verify_node(state: AgentState) -> dict:
        """自动修正回复 + 智能压缩 + 跨论文库自检。不合格→反馈给 LLM 重新生成"""
        messages = state.get("messages", [])
        if not messages:
            return {}
        last_msg = messages[-1]
        content = last_msg.get("content", "")
        if not content:
            return {}

        # 本轮是否用了工具？
        recent = list(messages[-8:])
        has_tools = any(m.get("role") == "tool" for m in recent)

        # ── 智能上下文压缩 ──
        if has_tools:
            for i, m in enumerate(recent):
                if m.get("role") != "tool":
                    continue
                txt = m.get("content", "")
                if txt.startswith("📄") and len(txt) > 3000 and "摘要" in content.lower():
                    recent[i] = dict(m, content="📄 PDF原文已压缩（全文已索引至本地库，可用 query_papers 检索）")
                    print("      📦 压缩 read_pdf 原文", file=sys.stderr)

        tool_texts = []
        has_read_pdf = False
        for m in recent:
            if m.get("role") == "tool":
                t = m.get("content", "")[:2000]  # tool_texts 是文本片段，公式可能在后半段
                tool_texts.append(t)
                if "📄" in t:
                    has_read_pdf = True

        if not has_tools:
            return {}

        # ── 跨论文库自检 ──
        library_context = ""
        if paper_store and has_read_pdf:
            try:
                lib = paper_store.query(content[:200], top_k=2)
                if lib:
                    lib_chunks = [f"[{r['title']}] {r['text'][:300]}" for r in lib]
                    library_context = (
                        "\n--- Relevant local papers (for cross-checking) ---\n"
                        + "\n".join(lib_chunks)
                    )
            except Exception:
                pass

        # Token 预算
        pct = 0
        if token_usage and token_usage.get("last_prompt"):
            limit = token_usage.get("context_limit", 131072)
            if limit > 0:
                pct = min(token_usage["last_prompt"] / limit, 1.0)

        # ── 验证 prompt ──
        verify_prompt = (
            "Review this AI response for factual errors. Reply with 'OK' if correct.\n"
            "If issues found, list them concisely:\n\n"
            "Rules:\n"
            "1. Formula claims NOT in tool results → flag (BUT: if query_papers returned text about formulas, the formulas may be in LaTeX or scattered across chunks — be lenient)\n"
            "2. Unclear source → flag\n"
            "3. Contradicts tool results → flag\n\n"
            f"--- Tool results ---\n{chr(10).join(f'[T{i+1}] {t}' for i, t in enumerate(tool_texts))}"
            f"{library_context}\n\n"
            f"--- Response ---\n{content[:1500]}\n\n"
            "Issues found (or OK):"
        )

        try:
            resp = requests.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": verify_prompt}],
                      "stream": False, "temperature": 0.1},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            return {}

        if "usage" in resp.json() and token_usage:
            vu = resp.json()["usage"]
            for k in ("prompt", "completion", "total"):
                token_usage[k] += vu.get(f"{k}_tokens", 0)
            token_usage["calls"] += 1

        # OK → 通过
        if not result or result.upper().startswith("OK"):
            return {}

        # 不合格 → 反馈给 LLM 重新生成（找到原始用户问题）
        # 找到最近一条 user 消息（不含 [系统验证）
        user_question = ""
        for m in reversed(messages):
            if m.get("role") == "user" and "[系统验证" not in m.get("content", ""):
                user_question = m.get("content", "")
                break
        feedback = (
            f"[系统验证] 上一轮回答存在以下问题:\n{result}\n\n"
            f"请修正后重新回答用户的原始问题。用户的问题是:\n{user_question[:500]}"
        )
        if pct > 0.9:
            feedback += "\n\n⚠️ 上下文已用 90%+，建议缩短回复。"
        print(f"      🔍 验证不合格 → 反馈 LLM 重试", file=sys.stderr)
        return {"metadata": {
            "verify_feedback": feedback,
            "verify_count": 1 + sum(1 for m in messages if m.get("role") == "system" and "verify" in m.get("content", "")),
        }}

    # ═══ 验证路由 ═══

    def verify_router(state: AgentState) -> str:
        """验证通过 → END，不合格 → 反馈给 llm 重新生成（最多重试 3 次）"""
        md = state.get("metadata", {})
        count = md.get("verify_count", 0)
        if count >= 3:
            return END
        if md.get("verify_feedback"):
            return "llm"
        return END

    # ═══ 构图 ═══

    graph = StateGraph(AgentState)
    graph.add_node("llm", llm_node)
    graph.add_node("tools", tool_node)
    graph.add_node("verify", verify_node)

    graph.set_entry_point("llm")
    graph.add_conditional_edges("llm", router, {"tools": "tools", "verify": "verify"})
    graph.add_edge("tools", "llm")
    graph.add_conditional_edges("verify", verify_router, {"llm": "llm", END: END})

    conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))


# ═══════════════════════════════════════════════════════════════
#  工具执行逻辑
# ═══════════════════════════════════════════════════════════════

def _execute_tool(name: str, args: dict, paper_store=None, glm_api_key: str = "", profile_manager=None) -> str:
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
            result = read_pdf_enhanced(str(pdf_path), max_pages=max_pages, max_chars=None)
        except ImportError as e:
            return f"❌ {e}"
        except Exception as e:
            return f"❌ PDF 解析失败: {type(e).__name__}: {e}"

        # ── 提取图片 ──
        if result and not result.startswith("❌"):
            try:
                from pdf_reader import extract_images
                imgs = extract_images(str(pdf_path), max_pages=max_pages)
                if imgs:
                    result += "\n\n🖼 **提取的图片**:\n" + "\n".join(f"  - {i}" for i in imgs)
            except Exception as e:
                print(f"      ⚠ 图片提取失败: {e}", file=sys.stderr)

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
                    "\n\n---\n"
                    "📋 **请对这篇论文生成一个结构化摘要卡片**，覆盖以下维度（控制在 10 行以内）：\n"
                    "1. **核心问题** — 这篇论文要解决什么\n"
                    "2. **方法一句话** — 核心思路是什么\n"
                    "3. **关键公式** — 最重要的 1-2 个公式（可用 query_papers 检索确认）\n"
                    "4. **实验结论** — 主要实验结果\n"
                    "5. **局限性/未解决的问题**\n"
                    "6. **与本方向的其他论文关系**（如有已读论文）\n\n"
                    "论文全文已自动索引到本地库，后续追问细节时请用 query_papers 精准检索。"
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

    # ── describe_image ──
    if name == "describe_image":
        image_path = args.get("image_path", "")
        if not image_path:
            return "❌ 请提供图片路径。"
        import base64
        p = Path(image_path)
        # 尝试常见目录 fallback
        for d in [Path("."), Path("data/papers"), Path("data/papers/images")]:
            candidate = d / p.name
            if candidate.exists():
                p = candidate
                break
        if not p.exists():
            return f"❌ 图片不存在: {image_path}"
        if not glm_api_key:
            return "❌ GLM-4V API Key 未配置。请在 .env 中设置 GLM_API_KEY。"
        try:
            img_data = p.read_bytes()
            b64 = base64.b64encode(img_data).decode()
            ext = p.suffix.lstrip(".").lower()
            mime = f"image/{'jpeg' if ext in ('jpg','jpeg') else ext}"
        except Exception as e:
            return f"❌ 读取图片失败: {e}"

        glm_payload = {
            "model": "glm-4v",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请详细描述这张图的内容。如果是网络架构图，描述每层的结构和数据流；如果是流程图，描述每个步骤；如果是实验数据图，描述数据和结论。用中文回答。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]
            }],
            "temperature": 0.3,
            "stream": False,
        }
        try:
            resp = requests.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                headers={
                    "Authorization": f"Bearer {glm_api_key}",
                    "Content-Type": "application/json",
                },
                json=glm_payload,
                timeout=60,
            )
            resp.raise_for_status()
            desc = resp.json()["choices"][0]["message"]["content"]
            return f"🖼️ 图片描述 ({p.name}):\n{desc}"
        except Exception as e:
            return f"❌ GLM-4V 调用失败: {e}"

    # ── update_profile ──
    if name == "update_profile":
        if not profile_manager:
            return "❌ 画像功能未启用。"
        action = args.get("action", "")
        content = args.get("content", "")
        if not action or not content:
            return "❌ 请提供 action 和 content。"
        try:
            profile_manager.update_from_agent(action, content)
            return f"✅ 画像已更新: {action}"
        except Exception as e:
            return f"❌ 画像更新失败: {e}"

    # ── 未知工具 ──
    return f"❌ 未知工具: {name}"

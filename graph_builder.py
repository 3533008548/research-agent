"""
🧠 LangGraph Agent 图结构 — 科研助手推理引擎

模块拆分:
  prompts.py      → 系统提示词
  tool_schemas.py → 工具 schema
  tools/          → 工具实现
  graph_builder.py → 图结构 + 节点 + 路由（本文件）

图结构:
  START → llm ── tool_calls? ──→ tools → llm
               ── no         ──→ verify ── OK ──→ END
                                         ── 不合格 ──→ llm
"""

import json
import sys
import sqlite3
from pathlib import Path
from typing import Annotated, TypedDict

import requests
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

from prompts import SYSTEM_PROMPT
from tool_schemas import get_tool_schemas
from tools import execute_tool


# ═══════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════

class AgentState(TypedDict):
    messages: Annotated[list[dict], lambda a, b: a + b]
    metadata: dict


# ═══════════════════════════════════════════════════════════════
#  build_graph
# ═══════════════════════════════════════════════════════════════

def build_graph(
    api_key: str,
    api_url: str = "https://api.deepseek.com/chat/completions",
    model: str = "deepseek-chat",
    paper_store = None,
    token_usage: dict = None,
    checkpoint_db: str = "checkpoint.db",
    glm_api_key: str = "",
    stream_callback = None,
    profile_manager = None,
    memory_store = None,  # MemoryStore 实例
):
    tool_schemas = get_tool_schemas()

    # ═══ LLM 节点 ═══

    def llm_node(state: AgentState) -> dict:
        messages = list(state.get("messages", []))

        # 清理孤儿 tool_calls
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
            profile_text = profile_manager.summary() if profile_manager else ""
            prompt = SYSTEM_PROMPT
            if profile_text:
                prompt = f"[用户画像] {profile_text}\n\n{prompt}"
            messages.insert(0, {"role": "system", "content": prompt})

        verify_fb = state.get("metadata", {}).get("verify_feedback", "")
        if verify_fb:
            messages.append({"role": "system", "content": verify_fb})

        if memory_store:
            topic = state.get("metadata", {}).get("topic", "")
            recent = memory_store.get_recent_summary("research-main", topic)
            if recent:
                messages.append({"role": "system", "content": f"[对话摘要] {recent}"})

        payload = {
            "model": model, "messages": messages, "tools": tool_schemas,
            "tool_choice": "auto", "temperature": 0.7,
            "stream": stream_callback is not None,
        }

        resp = requests.post(api_url, headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }, json=payload, timeout=120)

        if resp.status_code == 429:
            return {"messages": [{"role": "assistant", "content": "⚠️ API 限流，请稍后再试"}]}
        if resp.status_code >= 400:
            body = resp.text[:500] if resp.text else "(empty)"
            print(f"      ❌ API {resp.status_code}: {body}", file=sys.stderr)
        resp.raise_for_status()

        # 流式模式
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
                if token_usage and "usage" in chunk:
                    u = chunk["usage"]
                    for k in ("prompt", "completion", "total"):
                        token_usage[k] += u.get(f"{k}_tokens", 0)
                    token_usage["calls"] += 1
                    token_usage["last_prompt"] = u.get("prompt_tokens", 0)
                    cached = (u.get("prompt_tokens_details", {}).get("cached_tokens")
                              or u.get("prompt_cache_hit_tokens", 0))
                    token_usage["cached"] = token_usage.get("cached", 0) + cached
                    missed = u.get("prompt_tokens", 0) - cached
                    cost = missed / 1e6 * 1 + cached / 1e6 * 0.02 + u.get("completion_tokens", 0) / 1e6 * 2
                    token_usage["cost"] = token_usage.get("cost", 0) + cost
                    token_usage["last_round_cost"] = cost
            assistant_msg = {"role": "assistant", "content": full_text or None}
            if tool_calls_acc:
                assistant_msg["tool_calls"] = [
                    {"id": v["id"], "type": "function", "function": v["function"]}
                    for v in sorted(tool_calls_acc.values(), key=lambda x: x.get("id", ""))
                ]
            return {"messages": [assistant_msg]}

        # 非流式模式
        data = resp.json()
        msg = data["choices"][0]["message"]
        if token_usage and "usage" in data:
            u = data["usage"]
            for k in ("prompt", "completion", "total"):
                token_usage[k] += u.get(f"{k}_tokens", 0)
            token_usage["calls"] += 1
            token_usage["last_prompt"] = u.get("prompt_tokens", 0)
            cached = (u.get("prompt_tokens_details", {}).get("cached_tokens")
                      or u.get("prompt_cache_hit_tokens", 0))
            token_usage["cached"] = token_usage.get("cached", 0) + cached
            missed = u.get("prompt_tokens", 0) - cached
            cost = missed / 1e6 * 1 + cached / 1e6 * 0.02 + u.get("completion_tokens", 0) / 1e6 * 2
            token_usage["cost"] = token_usage.get("cost", 0) + cost
            token_usage["last_round_cost"] = cost
        assistant_msg = {"role": "assistant", "content": msg.get("content") or None}
        if msg.get("tool_calls"):
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                for tc in msg["tool_calls"]
            ]
        return {"messages": [assistant_msg]}

    # ═══ 路由 ═══

    def router(state: AgentState) -> str:
        messages = state.get("messages", [])
        if not messages:
            return END
        last = messages[-1]
        if last.get("tool_calls"):
            return "tools"
        return "verify"

    # ═══ 工具节点 ═══

    def tool_node(state: AgentState) -> dict:
        messages = state.get("messages", [])
        tool_calls = messages[-1].get("tool_calls", [])
        tool_msgs = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            print(f"      🔧 {name}({json.dumps(args, ensure_ascii=False)})", file=sys.stderr)
            result = execute_tool(name, args, paper_store=paper_store, glm_api_key=glm_api_key, profile_manager=profile_manager, memory_store=memory_store)
            if isinstance(result, str) and len(result) > 12000:
                result = result[:12000] + "\n\n...（截断至 12000 字符）"
            tool_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        return {"messages": tool_msgs}

    # ═══ 验证节点 ═══

    def verify_node(state: AgentState) -> dict:
        messages = state.get("messages", [])
        if not messages:
            return {}
        last_msg = messages[-1]
        content = last_msg.get("content", "")
        if not content:
            return {}
        recent = list(messages[-8:])
        has_tools = any(m.get("role") == "tool" for m in recent)

        if not has_tools:
            return {}

        # ── 状态提示 ──
        retry_n = state.get("metadata", {}).get("verify_count", 0)
        if token_usage:
            token_usage["verify_status"] = f"🔄 第{retry_n+1}次修改..." if retry_n else "🔍 验证中..."

        # 智能压缩
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
                t = m.get("content", "")[:2000]
                tool_texts.append(t)
                if "📄" in t:
                    has_read_pdf = True

        if not has_tools:
            return {}

        library_context = ""
        if paper_store and has_read_pdf:
            try:
                lib = paper_store.query(content[:200], top_k=2)
                if lib:
                    lib_chunks = [f"[{r['title']}] {r['text'][:300]}" for r in lib]
                    library_context = "\n--- Relevant local papers ---\n" + "\n".join(lib_chunks)
            except Exception:
                pass

        verify_prompt = (
            "Review this AI response for factual errors. Reply with 'OK' if correct.\n"
            "Rules:\n"
            "1. Formula claims NOT in tool results → flag (BUT: query_papers results may have LaTeX — be lenient)\n"
            "2. Unclear source → flag\n"
            "3. Contradicts tool results → flag\n\n"
            f"--- Tool results ---\n{chr(10).join(f'[T{i+1}] {t}' for i, t in enumerate(tool_texts))}"
            f"{library_context}\n\n--- Response ---\n{content[:1500]}\n\nIssues (or OK):"
        )

        try:
            resp = requests.post(api_url, headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            }, json={"model": model, "messages": [{"role": "user", "content": verify_prompt}],
                      "stream": False, "temperature": 0.1}, timeout=30)
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            return {}
        if "usage" in resp.json() and token_usage:
            vu = resp.json()["usage"]
            for k in ("prompt", "completion", "total"):
                token_usage[k] += vu.get(f"{k}_tokens", 0)
            token_usage["calls"] += 1

        if not result or result.upper().startswith("OK"):
            if token_usage:
                token_usage.pop("verify_status", None)
            # ── 对话摘要：上下文 > 50% 或 > 10 轮对话时生成 ──
            if memory_store:
                user_msg_count = sum(1 for m in messages if m.get("role") == "user")
                pct_ctx = (token_usage.get("last_prompt", 0) / token_usage.get("context_limit", 131072)) if token_usage else 0
                if user_msg_count > 10 or pct_ctx > 0.5:
                    try:
                        # 提取最近 20 条消息生成摘要
                        recent_msgs = []
                        for m in messages[-20:]:
                            c = m.get("content", "") or ""
                            recent_msgs.append(f"[{m.get('role','')}] {c[:300]}")
                        raw = "\n".join(recent_msgs)
                        sum_prompt = f"Summarize this research conversation in 150 chars Chinese:\n{raw[:3000]}"
                        sr = requests.post(api_url, headers={
                            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                        }, json={"model": model, "messages": [{"role": "user", "content": sum_prompt}],
                                  "stream": False, "temperature": 0.2}, timeout=20)
                        if sr.status_code == 200:
                            summary = sr.json()["choices"][0]["message"]["content"].strip()[:300]
                            topic = state.get("metadata", {}).get("topic", "")
                            memory_store.add_summary("research-main", topic, summary)
                    except Exception:
                        pass
            return {}

        user_q = ""
        for m in reversed(messages):
            if m.get("role") == "user" and "[系统验证" not in m.get("content", ""):
                user_q = m.get("content", ""); break
        feedback = f"[系统验证] 上一轮回答存在以下问题:\n{result}\n\n请修正后重新回答。用户的问题是:\n{user_q[:500]}"
        count = 1 + state.get("metadata", {}).get("verify_count", 0)
        print(f"      🔍 验证不合格 (第{count}次) → 反馈 LLM", file=sys.stderr)
        return {"metadata": {
            "verify_feedback": feedback,
            "verify_count": count,
            "verify_issues": result,  # 保存问题文本，最终失败时用于横幅
        }}

    # ═══ 验证路由 ═══

    def verify_router(state: AgentState) -> str:
        md = state.get("metadata", {})
        count = md.get("verify_count", 0)
        if count >= 3:
            # 最终失败 — 不覆盖，在原回答前加警告横幅
            issues = md.get("verify_issues", "")
            msgs = state.get("messages", [])
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "assistant" and msgs[i].get("content"):
                    warning = f"⚠️ 以下回答未通过自动验证，可能存在以下问题:\n{issues}\n\n---\n"
                    msgs[i] = dict(msgs[i], content=warning + msgs[i]["content"])
                    break
            if token_usage:
                token_usage.pop("verify_status", None)
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

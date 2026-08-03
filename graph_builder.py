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
            if isinstance(result, str):
                # ── 按工具类型差异化截断 ──
                limits = {
                    "query_papers": 5000,
                    "search_papers": 3000,
                    "read_pdf": 12000,
                    "describe_image": 2000,
                    "memory_search": 1500,
                }
                limit = limits.get(name, 3000)
                if len(result) > limit:
                    result = result[:limit] + f"\n\n...（截断至 {limit} 字符）"
            tool_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        return {"messages": tool_msgs}

    # ═══ 验证节点 ═══

    def _is_subjective_question(q: str) -> bool:
        """判断是否为主观问题（无需事实验证）"""
        subjective_markers = ["你觉得", "怎么看待", "你的观点", "你的看法", "推荐一下", "有什么建议",
                              "喜不喜欢", "好不好用", "值不值得", "opinion", "think about", "your view",
                              "recommend", "suggest"]
        ql = q.lower()
        # 含查证意图的（"论文里怎么说的"）不算主观
        if any(m in ql for m in ["论文里", "原文", "文中", "paper says", "in the paper", "what does"]):
            return False
        return any(m in ql for m in subjective_markers)

    def _parse_issues(raw: str) -> tuple[list[str], list[str]]:
        """解析 verify 结果 → (严重问题列表, 轻微问题列表)"""
        severe, minor = [], []
        for line in raw.split("\n"):
            ls = line.strip()
            if ls.startswith("[严重]") or ls.startswith("[SEVERE]"):
                severe.append(ls)
            elif ls.startswith("[轻微]") or ls.startswith("[MINOR]"):
                minor.append(ls)
        return severe, minor

    def verify_node(state: AgentState) -> dict:
        messages = state.get("messages", [])
        metadata = state.get("metadata") or {}

        def _complete_verification() -> dict:
            """结束本轮验证时清除只对重试有效的状态。"""
            cleaned = dict(metadata)
            for key in (
                "verify_feedback", "verify_count", "verify_issues", "verify_history",
            ):
                cleaned.pop(key, None)
            return {"metadata": cleaned} if cleaned != metadata else {}

        if not messages:
            return _complete_verification()
        last_msg = messages[-1]
        content = last_msg.get("content", "")
        if not content:
            return _complete_verification()
        recent = list(messages[-8:])
        has_tools = any(m.get("role") == "tool" for m in recent)

        # ── 第1层：跳过门控 ──
        if not has_tools:
            return _complete_verification()
        # 短回复跳过（无事实可查）
        if len(content) < 100:
            return _complete_verification()
        # 主观问题跳过
        user_q = ""
        for m in reversed(messages):
            if m.get("role") == "user" and "[系统验证" not in m.get("content", ""):
                user_q = m.get("content", ""); break
        if _is_subjective_question(user_q):
            return _complete_verification()

        # ── 状态提示 ──
        retry_n = state.get("metadata", {}).get("verify_count", 0)
        if token_usage:
            token_usage["verify_status"] = f"🔄 第{retry_n+1}次修改..." if retry_n else "🔍 验证中..."

        # 智能压缩
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

        # ── verify 输入精简：低价值工具结果不送验证 ──
        # 只保留 query_papers / read_pdf / search_papers 的结果（含事实内容）
        def _is_fact_tool(text: str) -> bool:
            return any(marker in text[:80] for marker in ["📚", "📄", "检索结果", "搜索结果", "PDF"])
        fact_texts = [t for t in tool_texts if _is_fact_tool(t)]
        if not fact_texts:
            fact_texts = tool_texts[:1]  # 兜底：至少送一条

        library_context = ""
        if paper_store and has_read_pdf:
            try:
                lib = paper_store.query(content[:200], top_k=2)
                if lib:
                    lib_chunks = [f"[{r['title']}] {r['text'][:300]}" for r in lib]
                    library_context = "\n--- Relevant local papers ---\n" + "\n".join(lib_chunks)
            except Exception:
                pass

        # ── 增量验证：上次的问题只重查修复情况 ──
        prev_issues = state.get("metadata", {}).get("verify_issues", "")
        incr_note = ""
        if prev_issues:
            incr_note = f"\nPreviously flagged issues (verify they are FIXED in the new response, don't re-flag unrelated):\n{prev_issues[:800]}\n"

        verify_prompt = (
            "Review this AI response for factual errors. Reply 'OK' if correct.\n"
            "For each issue, format as: [严重|轻微] description\n"
            "[严重] = contradicts tool results / wrong formula or data\n"
            "[轻微] = unclear source / vague wording, content is fine\n\n"
            "After issues, judge relevance: is the issue about the user's CORE question? Reply [需修正] or [可忽略] per issue.\n\n"
            f"User question: {user_q[:300]}\n"
            f"--- Tool results ---\n{chr(10).join(f'[T{i+1}] {t}' for i, t in enumerate(fact_texts))}"
            f"{library_context}"
            f"{incr_note}"
            f"\n--- Response ---\n{content[:1500]}\n\nIssues (or OK):"
        )

        try:
            resp = requests.post(api_url, headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            }, json={"model": model, "messages": [{"role": "user", "content": verify_prompt}],
                      "stream": False, "temperature": 0.1}, timeout=30)
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            # ── 降级静默：API 失败不阻塞，但状态栏提示 ──
            if token_usage:
                token_usage["verify_status"] = "⚠️ 验证跳过（API 失败）"
            print(f"      ⚠ verify API 失败: {e}", file=sys.stderr)
            return _complete_verification()
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
                # ── 摘要频率控制：10 分钟内不重复摘要 ──
                from datetime import datetime as _dt, timedelta as _td
                last_sum = memory_store.get_last_summary_time("research-main")
                recent_sum = False
                if last_sum:
                    try:
                        recent_sum = (_dt.fromisoformat(last_sum) + _td(minutes=10)) > _dt.now()
                    except Exception:
                        recent_sum = False
                if (user_msg_count > 10 or pct_ctx > 0.5) and not recent_sum:
                    try:
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
                # ── 预算预警前置：> 60% 提示（> 90% 强警告）──
                if pct_ctx > 0.9 and token_usage:
                    token_usage["budget_warning"] = "⚠️ 上下文 90%+，建议 /new"
                elif pct_ctx > 0.6 and token_usage:
                    token_usage["budget_warning"] = f"📊 上下文 {int(pct_ctx*100)}%"
            return _complete_verification()

        # ── 分级处理 ──
        severe, minor = _parse_issues(result)
        count = 1 + state.get("metadata", {}).get("verify_count", 0)
        # 重试历史：连续 [轻微] 触发 2 次 → 降级为仅追加提示
        hist = state.get("metadata", {}).get("verify_history", {})
        minor_key = "minor_repeat"
        hist[minor_key] = hist.get(minor_key, 0) + 1 if minor and not severe else 0

        need_regenerate = bool(severe)
        if not need_regenerate and minor:
            # 轻微问题：判断是否与用户问题强相关
            need_regenerate = "[需修正]" in result
            if hist.get(minor_key, 0) >= 2 and not severe:
                need_regenerate = False  # 连续轻微 → 不重生成，追加提示

        if not need_regenerate and minor:
            # 仅追加提示，不重生成
            note = "\n\n⚠️ 注: " + "; ".join(minor[:2])
            if token_usage:
                token_usage.pop("verify_status", None)
            update = _complete_verification()
            update["messages"] = [{"role": "assistant", "content": content + note}]
            return update

        # ── 需要重生成 ──
        if count >= 3:
            if token_usage:
                token_usage.pop("verify_status", None)
            warning = f"⚠️ 以下回答未通过自动验证，可能存在以下问题:\n{result}\n\n---\n"
            update = _complete_verification()
            update["messages"] = [{"role": "assistant", "content": warning + content}]
            return update

        feedback = f"[系统验证] 上一轮回答存在以下问题:\n{result}\n\n请修正后重新回答。用户的问题是:\n{user_q[:500]}"
        print(f"      🔍 验证不合格 (第{count}次, {'严重' if severe else '轻微'}) → 反馈 LLM", file=sys.stderr)
        return {"metadata": {
            "verify_feedback": feedback,
            "verify_count": count,
            "verify_issues": result,
            "verify_history": hist,
        }}

    # ═══ 验证路由 ═══

    def verify_router(state: AgentState) -> str:
        md = state.get("metadata", {})
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

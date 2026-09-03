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
import threading
import time
from pathlib import Path
from typing import Annotated, Callable, TypedDict

import requests
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

from cancellation import RequestCancelledError, raise_if_cancelled
from conversation_memory import context_usage_ratio, maybe_store_conversation_summary
from llm_client import (
    LLMCircuitOpenError,
    LLMQueueFullError,
    LLMRequestTimeoutError,
    RequestPolicy,
    RequestPriority,
)
from prompts import SYSTEM_PROMPT
from tool_catalog import get_tool_schemas
from tool_runtime import ToolExecutionContext, ToolRuntime
from tools import execute_tool


# ═══════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════

class AgentState(TypedDict):
    messages: Annotated[list[dict], lambda a, b: a + b]
    metadata: dict


def sanitize_model_messages(messages: list[dict]) -> list[dict]:
    """Return a payload-safe history without mutating persisted checkpoints."""
    result_ids = {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str)
    }
    usable_tool_ids: set[str] = set()
    sanitized: list[dict] = []

    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        message = dict(raw_message)
        role = message.get("role")

        if role == "assistant":
            calls = [
                call for call in (message.get("tool_calls") or [])
                if isinstance(call, dict)
                and isinstance(call.get("id"), str)
                and call["id"] in result_ids
                and isinstance(call.get("function"), dict)
                and call["function"].get("name")
            ]
            if calls:
                message["tool_calls"] = calls
                usable_tool_ids.update(call["id"] for call in calls)
            else:
                message.pop("tool_calls", None)

            # DeepSeek requires an assistant message to have content or tool_calls.
            if message.get("content") in (None, "") and not calls:
                continue
            sanitized.append(message)
            continue

        if role == "tool":
            if message.get("tool_call_id") not in usable_tool_ids:
                continue
            if message.get("content") is None:
                message["content"] = ""
            sanitized.append(message)
            continue

        if role in {"system", "user"} and message.get("content") not in (None, ""):
            sanitized.append(message)

    return sanitized


# ═══════════════════════════════════════════════════════════════
#  build_graph
# ═══════════════════════════════════════════════════════════════

def build_graph(
    api_key: str,
    api_url: str = "https://api.deepseek.com/chat/completions",
    model: str = "deepseek-chat",
    paper_store = None,
    token_usage: dict = None,
    checkpoint_db: str | None = None,
    glm_api_key: str = "",
    stream_callback = None,
    event_callback = None,
    cancel_event: threading.Event | None = None,
    profile_manager = None,
    memory_store = None,  # MemoryStore 实例
    verify_timeout_seconds: int = 8,
    verify_guard = None,
    llm_client = None,
    system_prompt: str | None = None,
    allowed_tool_names: set[str] | None = None,
    enable_verify: bool = True,
    request_policy: RequestPolicy | None = None,
    max_tool_rounds: int | None = None,
    tool_argument_normalizer: Callable[[str, dict], dict] | None = None,
    tool_context: ToolExecutionContext | None = None,
):
    # A graph must use the process-wide client owned by ResearchAgent. Creating
    # one here would silently defeat shared admission control in multi-agent
    # execution.
    if llm_client is None:
        raise ValueError("build_graph requires the shared llm_client instance")
    if checkpoint_db is None:
        from runtime_paths import get_runtime_paths
        checkpoint_db = str(get_runtime_paths().checkpoint_db)
    tool_schemas = get_tool_schemas(allowed_tool_names)
    if max_tool_rounds is not None:
        max_tool_rounds = max(1, int(max_tool_rounds))

    def emit(event_type: str, **details) -> None:
        """Best-effort trace hook; instrumentation must never interrupt inference."""
        if not event_callback:
            return
        try:
            event_callback({"type": event_type, **details})
        except Exception:
            pass

    def ensure_active(stage: str) -> None:
        """在图节点边界停止已取消的请求，避免启动下一次外部调用。"""
        if cancel_event is not None and cancel_event.is_set():
            emit("request_cancelled", stage=stage)
        raise_if_cancelled(cancel_event, f"请求已在 {stage} 取消")

    runtime_context = tool_context or ToolExecutionContext(
        run_kind="research" if allowed_tool_names is not None else "chat",
        allowed_tool_names=(
            frozenset(allowed_tool_names) if allowed_tool_names is not None else None
        ),
        cancel_event=cancel_event,
    )
    # Keep the dispatcher injectable at this seam: it preserves lightweight
    # graph tests while the runtime owns all cross-cutting policy.
    tool_runtime = ToolRuntime(
        runtime_context, event_callback=emit, executor=execute_tool,
    )

    def watch_stream_cancellation(response):
        """取消时关闭已建立的 SSE 连接，打断 ``iter_lines`` 的阻塞读取。"""
        if cancel_event is None:
            return lambda: None
        stopped = threading.Event()

        def _watch() -> None:
            while not stopped.wait(0.1):
                if cancel_event.is_set():
                    try:
                        response.close()
                    except Exception:
                        pass
                    return

        threading.Thread(target=_watch, daemon=True, name="cancel-sse-watch").start()
        return stopped.set

    # ═══ LLM 节点 ═══

    def llm_node(state: AgentState) -> dict:
        ensure_active("llm")
        messages = sanitize_model_messages(list(state.get("messages", [])))

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

        # 对话上下文不会替代核心约束提示词。
        profile_text = profile_manager.summary() if profile_manager else ""
        prompt = system_prompt or SYSTEM_PROMPT
        if profile_text:
            prompt = f"[用户画像] {profile_text}\n\n{prompt}"
        messages.insert(0, {"role": "system", "content": prompt})

        verify_fb = state.get("metadata", {}).get("verify_feedback", "")
        if verify_fb:
            messages.append({"role": "system", "content": verify_fb})

        if memory_store:
            metadata = state.get("metadata", {})
            thread_id = metadata.get("session_id", "research-main")
            recent = memory_store.get_recent_summary(thread_id)
            if recent:
                messages.append({"role": "system", "content": f"[对话摘要] {recent}"})

        payload = {
            "model": model, "messages": messages,
            "temperature": 0.7, "stream": stream_callback is not None,
        }
        # An empty tool list with ``tool_choice=auto`` is rejected by some
        # OpenAI-compatible providers.  Direct engineering answers therefore
        # omit tool fields completely instead of relying on model compliance.
        if tool_schemas:
            payload["tools"] = tool_schemas
            payload["tool_choice"] = "auto"

        def _api_status(message: str) -> None:
            if token_usage is not None:
                token_usage["api_status"] = message
            emit("llm_status", status=message)

        request_budget = llm_client.new_request_budget(
            request_policy or RequestPolicy(
                purpose="chat",
                priority=RequestPriority.INTERACTIVE,
            )
        )
        request_started = time.perf_counter()
        emit("llm_request_started", model=model, stream=stream_callback is not None)
        try:
            post_args = {
                "stream": stream_callback is not None,
                "on_status": _api_status,
                "budget": request_budget,
            }
            if cancel_event is not None:
                post_args["cancel_event"] = cancel_event
            resp = llm_client.post(payload, **post_args)
        except Exception as exc:
            emit("llm_request_failed", error_type=type(exc).__name__)
            raise
        emit("llm_response_headers", duration_ms=round((time.perf_counter() - request_started) * 1000, 1))

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

            def _consume_stream(response) -> bool:
                nonlocal full_text
                lines = iter(response.iter_lines(decode_unicode=True))
                while True:
                    ensure_active("stream_read")
                    if llm_client:
                        if cancel_event is None:
                            stream_read_ready = llm_client.prepare_stream_read(
                                response, request_budget,
                            )
                        else:
                            stream_read_ready = llm_client.prepare_stream_read(
                                response, request_budget, cancel_event=cancel_event,
                            )
                        if not stream_read_ready:
                            raise requests.exceptions.ReadTimeout("stream deadline exceeded")
                    try:
                        line = next(lines)
                    except StopIteration:
                        # 正常 EOF 但未收到协议结束标记，也属于不完整流，不能当作成功。
                        return False
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        return True
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

            while True:
                stop_watch = watch_stream_cancellation(resp)
                try:
                    completed = _consume_stream(resp)
                    if not completed:
                        raise requests.exceptions.ChunkedEncodingError(
                            "stream ended before [DONE]"
                        )
                    if llm_client:
                        llm_client.finish_stream(resp, success=True)
                    break
                except RequestCancelledError:
                    if llm_client:
                        llm_client.finish_stream(resp, success=False, cancelled=True)
                    else:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    emit("request_cancelled", stage="stream")
                    raise
                except requests.RequestException as exc:
                    if cancel_event is not None and cancel_event.is_set():
                        if llm_client:
                            llm_client.finish_stream(resp, success=False, cancelled=True)
                        else:
                            try:
                                resp.close()
                            except Exception:
                                pass
                        emit("request_cancelled", stage="stream")
                        raise RequestCancelledError("模型流式响应已取消") from exc
                    if llm_client:
                        llm_client.finish_stream(resp, success=False)
                    else:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    # 未输出任何 token 时可以安全重放同一模型请求；有内容后不重放，避免重复。
                    if (
                        not full_text and llm_client
                        and request_budget.reserve_retry()
                    ):
                        retry_args = {
                            "stream": True,
                            "on_status": _api_status,
                            "budget": request_budget,
                        }
                        if cancel_event is not None:
                            retry_args["cancel_event"] = cancel_event
                        resp = llm_client.post(payload, **retry_args)
                        continue
                    if not full_text:
                        raise LLMRequestTimeoutError(
                            "模型流式响应中断；未切换模型，请稍后点击重试"
                        ) from exc
                    interruption = "\n\n⚠️ 输出中断，已保留以上内容。发送 `/retry` 可重新请求。"
                    full_text += interruption
                    stream_callback(interruption)
                    if token_usage is not None:
                        token_usage["api_status"] = "⚠️ 流式输出中断，可发送 /retry"
                    break
                except BaseException:
                    # 客户端断开、回调取消等非 requests 异常也不能遗留 SSE 连接或并发槽。
                    if llm_client:
                        llm_client.finish_stream(resp, success=False)
                    else:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    raise
                finally:
                    stop_watch()
            assistant_msg = {"role": "assistant", "content": full_text or None}
            if tool_calls_acc:
                assistant_msg["tool_calls"] = [
                    {"id": v["id"], "type": "function", "function": v["function"]}
                    for v in sorted(tool_calls_acc.values(), key=lambda x: x.get("id", ""))
                ]
            emit(
                "llm_request_finished",
                duration_ms=round((time.perf_counter() - request_started) * 1000, 1),
                **request_budget.metrics(),
            )
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
        emit(
            "llm_request_finished",
            duration_ms=round((time.perf_counter() - request_started) * 1000, 1),
            **request_budget.metrics(),
        )
        return {"messages": [assistant_msg]}

    # ═══ 路由 ═══

    def router(state: AgentState) -> str:
        ensure_active("route")
        messages = state.get("messages", [])
        if not messages:
            return END
        last = messages[-1]
        if last.get("tool_calls"):
            return "tools"
        return "verify" if enable_verify else END

    def after_tools_router(state: AgentState) -> str:
        """End bounded research workers after their final evidence tool round.

        The normal chat graph remains unbounded apart from its outer recursion
        limit. Research workers, however, do not need to ask the model for one
        more prose turn after gathering evidence: the research synthesizer owns
        that task. Ending here preserves collected tool messages rather than
        losing them to a graph recursion error when a model keeps searching.
        """
        if max_tool_rounds is not None:
            rounds = sum(
                1 for message in state.get("messages", [])
                if message.get("role") == "assistant" and message.get("tool_calls")
            )
            if rounds >= max_tool_rounds:
                emit("tool_round_limit_reached", limit=max_tool_rounds)
                return END
        return "llm"

    # ═══ 工具节点 ═══

    def tool_node(state: AgentState) -> dict:
        ensure_active("tools")
        messages = state.get("messages", [])
        tool_calls = messages[-1].get("tool_calls", [])
        tool_msgs = []
        for tc in tool_calls:
            ensure_active("before_tool")
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if tool_argument_normalizer is not None:
                normalized_args = tool_argument_normalizer(name, dict(args))
                if isinstance(normalized_args, dict):
                    args = normalized_args
            print(f"      🔧 {name}", file=sys.stderr)
            result = tool_runtime.execute(
                name, args, paper_store=paper_store, glm_api_key=glm_api_key,
                profile_manager=profile_manager, memory_store=memory_store,
                llm_client=llm_client, model=model,
            )
            ensure_active("after_tool")
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
        ensure_active("verify")
        messages = state.get("messages", [])
        metadata = state.get("metadata") or {}

        def _complete_verification() -> dict:
            """结束本轮验证时清除只对本轮有效的状态。"""
            if token_usage:
                # The final answer already contains any user-facing caveat, and
                # the run timeline records a skipped/failed verification. Do not
                # leave an old optional-stage warning in the next chat turn.
                token_usage.pop("verify_status", None)
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

        # verify 是辅助质量保障，不能在服务异常时拖住主对话。
        if verify_guard and not verify_guard.allow_request():
            if token_usage:
                wait_seconds = verify_guard.remaining_seconds()
                token_usage["verify_status"] = (
                    f"⚠️ 验证暂时跳过（服务繁忙，约 {wait_seconds}s 后恢复）"
                )
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

        verify_policy = RequestPolicy(
            purpose="verify",
            priority=RequestPriority.VERIFY,
            deadline_seconds=verify_timeout_seconds,
            max_retries=0,
            counts_toward_circuit=False,
        )

        def _verify_status(message: str) -> None:
            if token_usage:
                token_usage["verify_status"] = f"🔎 验证中：{message}"
            emit("verify_status", status=message)

        try:
            verify_budget = llm_client.new_request_budget(
                verify_policy,
            )
            emit("verify_request_started", model=model, **verify_budget.metrics())
            resp = llm_client.post(
                {"model": model, "messages": [{"role": "user", "content": verify_prompt}],
                 "stream": False, "temperature": 0.1},
                stream=False,
                on_status=_verify_status,
                budget=verify_budget,
                cancel_event=cancel_event,
            )
            result = resp.json()["choices"][0]["message"]["content"].strip()
            emit("verify_request_finished", **verify_budget.metrics())
        except RequestCancelledError:
            emit("verify_request_cancelled")
            raise
        except (LLMQueueFullError, LLMCircuitOpenError) as e:
            # Queue pressure and the shared model circuit do not mean that the
            # verify feature itself is unhealthy. Skip this optional stage
            # without opening its local feature guard.
            if token_usage:
                token_usage["verify_status"] = "⚠️ 验证跳过（模型繁忙或暂不可用）"
            emit("verify_request_skipped", error_type=type(e).__name__)
            return _complete_verification()
        except Exception as e:
            # ── 快速降级：失败两次后熔断，后续请求直接返回主回答 ──
            if verify_guard:
                verify_guard.record_failure()
            if token_usage:
                token_usage["verify_status"] = "⚠️ 验证跳过（服务超时或不可用）"
            emit("verify_request_failed", error_type=type(e).__name__)
            print(f"      ⚠ verify 跳过: {type(e).__name__}: {e}", file=sys.stderr)
            return _complete_verification()
        if verify_guard:
            verify_guard.record_success()
        if "usage" in resp.json() and token_usage:
            vu = resp.json()["usage"]
            for k in ("prompt", "completion", "total"):
                token_usage[k] += vu.get(f"{k}_tokens", 0)
            token_usage["calls"] += 1

        if not result or result.upper().startswith("OK"):
            if token_usage:
                token_usage.pop("verify_status", None)
            # Successful evidence-backed turns may compact their recent history.
            if memory_store:
                pct_ctx = context_usage_ratio(token_usage)
                maybe_store_conversation_summary(
                    messages=messages,
                    metadata=metadata,
                    context_ratio=pct_ctx,
                    memory_store=memory_store,
                    llm_client=llm_client,
                    model=model,
                    timeout_seconds=verify_timeout_seconds,
                    cancel_event=cancel_event,
                )
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
    graph.add_conditional_edges(
        "llm", router, {"tools": "tools", "verify": "verify", END: END},
    )
    graph.add_conditional_edges("tools", after_tools_router, {"llm": "llm", END: END})
    graph.add_conditional_edges("verify", verify_router, {"llm": "llm", END: END})

    conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))

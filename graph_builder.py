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
from context_engine import prepare_model_context
from conversation_memory import context_usage_ratio, maybe_store_conversation_summary
from llm_client import (
    LLMCircuitOpenError,
    LLMQueueFullError,
    LLMRequestTimeoutError,
    LLMStreamError,
    RequestPolicy,
    RequestPriority,
)
from prompts import SYSTEM_PROMPT
from run_contract import safe_error_details
from tool_catalog import get_tool_schemas
from tool_runtime import ToolExecutionContext, ToolRuntime
from tools import execute_tool
from verification import MAX_VERIFY_PASSES, classify_review, verification_prompt


# ═══════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════

def merge_messages(previous: list[dict], incoming: list[dict]) -> list[dict]:
    """Replace a reviewed draft while retaining tool evidence and earlier turns."""
    merged = list(previous)
    for raw in incoming:
        message = dict(raw)
        if message.pop("_replace_answer", False):
            start = max((i for i, m in enumerate(merged) if m.get("role") == "user"), default=-1)
            merged = [m for i, m in enumerate(merged) if i <= start or
                      m.get("role") != "assistant" or m.get("tool_calls")]
        merged.append(message)
    return merged


class AgentState(TypedDict):
    messages: Annotated[list[dict], merge_messages]
    metadata: dict


def sanitize_model_messages(
    messages: list[dict], *, require_reasoning_content_for_tools: bool = False,
) -> list[dict]:
    """Return a payload-safe history without mutating persisted checkpoints.

    DeepSeek Flash thinking-mode tool turns require the model's complete
    ``reasoning_content`` to be echoed in later requests.  Checkpoints created
    before that field was preserved cannot be reconstructed, so their tool
    calls/results are removed from the *model view* rather than repeatedly
    sending an invalid request.  User and final assistant prose remain intact.
    """
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
        # Older versions appended private review notes to public answers. Keep
        # the answer, but do not feed these generated notes into the next task.
        if role == "assistant" and isinstance(message.get("content"), str):
            message["content"] = message["content"].split("\n\n核验提示（不触发自动重写）：\n", 1)[0]

        if role == "assistant":
            reasoning = message.get("reasoning_content")
            has_reasoning = isinstance(reasoning, str) and bool(reasoning)
            if not has_reasoning:
                message.pop("reasoning_content", None)
            calls = [
                call for call in (message.get("tool_calls") or [])
                if isinstance(call, dict)
                and isinstance(call.get("id"), str)
                and call["id"] in result_ids
                and isinstance(call.get("function"), dict)
                and call["function"].get("name")
            ]
            if require_reasoning_content_for_tools and not has_reasoning:
                calls = []
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

def tool_rounds_in(messages: list[dict]) -> int:
    """Count tool-issuing assistant turns, scoped to the latest user turn.

    Rounds are counted per assistant message that carries ``tool_calls`` rather
    than per individual call: a model asking for three tools at once has spent
    one round, not three.
    """
    turn_start = max(
        (index for index, message in enumerate(messages)
         if message.get("role") == "user"),
        default=-1,
    )
    return sum(
        1 for index, message in enumerate(messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
        and index > turn_start
    )


def budget_exhaustion_warning(remaining: int) -> dict:
    """A system nudge asking the model to finish before recursion cuts it off."""
    return {
        "role": "system",
        "content": (
            "[预算提醒]\n"
            f"本轮的工具调用预算还剩 {remaining} 次，之后系统会强制结束。\n"
            "不要再发起探索性或重试性的工具调用。请立即基于已获得的信息作答：\n"
            "1) 说清楚已经完成的部分；"
            "2) 若任务没做完，明确列出剩下要做的步骤，并说明下一步应从哪里继续；\n"
            "3) 不要提交内容不完整或被截断的补丁。"
        ),
    }


def build_graph(
    api_key: str,
    api_url: str = "https://api.deepseek.com/chat/completions",
    model: str = "deepseek-chat",
    paper_store = None,
    token_usage: dict = None,
    checkpoint_db: str | None = None,
    vision_model: str = "deepseek-flash",
    stream_callback = None,
    event_callback = None,
    cancel_event: threading.Event | None = None,
    profile_manager = None,
    memory_store = None,  # MemoryStore 实例
    verify_timeout_seconds: int = 120,
    verify_guard = None,
    llm_client = None,
    system_prompt: str | None = None,
    allowed_tool_names: set[str] | None = None,
    enable_verify: bool = True,
    request_policy: RequestPolicy | None = None,
    max_tool_rounds: int | None = None,
    # Soft budget used only to warn the model before the hard recursion limit
    # fires.  Without it a long task dies with GraphRecursionError and the
    # partial work is never surfaced to the user.
    tool_round_budget: int | None = None,
    tool_argument_normalizer: Callable[[str, dict], dict] | None = None,
    tool_context: ToolExecutionContext | None = None,
    force_tool_name: str | None = None,
    steer_provider: Callable[[str], list[str]] | None = None,
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
    forced_tool = str(force_tool_name or "").strip()
    if forced_tool and not any(
        schema.get("function", {}).get("name") == forced_tool for schema in tool_schemas
    ):
        raise ValueError("forced tool must be included in the available tool set")
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

    def inject_running_guidance(messages: list[dict], stage: str) -> None:
        """Add user steering only at an LLM boundary, never mid-request."""
        if steer_provider is None:
            return
        try:
            notes = [str(note).strip() for note in steer_provider(stage) if str(note).strip()]
        except Exception:
            return
        if not notes:
            return
        joined = "\n".join(f"- {note}" for note in notes)
        messages.append({
            "role": "system",
            "content": (
                "[运行中用户补充]\n"
                f"{joined}\n"
                "将其视为新的任务约束或待核验线索；若与已知证据冲突，指出冲突，"
                "不要将未经核验的补充表述为已证实事实。"
            ),
        })

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
        result_db=checkpoint_db if checkpoint_db != ':memory:' else None,
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

    # Once a visible draft needs replay, keep later attempts private. The run's
    # final answer replaces that draft through the existing done event, avoiding
    # duplicated text and replay of already-completed tool nodes.
    buffer_stream_output = False

    def llm_node(state: AgentState) -> dict:
        nonlocal buffer_stream_output
        metadata = dict(state.get("metadata") or {})
        metadata.pop("stream_incomplete", None)
        ensure_active("llm")
        messages = sanitize_model_messages(
            list(state.get("messages", [])),
            # DeepSeek Flash enables thinking by default.  Its tool protocol
            # rejects a historical tool turn that lacks the returned CoT.
            require_reasoning_content_for_tools=(
                model == "deepseek-flash" and bool(tool_schemas)
            ),
        )
        inject_running_guidance(messages, "llm")

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

        metadata = state.get("metadata", {})
        context_limit = 131_072
        if token_usage:
            try:
                context_limit = max(1, int(token_usage.get("context_limit", context_limit)))
            except (TypeError, ValueError):
                pass
        prepared_context = prepare_model_context(messages, context_limit=context_limit)
        messages = prepared_context.messages
        if prepared_context.compacted:
            if token_usage is not None:
                token_usage["context_compacted"] = True
                token_usage["context_estimated_before"] = prepared_context.original_tokens
                token_usage["context_estimated_after"] = prepared_context.prepared_tokens
            emit(
                "context_compacted",
                before_tokens=prepared_context.original_tokens,
                after_tokens=prepared_context.prepared_tokens,
            )

        # 对话上下文不会替代核心约束提示词。
        profile_text = profile_manager.summary() if profile_manager else ""
        prompt = system_prompt or SYSTEM_PROMPT
        if tool_schemas:
            from tool_results import RESULT_GUIDANCE
            prompt += '\n\n工具结果读取规则：' + RESULT_GUIDANCE
        if profile_text:
            prompt = f"[用户画像] {profile_text}\n\n{prompt}"
        if prepared_context.archive_context:
            prompt = f"{prompt}\n\n{prepared_context.archive_context}"
        session_recall = metadata.get("session_recall", "")
        if isinstance(session_recall, str) and session_recall.strip():
            prompt = f"{prompt}\n\n{session_recall.strip()}"
        reference_context = metadata.get("reference_context", "")
        if isinstance(reference_context, str) and reference_context.strip():
            # Session reference state is small, explicit and refreshed before
            # every user turn.  Unlike a lossy conversation summary it remains
            # valid after old message checkpoints are compacted.
            prompt = f"{prompt}\n\n{reference_context.strip()}"
        messages.insert(0, {"role": "system", "content": prompt})

        verify_fb = state.get("metadata", {}).get("verify_feedback", "")
        if verify_fb:
            # Replace the visible draft only with the final repaired answer.
            buffer_stream_output = True
            messages.append({"role": "system", "content": verify_fb})

        if memory_store:
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
            already_called_forced_tool = any(
                tool_call.get("function", {}).get("name") == forced_tool
                for message in messages
                if message.get("role") == "assistant"
                for tool_call in (message.get("tool_calls") or [])
            )
            payload["tool_choice"] = (
                {"type": "function", "function": {"name": forced_tool}}
                if forced_tool and not already_called_forced_tool
                else "auto"
            )

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
        request_failure_emitted = False

        def emit_request_failed(error) -> None:
            nonlocal request_failure_emitted
            if request_failure_emitted:
                return
            request_failure_emitted = True
            emit(
                "llm_request_failed", error_type=type(error).__name__,
                error_details=getattr(error, "error_details", {}),
                duration_ms=round((time.perf_counter() - request_started) * 1000, 1),
            )

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
            emit_request_failed(exc)
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
            reasoning_content = ""
            tool_calls_acc = {}
            finish_reason = None
            attempt_usage = None
            saved_partial = ""
            stream_failed = False

            def _consume_stream(response) -> bool:
                # ``+=`` rebinds both strings in this nested function.  They
                # must therefore be declared nonlocal; otherwise a stream
                # containing DeepSeek's first reasoning delta fails before
                # any visible answer is emitted.
                nonlocal full_text, reasoning_content, finish_reason, attempt_usage
                lines = iter(response.iter_lines(chunk_size=1, decode_unicode=True))
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
                        # EOF without a successful terminal choice is incomplete.
                        return finish_reason in {"stop", "tool_calls"}
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        return True
                    try:
                        chunk = _json.loads(data_str)
                    except (ValueError, TypeError):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR") from None
                    if not isinstance(chunk, dict) or chunk.get("error"):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                    # A final usage-only frame can have an empty choices list.
                    choices = chunk.get("choices") or [{}]
                    if not isinstance(choices, list) or not isinstance(choices[0], dict):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                    if choice.get("finish_reason") is not None and not isinstance(choice["finish_reason"], str):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                    finish_reason = choice.get("finish_reason") or finish_reason
                    if isinstance(chunk.get("usage"), dict):
                        attempt_usage = chunk["usage"]
                    token = delta.get("content", "")
                    if token:
                        if not isinstance(token, str):
                            raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                        full_text += token
                        if not buffer_stream_output:
                            stream_callback(token)
                    reasoning_delta = delta.get("reasoning_content", "")
                    if reasoning_delta:
                        if not isinstance(reasoning_delta, str):
                            raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                        reasoning_content += reasoning_delta
                    tool_deltas = delta.get("tool_calls") or []
                    if not isinstance(tool_deltas, list):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                    for tc in tool_deltas:
                        if not isinstance(tc, dict):
                            raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                        idx = tc.get("index", 0)
                        function = tc.get("function") or {}
                        if (not isinstance(idx, int) or not isinstance(function, dict)
                                or (tc.get("id") is not None and not isinstance(tc["id"], str))
                                or any(function.get(key) is not None and not isinstance(function[key], str)
                                       for key in ("name", "arguments"))):
                            raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": tc.get("id", ""), "function": {"name": "", "arguments": ""}}
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        if function.get("name"):
                            tool_calls_acc[idx]["function"]["name"] += function["name"]
                        if function.get("arguments"):
                            tool_calls_acc[idx]["function"]["arguments"] += function["arguments"]
                    if finish_reason not in {None, "stop", "tool_calls"}:
                        code = {"length": "LLM_OUTPUT_TRUNCATED", "content_filter": "LLM_CONTENT_FILTERED"}.get(finish_reason, "LLM_STREAM_INTERRUPTED")
                        raise LLMStreamError(code, retryable=finish_reason != "content_filter")

            def record_attempt_usage():
                if token_usage is not None and attempt_usage is not None:
                    u = attempt_usage
                    for k in ("prompt", "completion", "total"):
                        token_usage[k] = token_usage.get(k, 0) + u.get(f"{k}_tokens", 0)
                    token_usage["calls"] = token_usage.get("calls", 0) + 1
                    token_usage["last_prompt"] = u.get("prompt_tokens", 0)
                    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
                              or u.get("prompt_cache_hit_tokens", 0))
                    token_usage["cached"] = token_usage.get("cached", 0) + cached
                    missed = u.get("prompt_tokens", 0) - cached
                    cost = missed / 1e6 * 1 + cached / 1e6 * 0.02 + u.get("completion_tokens", 0) / 1e6 * 2
                    token_usage["cost"] = token_usage.get("cost", 0) + cost
                    token_usage["last_round_cost"] = cost

            def validate_tool_calls():
                if not full_text.strip() and not tool_calls_acc:
                    raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")
                for call in tool_calls_acc.values():
                    try:
                        arguments = _json.loads(call["function"]["arguments"])
                    except (ValueError, TypeError):
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR") from None
                    if not isinstance(arguments, dict) or not call["id"] or not call["function"]["name"]:
                        raise LLMStreamError("LLM_STREAM_PROTOCOL_ERROR")

            while True:
                stop_watch = watch_stream_cancellation(resp)
                try:
                    completed = _consume_stream(resp)
                    if not completed:
                        raise requests.exceptions.ChunkedEncodingError(
                            "stream ended before [DONE]"
                        )
                    validate_tool_calls()
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
                    # A transport close after a valid terminal choice cannot
                    # truncate its already-complete content. Still validate any
                    # tool arguments before allowing the graph to execute them.
                    if finish_reason in {"stop", "tool_calls"} and not isinstance(exc, LLMStreamError):
                        try:
                            validate_tool_calls()
                        except LLMStreamError as invalid:
                            exc = invalid
                        else:
                            llm_client.finish_stream(resp, success=True)
                            break
                    if llm_client:
                        # Snapshot HTTP status and the low-level read failure
                        # before closing/replaying a stream. A replay may fail
                        # at its deadline gate without making another request.
                        stream_error = (
                            llm_client.stream_error(request_budget, exc, resp)
                            if hasattr(llm_client, "stream_error") else
                            LLMRequestTimeoutError("模型流式响应中断；未切换模型，请稍后点击重试")
                        )
                        llm_client.finish_stream(resp, success=False)
                    else:
                        stream_error = LLMRequestTimeoutError("模型流式响应中断；未切换模型，请稍后点击重试")
                        try:
                            resp.close()
                        except Exception:
                            pass
                    record_attempt_usage()
                    attempt_usage = None
                    # Retry just this model invocation, retaining the existing
                    # tool results in payload. Never re-enter prior tool nodes.
                    if (
                        llm_client and getattr(exc, "retryable", True)
                        and getattr(request_budget, "remaining_seconds", lambda: 1)() > 0
                        and request_budget.reserve_retry()
                    ):
                        saved_partial = full_text or saved_partial
                        buffer_stream_output = buffer_stream_output or bool(full_text)
                        full_text, reasoning_content = "", ""
                        tool_calls_acc.clear()
                        finish_reason = None
                        _api_status("正在自动恢复模型输出，已有内容保留，完成后更新回复")
                        emit("llm_stream_recovery_started", error_details=getattr(stream_error, "error_details", {}),
                             **request_budget.metrics())
                        if isinstance(exc, LLMStreamError) and exc.code == "LLM_OUTPUT_TRUNCATED":
                            payload = {**payload, "messages": [*messages, {
                                "role": "system", "content": "请在输出预算内给出完整且简洁的回答，优先结论与必要证据，避免重复展开。",
                            }]}
                        retry_args = {
                            "stream": True,
                            "on_status": _api_status,
                            "budget": request_budget,
                        }
                        if cancel_event is not None:
                            retry_args["cancel_event"] = cancel_event
                        try:
                            resp = llm_client.post(payload, **retry_args)
                        except RequestCancelledError:
                            raise
                        except Exception as retry_error:
                            # Preserve the earlier draft even if replay fails
                            # before response headers (for example, HTTP 503).
                            stream_error = retry_error
                        else:
                            continue
                    full_text = full_text or saved_partial
                    tool_calls_acc.clear()
                    stream_failed = True
                    if not full_text:
                        emit_request_failed(stream_error)
                        raise stream_error from exc
                    emit_request_failed(stream_error)
                    interruption = "\n\n⚠️ 输出中断，自动恢复未能完成，已保留以上内容。可在运行详情中查看原因并重试。"
                    full_text += interruption
                    if not buffer_stream_output:
                        stream_callback(interruption)
                    if token_usage is not None:
                        token_usage["api_status"] = "⚠️ 流式输出自动恢复未完成"
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
                    record_attempt_usage()
                    stop_watch()
            assistant_msg = {"role": "assistant", "content": full_text or None}
            if reasoning_content:
                # Keep this in the durable graph state only.  It is never
                # streamed to the user, but DeepSeek requires it on every
                # subsequent request that carries the tools parameter.
                assistant_msg["reasoning_content"] = reasoning_content
            if tool_calls_acc:
                assistant_msg["tool_calls"] = [
                    {"id": v["id"], "type": "function", "function": v["function"]}
                    for v in sorted(tool_calls_acc.values(), key=lambda x: x.get("id", ""))
                ]
            if not request_failure_emitted:
                emit(
                    "llm_request_finished",
                    duration_ms=round((time.perf_counter() - request_started) * 1000, 1),
                    **request_budget.metrics(),
                )
            if stream_failed:
                metadata["stream_incomplete"] = True
            if verify_fb and not assistant_msg.get("tool_calls"):
                assistant_msg["_replace_answer"] = True
            return {"messages": [assistant_msg], "metadata": metadata}

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
        reasoning_content = msg.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if msg.get("tool_calls"):
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                for tc in msg["tool_calls"]
            ]
        elif verify_fb:
            assistant_msg["_replace_answer"] = True
        emit(
            "llm_request_finished",
            duration_ms=round((time.perf_counter() - request_started) * 1000, 1),
            **request_budget.metrics(),
        )
        return {"messages": [assistant_msg], "metadata": metadata}

    # ═══ 路由 ═══

    def router(state: AgentState) -> str:
        ensure_active("route")
        if state.get("metadata", {}).get("stream_incomplete"):
            return END
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
            if tool_rounds_in(state.get("messages", [])) >= max_tool_rounds:
                emit("tool_round_limit_reached", limit=max_tool_rounds)
                return END
        return "llm"

    # ═══ 工具节点 ═══

    def _warn_when_budget_is_nearly_spent(
        state_messages: list[dict], tool_msgs: list[dict],
    ) -> None:
        """Tell the model to wrap up instead of letting recursion kill the run.

        The hard limit is LangGraph's recursion_limit, which surfaces as
        GraphRecursionError and discards every intermediate result.  A warning
        one round early lets the model answer with what it already has.
        """
        if not tool_round_budget:
            return
        remaining = int(tool_round_budget) - tool_rounds_in(state_messages)
        if remaining > 1:
            return
        tool_msgs.append(budget_exhaustion_warning(max(remaining, 0)))

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
                name, args, paper_store=paper_store, vision_model=vision_model,
                profile_manager=profile_manager, memory_store=memory_store,
                llm_client=llm_client, model=model,
            )
            ensure_active("after_tool")
            tool_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        _warn_when_budget_is_nearly_spent(messages, tool_msgs)
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
        turn_start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        recent = list(messages[turn_start + 1:])[-8:]
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
            emit("verify_request_skipped", error_type="VerifyCircuitOpen")
            if token_usage:
                wait_seconds = verify_guard.remaining_seconds()
                token_usage["verify_status"] = (
                    f"⚠️ 验证暂时跳过（服务繁忙，约 {wait_seconds}s 后恢复）"
                )
            return _complete_verification()

        # ── 状态提示 ──
        retry_n = state.get("metadata", {}).get("verify_count", 0)
        verify_round = retry_n + 1
        if verify_round > MAX_VERIFY_PASSES:
            emit("verify_unresolved", verification_round=MAX_VERIFY_PASSES)
            return _complete_verification()
        if token_usage:
            token_usage["verify_status"] = f"🔄 第{retry_n+1}次修改..." if retry_n else "🔍 验证中..."

        tool_texts = []
        has_read_pdf = False
        for m in recent:
            if m.get("role") == "tool":
                t = m.get("content", "")
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

        # Keep full ordinary answers and explicitly mark any bounded excerpts.
        prev_issues = state.get("metadata", {}).get("verify_issues", "")
        verify_prompt = verification_prompt(user_q, content, fact_texts + ([library_context] if library_context else []), prev_issues)

        verify_policy = RequestPolicy(
            purpose="verify",
            priority=RequestPriority.VERIFY,
            deadline_seconds=verify_timeout_seconds,
            read_timeout_seconds=verify_timeout_seconds,
            max_retries=1,
            counts_toward_circuit=False,
        )

        def _verify_status(message: str) -> None:
            if token_usage:
                token_usage["verify_status"] = f"🔎 验证中：{message}"
            emit("verify_status", status=message)

        verify_started_at = time.perf_counter()
        def verify_elapsed_ms():
            return round((time.perf_counter() - verify_started_at) * 1000, 1)

        try:
            verify_budget = llm_client.new_request_budget(
                verify_policy,
            )
            emit("verify_request_started", model=model, verification_round=verify_round, **verify_budget.metrics())
            resp = llm_client.post(
                {"model": model, "messages": [{"role": "user", "content": verify_prompt}],
                 "stream": False, "temperature": 0.1},
                stream=False,
                on_status=_verify_status,
                budget=verify_budget,
                cancel_event=cancel_event,
            )
            choice = resp.json()["choices"][0]
            result = str(choice.get("message", {}).get("content") or "").strip()
            if choice.get("finish_reason") in {"length", "content_filter"}:
                result = ""  # Incomplete review output must not be treated as a pass.
            emit("verify_request_finished", verification_round=verify_round, duration_ms=verify_elapsed_ms(), **verify_budget.metrics())
        except RequestCancelledError:
            emit("verify_request_cancelled", duration_ms=verify_elapsed_ms())
            raise
        except (LLMQueueFullError, LLMCircuitOpenError) as e:
            # Queue pressure and the shared model circuit do not mean that the
            # verify feature itself is unhealthy. Skip this optional stage
            # without opening its local feature guard.
            if token_usage:
                token_usage["verify_status"] = "⚠️ 验证跳过（模型繁忙或暂不可用）"
            emit("verify_request_skipped", error_type=type(e).__name__, duration_ms=verify_elapsed_ms(),
                 error_details=safe_error_details(getattr(e, "error_details", None)))
            return _complete_verification()
        except Exception as e:
            # ── 快速降级：失败两次后熔断，后续请求直接返回主回答 ──
            if verify_guard:
                verify_guard.record_failure()
            if token_usage:
                token_usage["verify_status"] = "⚠️ 验证跳过（服务超时或不可用）"
            emit("verify_request_failed", error_type=type(e).__name__, duration_ms=verify_elapsed_ms(),
                 error_details=safe_error_details(getattr(e, "error_details", None)))
            print(f"      ⚠ verify 跳过: {type(e).__name__}: {e}", file=sys.stderr)
            return _complete_verification()
        if verify_guard:
            verify_guard.record_success()
        if "usage" in resp.json() and token_usage:
            vu = resp.json()["usage"]
            for k in ("prompt", "completion", "total"):
                token_usage[k] += vu.get(f"{k}_tokens", 0)
            token_usage["calls"] += 1

        decision = classify_review(result)
        if decision.result == "passed" and len(content) > 24000:
            emit("verify_inconclusive", verification_round=verify_round)
            update = _complete_verification()
            update["messages"] = [{"role": "assistant", "_replace_answer": True, "content": content + "\n\n⚠️ 本次只核对了部分内容，其余内容仍需核对。"}]
            return update
        if decision.result == "passed":
            emit("verify_passed", verification_round=verify_round)
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

        if decision.result == "inconclusive":
            emit("verify_inconclusive", verification_round=verify_round)
            update = _complete_verification()
            update["messages"] = [{"role": "assistant", "_replace_answer": True, "content": content + "\n\n⚠️ 自动核验未返回可用结论，本轮回复仍需核对。"}]
            return update

        if decision.result == "advisory":
            emit("verify_advisory", verification_round=verify_round)
            update = _complete_verification()
            # Advisory review is an internal quality signal, not a new user request.
            return update

        if verify_round >= MAX_VERIFY_PASSES:
            emit("verify_unresolved", verification_round=verify_round)
            update = _complete_verification()
            warning = "\n\n⚠️ 本次回答仍有事实争议，相关结论需要对照原文确认，暂不宜直接采用。"
            update["messages"] = [{"role": "assistant", "_replace_answer": True, "content": content + warning}]
            return update

        blocking = "\n".join(decision.blocking)
        feedback = (f"[系统验证] 仅针对以下实质性事实问题做一次定向修正：\n{blocking}\n\n"
                    "核验意见是待检查的判断，不应盲从；对照原文或计算确认后再修正，证据不足则明确保留不确定性。"
                    "保留与这些问题无关的正确内容，不因措辞偏好扩展研究范围。"
                    "这是内部质量检查，不是用户的新消息。完成必要的原文核对后，输出面向用户当前请求的完整最终答复，替换整份初稿；"
                    "将修正融入正文，保留本轮操作结果和有效内容，不要只输出勘误或修正说明。"
                    "不要对核验器说话，不要叙述采纳意见的过程，也不要回应上一轮核验提示。")
        emit("verify_revision_requested", verification_round=verify_round)
        return {"metadata": {**metadata,
            "verify_feedback": feedback,
            "verify_count": verify_round,
            "verify_issues": blocking,
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

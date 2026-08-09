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
import threading
import time
from dataclasses import replace
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
from cancellation import RequestCancelledError, raise_if_cancelled
from graph_builder import build_graph
from llm_client import LLMClient, LLMClientError
from profile import ProfileManager
from research_orchestrator import ResearchOrchestrator
from resilience import CircuitBreaker
from search_api import list_downloaded_papers
from session_store import SessionStore


# Keep a normal chat turn bounded even when the model repeatedly requests tools.
# A graph "round" contains several internal nodes, so this still leaves room for
# multiple search/read calls while preventing an accidental unbounded cost loop.
MAX_AGENT_GRAPH_STEPS = 16


_DIRECT_ENGINEERING_MARKERS = (
    "系统应如何", "系统应该如何", "为什么仍应", "为何仍应", "为什么要",
    "超时", "熔断", "降级", "并发槽", "优先级", "取消", "会话隔离",
    "会话删除", "配置格式", "安全边界", "绝不能记录", "至少应报告", "实验设置",
)
_EXPLICIT_EVIDENCE_MARKERS = (
    "论文", "文献", "引用", "原文", "最新进展", "最新研究", "搜索论文",
    "检索论文", "找论文", "doi", "arxiv", "paper", "作者", "哪篇",
)


def should_answer_without_tools(user_input: str) -> bool:
    """Keep explanatory engineering turns out of the expensive research loop.

    This is intentionally narrow: explicit requests for papers or citations
    still receive the full retrieval tool set.  The rule prevents a generic
    design question from downloading unrelated papers merely to manufacture
    references before a useful answer can be streamed.
    """
    text = str(user_input or "").casefold()
    if not text or not any(marker in text for marker in _DIRECT_ENGINEERING_MARKERS):
        return False
    return not any(marker in text for marker in _EXPLICIT_EVIDENCE_MARKERS)


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

    def __init__(self, cfg=None):
        if cfg is None:
            from config import Config
            cfg = Config.load()
        self.cfg = cfg
        self.model = cfg.model
        self.api_key = cfg.deepseek_key
        self.enable_rag = cfg.rag_enabled
        self._paper_store = None

        if not self.api_key:
            raise ValueError(
                "❌ 未找到 DEEPSEEK_API_KEY！\n"
                "   请在 .env 中设置 DEEPSEEK_API_KEY=sk-****"
            )

        # 用户画像
        self.profile = ProfileManager(cfg.profile_path)
        print(f"      👤 用户画像: {self.profile.summary() or '待完善'}", file=sys.stderr)

        # 每个会话各自保存 Token 统计；请求内新建图实例，避免流式回调跨会话串线。
        self._usage_cache: dict[str, dict] = {}
        self._retry_inputs: dict[str, dict] = {}
        self._last_traces: dict[str, dict] = {}
        self.runtime_status: dict[str, object] = {}
        self.verify_guard = CircuitBreaker(failure_threshold=2, recovery_seconds=120)
        self.llm_client = LLMClient(
            api_key=self.api_key,
            api_url=cfg.api_url,
            max_concurrency=cfg.api_max_concurrency,
            interactive_reserved_slots=cfg.api_interactive_reserved_slots,
            queue_size=cfg.api_queue_size,
            connect_timeout_seconds=cfg.api_connect_timeout_seconds,
            read_timeout_seconds=cfg.api_read_timeout_seconds,
            request_deadline_seconds=cfg.api_request_deadline_seconds,
            max_retries=cfg.api_max_retries,
            circuit_failure_threshold=cfg.api_circuit_failure_threshold,
            circuit_recovery_seconds=cfg.api_circuit_recovery_seconds,
        )
        self._session_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

        # 记忆模块
        from memory import MemoryStore
        self.memory = MemoryStore(cfg.memory_db)

        # RAG 论文库
        if cfg.rag_enabled:
            try:
                from paper_store import PaperStore
                print("      📚 初始化论文向量库...", file=sys.stderr, flush=True)
                self._paper_store = PaperStore(persist_dir=cfg.chroma_dir)
                print(
                    f"      ✅ 已加载 {self._paper_store.paper_count} 篇论文, "
                    f"{self._paper_store.chunk_count} 个块",
                    file=sys.stderr,
                )
            except Exception as e:
                from paper_store import NoOpStore
                self._paper_store = NoOpStore()
                print(f"      ⚠️ RAG 降级运行（{e}）", file=sys.stderr)

        # 会话目录与 LangGraph checkpoint 共用同一个 SQLite 文件，便于原子删除。
        self.sessions = SessionStore(cfg.checkpoint_db)
        if self.sessions.ensure_legacy_session():
            print("      💾 已迁移旧对话为「历史会话」", file=sys.stderr)
        cleaned_checkpoints = self.sessions.cleanup_orphaned_checkpoints()
        cleaned_rows = sum(cleaned_checkpoints.values())
        if cleaned_rows:
            print(
                f"      🧹 已清理 {cleaned_rows} 条无归属的已删除会话记录",
                file=sys.stderr,
            )
        existing_sessions = self.sessions.list()
        if existing_sessions:
            self._thread_id = existing_sessions[0]["thread_id"]
        else:
            self._thread_id = self.sessions.create()["thread_id"]

        checkpoint_path = Path(cfg.checkpoint_db)
        if checkpoint_path.exists():
            size = checkpoint_path.stat().st_size
            print(f"      💾 对话历史: {checkpoint_path.name} ({size / 1024:.0f} KB)", file=sys.stderr)
            self._load_context_from_checkpoint(self._thread_id)
            self._prune_checkpoint(max_snapshots=50)

    # ── 公开 API ──

    def step(
        self,
        user_input: str,
        context: str | None = None,
        on_token=None,
        session_id: str | None = None,
        topic: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """单轮推理：输入用户消息，返回 Agent 回复文本。context 可选注入话题/笔记上下文。
           ``session_id`` 省略时使用当前 CLI 会话；Web 请求必须显式传入。"""
        try:
            raise_if_cancelled(cancel_event, "请求已取消")
        except RequestCancelledError:
            return "⏹️ 请求已取消。"
        thread_id = session_id or self._thread_id
        state = {"messages": [], "metadata": {"session_id": thread_id}}
        if topic:
            state["metadata"]["topic"] = topic
        if context:
            state["messages"].append({"role": "system", "content": context})
        state["messages"].append({"role": "user", "content": user_input})

        # 同一会话的对话和删除互斥；不同会话仍可并行执行。等待锁时也响应取消。
        session_lock = self._lock_for(thread_id)
        while not session_lock.acquire(timeout=0.2):
            try:
                raise_if_cancelled(cancel_event, "等待会话操作时已取消")
            except RequestCancelledError:
                return "⏹️ 请求已取消。"
        try:
            if not self.sessions.get(thread_id):
                return "⚠️ 当前会话不存在或已被删除，请新建一个会话。"
            self._retry_inputs[thread_id] = {
                "user_input": user_input, "context": context, "topic": topic,
            }
            usage = self.get_usage(thread_id)
            usage_before = dict(usage)
            started_at = time.perf_counter()
            chat_run = self.sessions.create_chat_run(thread_id, self.model)
            chat_run_id = str(chat_run["run_id"])
            self.sessions.add_run_event(
                thread_id, chat_run_id, "chat", "single_agent", "queue", "running",
                summary="请求已进入交互队列",
            )
            trace_events: list[dict] = []
            first_token_ms: float | None = None
            outcome = "error"
            answer = ""

            def _record_event(event: dict) -> None:
                at_ms = round((time.perf_counter() - started_at) * 1000, 1)
                trace_events.append({
                    **event,
                    "at_ms": at_ms,
                })
                self._persist_chat_event(thread_id, chat_run_id, event, at_ms)

            def _on_token(token: str) -> None:
                nonlocal first_token_ms
                if cancel_event is not None and cancel_event.is_set():
                    return
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started_at) * 1000, 1)
                    _record_event({"type": "first_token"})
                if on_token:
                    on_token(token)

            allowed_tool_names = set() if should_answer_without_tools(user_input) else None
            if allowed_tool_names is not None:
                _record_event({"type": "tool_policy", "policy": "direct_engineering_answer"})
            app = None
            try:
                if allowed_tool_names is None:
                    # Preserve the long-standing default call shape so custom
                    # integrations and lightweight test doubles do not need
                    # to accept a no-op policy argument.
                    app = self._build_app(
                        usage, _on_token if on_token else None, _record_event,
                        cancel_event=cancel_event,
                    )
                else:
                    app = self._build_app(
                        usage, _on_token if on_token else None, _record_event,
                        cancel_event=cancel_event, allowed_tool_names=allowed_tool_names,
                    )
                result = app.invoke(
                    state,
                    config={
                        "configurable": {"thread_id": thread_id},
                        "recursion_limit": MAX_AGENT_GRAPH_STEPS,
                    },
                )
                messages = result.get("messages", [])
                if not messages:
                    answer = "⚠️ Agent 未返回任何消息。"
                    outcome = "empty_response"
                    return answer
                last = messages[-1]
                answer = last.get("content", "") or ""
                outcome = "success"
                return answer
            except RequestCancelledError:
                answer = "⏹️ 请求已取消。"
                outcome = "cancelled"
                _record_event({"type": "request_cancelled"})
                return answer
            except LLMClientError as e:
                answer = f"⚠️ {e}"
                outcome = "llm_error"
                return answer
            except requests.RequestException as e:
                answer = f"❌ 网络请求失败: {e}\n   请检查网络和 API Key。"
                outcome = "network_error"
                return answer
            except Exception as e:
                if type(e).__name__ == "GraphRecursionError":
                    answer = (
                        "⚠️ 本轮 Agent 已达到工具调用上限，已停止继续执行。"
                        "请缩小问题范围后重试。"
                    )
                    outcome = "agent_limit"
                    return answer
                answer = f"❌ Agent 错误: {type(e).__name__}: {e}"
                outcome = "agent_error"
                return answer
            finally:
                # Status-bar text describes only the live request.  Completed
                # interruptions and optional verification failures are visible
                # in the answer/run timeline, not as stale session-wide alerts.
                usage.pop("api_status", None)
                usage.pop("verify_status", None)
                self.sessions.touch(thread_id, user_input)
                self.sessions.save_usage(thread_id, usage)
                self._close_app(app)
                tool_events = [event for event in trace_events if event.get("type") == "tool_finished"]
                duration_ms = round((time.perf_counter() - started_at) * 1000, 1)
                run_status = {
                    "success": "completed",
                    "cancelled": "cancelled",
                }.get(outcome, "failed")
                self.sessions.update_chat_run(
                    chat_run_id,
                    status=run_status,
                    duration_ms=duration_ms,
                    metrics={
                        "model_calls": usage.get("calls", 0) - usage_before.get("calls", 0),
                        "tool_count": len(tool_events),
                        "event_count": len(trace_events),
                    },
                    error_type="" if run_status == "completed" else outcome,
                )
                terminal_summaries = {
                    "completed": "本轮对话已完成",
                    "cancelled": "本轮对话已取消",
                    "failed": "本轮对话未完成，可重试",
                }
                self.sessions.add_run_event(
                    thread_id, chat_run_id, "chat", "single_agent", "run", run_status,
                    summary=terminal_summaries[run_status],
                    metrics={"duration_ms": duration_ms},
                    error_type="" if run_status == "completed" else outcome,
                )
                self._last_traces[thread_id] = {
                    "session_id": thread_id,
                    "model": self.model,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "first_token_ms": first_token_ms,
                    "tool_trace": tool_events,
                    "events": trace_events,
                    "usage_delta": {
                        key: usage.get(key, 0) - usage_before.get(key, 0)
                        for key in ("prompt", "completion", "total", "calls")
                    },
                    "answer_chars": len(answer),
                }
        finally:
            session_lock.release()

    def research(
        self,
        query: str = "",
        *,
        scope: str = "both",
        context: str | None = None,
        session_id: str | None = None,
        resume: bool = False,
        cancel_event: threading.Event | None = None,
        on_progress=None,
    ) -> str:
        """Run the bounded multi-agent research loop for one explicit session."""
        thread_id = session_id or self._thread_id
        session_lock = self._lock_for(thread_id)
        while not session_lock.acquire(timeout=0.2):
            try:
                raise_if_cancelled(cancel_event, "等待会话操作时已取消")
            except RequestCancelledError:
                return "⚠️ 深度研究已取消。"
        try:
            if not self.sessions.get(thread_id):
                return "⚠️ 当前会话不存在或已被删除，请新建一个会话。"
            usage = self.get_usage(thread_id)
            usage_before = dict(usage)
            started_at = time.perf_counter()
            events: list[dict] = []

            def _record_progress(event: dict) -> None:
                events.append({
                    **event,
                    "at_ms": round((time.perf_counter() - started_at) * 1000, 1),
                })
                if on_progress:
                    on_progress(event)

            orchestrator = ResearchOrchestrator(
                session_store=self.sessions,
                api_key=self.api_key,
                model=self.model,
                paper_store=self._paper_store,
                glm_api_key=self.cfg.glm_key,
                llm_client=self.llm_client,
                verify_timeout_seconds=self.cfg.verify_timeout_seconds,
            )
            result = orchestrator.run(
                query,
                thread_id=thread_id,
                scope=scope,
                context=context,
                resume=resume,
                cancel_event=cancel_event,
                on_progress=_record_progress,
            )
            for key in ("prompt", "completion", "total", "calls"):
                usage[key] = usage.get(key, 0) + result.usage.get(key, 0)
            usage["last_round_cost"] = 0
            visible_query = "继续上次深度研究" if resume else query
            self.sessions.touch(thread_id, visible_query)
            self.sessions.save_usage(thread_id, usage)
            self._append_research_turn(
                thread_id,
                visible_query,
                result.answer,
            )
            self._last_traces[thread_id] = {
                "session_id": thread_id,
                "model": self.model,
                "outcome": result.status,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
                "events": events,
                "research_run_id": result.run_id,
                "usage_delta": {
                    key: usage.get(key, 0) - usage_before.get(key, 0)
                    for key in ("prompt", "completion", "total", "calls")
                },
                "answer_chars": len(result.answer),
            }
            return result.answer
        except RequestCancelledError:
            return "⚠️ 深度研究已取消。"
        except ValueError as exc:
            return f"⚠️ {exc}"
        except Exception as exc:
            return f"❌ 深度研究错误: {type(exc).__name__}: {exc}"
        finally:
            session_lock.release()

    def _append_research_turn(self, thread_id: str, query: str, answer: str) -> None:
        """Persist only the user-visible research turn, never worker messages."""
        app = self._build_app(self.get_usage(thread_id))
        config = {"configurable": {"thread_id": thread_id}}
        try:
            app.update_state(
                config,
                {
                    "messages": [
                        {"role": "user", "content": query},
                        {"role": "assistant", "content": answer},
                    ],
                    "metadata": {"session_id": thread_id},
                },
            )
        finally:
            self._close_app(app)

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

    def _load_context_from_checkpoint(self, thread_id: str):
        """读取一个会话已有消息，估算它自己的上下文占比。"""
        usage = self.get_usage(thread_id)
        app = self._build_app(usage)
        try:
            state = app.get_state({"configurable": {"thread_id": thread_id}})
            messages = state.values.get("messages", []) if state.values else []
        except Exception:
            messages = []
        finally:
            self._close_app(app)
        if not messages:
            return
        # 清理不完整的 tool_calls（assistant 有 tool_calls 但无对应 tool 结果）
        valid_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                m["tool_calls"] = [tc for tc in m["tool_calls"] if tc["id"] in valid_ids]
                if not m["tool_calls"]:
                    del m["tool_calls"]
        # 估算 tokens：总字符数 / 2（中英混合近似）
        total_chars = sum(len(m.get("content", "") or "") for m in messages)
        est_tokens = total_chars // 3  # 中英混排约 3 字符/token
        context_limit = 131072
        if "reasoner" in self.model:
            context_limit = 65536
        elif "flash" in self.model:
            context_limit = 1000000  # deepseek-v4-flash 官方 1M
        usage["prompt"] = est_tokens
        usage["total"] = est_tokens
        usage["last_prompt"] = est_tokens
        usage["context_limit"] = context_limit
        self.sessions.save_usage(thread_id, usage)
        pct = min(int(est_tokens / context_limit * 100), 99)
        print(f"      📊 已有上下文: ~{est_tokens:,} tokens ({pct}%)", file=sys.stderr)
        # 不自动新建会话：保留历史可见，由状态栏提示用户手动新建。
        if pct > 80:
            usage["budget_warning"] = "⚠️ 上下文 80%+，建议新建会话"
            self.sessions.save_usage(thread_id, usage)

    def _prune_checkpoint(self, max_snapshots: int = 50):
        """剪裁 checkpoint——只保留最近 N 个快照（每个 thread）"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.cfg.checkpoint_db)
            # 获取所有 thread_id
            threads = conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ).fetchall()
            pruned = 0
            for (tid,) in threads:
                rows = conn.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_id DESC",
                    (tid,),
                ).fetchall()
                if len(rows) > max_snapshots:
                    oldest = rows[max_snapshots:][0][0]
                    conn.execute(
                        "DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_id <= ?",
                        (tid, oldest),
                    )
                    pruned += len(rows) - max_snapshots
            conn.commit()
            conn.close()
            if pruned:
                print(f"      🧹 checkpoint 剪裁: 清理 {pruned} 个旧快照", file=sys.stderr)
        except Exception:
            pass  # 剪裁失败不影响正常使用

    def reset(self):
        """创建并切换到新会话（兼容 CLI 的 /new 命令）。"""
        self._thread_id = self.create_session()["thread_id"]
        print(f"\n🔄 已开始新对话 (thread: {self._thread_id})。", file=sys.stderr)
        return self._thread_id

    def create_session(self, title: str | None = None) -> dict:
        session = self.sessions.create(title)
        self._usage_cache[session["thread_id"]] = self._new_usage()
        return session

    def list_sessions(self) -> list[dict]:
        return self.sessions.list()

    def select_session(self, thread_id: str) -> dict:
        session = self.sessions.get(thread_id)
        if not session:
            raise KeyError(f"会话不存在: {thread_id}")
        self._thread_id = thread_id
        self.get_usage(thread_id)
        return session

    def delete_session(self, thread_id: str) -> bool:
        """硬删除会话、checkpoint 和会话摘要；调用方需先完成二次确认。"""
        with self._lock_for(thread_id):
            deleted = self.sessions.delete(thread_id)
            if not deleted:
                return False
            self.memory.delete_summaries(thread_id)
            self._usage_cache.pop(thread_id, None)
            self._retry_inputs.pop(thread_id, None)
            if self._thread_id == thread_id:
                remaining = self.sessions.list()
                self._thread_id = remaining[0]["thread_id"] if remaining else self.create_session()["thread_id"]
            return True

    def get_usage(self, thread_id: str | None = None) -> dict:
        thread_id = thread_id or self._thread_id
        if thread_id not in self._usage_cache:
            stored = self.sessions.get_usage(thread_id)
            # ``api_status`` and ``verify_status`` describe in-flight work only.
            # Older versions persisted them, so discard any stale value on first
            # loading a session after restart.
            transient_removed = False
            for key in ("api_status", "verify_status"):
                if key in stored:
                    stored.pop(key, None)
                    transient_removed = True
            if transient_removed:
                self.sessions.save_usage(thread_id, stored)
            usage = self._new_usage()
            usage.update(stored)
            self._usage_cache[thread_id] = usage
        return self._usage_cache[thread_id]

    def get_retry_input(self, thread_id: str | None = None) -> dict | None:
        """返回本进程内最后一个模型请求，用于 UI 的 `/retry`。"""
        return self._retry_inputs.get(thread_id or self._thread_id)

    def get_last_trace(self, thread_id: str | None = None) -> dict | None:
        """返回最近一轮的脱敏链路追踪，供评测和诊断使用。"""
        trace = self._last_traces.get(thread_id or self._thread_id)
        return json.loads(json.dumps(trace, ensure_ascii=False)) if trace else None

    @property
    def token_usage(self) -> dict:
        """兼容 CLI 旧调用；Web UI 请用 ``get_usage(session_id)``。"""
        return self.get_usage(self._thread_id)

    def get_history(self, thread_id: str) -> list[dict]:
        """返回可直接交给 Gradio Chatbot 的用户/助手消息。"""
        with self._lock_for(thread_id):
            if not self.sessions.get(thread_id):
                return []
            app = self._build_app(self.get_usage(thread_id))
            try:
                state = app.get_state({"configurable": {"thread_id": thread_id}})
                messages = state.values.get("messages", []) if state.values else []
            except Exception:
                messages = []
            finally:
                self._close_app(app)
        return [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in messages
            if m.get("role") in {"user", "assistant"} and m.get("content")
        ]

    @property
    def paper_store(self):
        """PaperStore 实例（可能为 None）"""
        return self._paper_store

    # ── 内部 ──

    def _new_usage(self) -> dict:
        limit = 131072
        if "reasoner" in self.model:
            limit = 65536
        elif "flash" in self.model:
            limit = 1000000
        return {
            "prompt": 0, "completion": 0, "total": 0, "calls": 0,
            "cached": 0, "cost": 0, "last_round_cost": 0,
            "last_prompt": 0, "context_limit": limit,
        }

    def _persist_chat_event(
        self,
        thread_id: str,
        run_id: str,
        event: dict,
        at_ms: float,
    ) -> None:
        """Project graph traces onto an intentionally small, non-content event schema."""
        event_type = str(event.get("type") or "")
        metrics = {"at_ms": at_ms}
        if isinstance(event.get("duration_ms"), (int, float)):
            metrics["duration_ms"] = event["duration_ms"]

        event_map = {
            "llm_request_started": ("single_agent", "model", "running", "模型请求已开始"),
            "llm_response_headers": ("single_agent", "model", "running", "模型服务已响应"),
            "llm_request_finished": ("single_agent", "model", "completed", "模型请求已完成"),
            "llm_request_failed": ("single_agent", "model", "failed", "模型请求失败，可重试"),
            "llm_status": ("single_agent", "model", "waiting", "模型请求正在排队或重试"),
            "first_token": ("single_agent", "model", "streaming", "已收到首个输出片段"),
            "request_cancelled": ("single_agent", "run", "cancelled", "请求已取消"),
            "verify_request_started": ("verifier", "verify", "running", "事实核验已开始"),
            "verify_request_finished": ("verifier", "verify", "completed", "事实核验已完成"),
            "verify_request_cancelled": ("verifier", "verify", "cancelled", "事实核验已取消"),
            "verify_request_skipped": ("verifier", "verify", "skipped", "事实核验暂时跳过"),
            "verify_request_failed": ("verifier", "verify", "failed", "事实核验失败，已继续生成回复"),
            "verify_status": ("verifier", "verify", "waiting", "事实核验正在等待服务"),
        }
        if event_type in {"tool_started", "tool_finished", "tool_failed"}:
            tool_name = str(event.get("tool") or "")
            tool_labels = {
                "query_papers": "论文检索",
                "search_papers": "论文搜索",
                "read_pdf": "论文阅读",
                "describe_image": "图像分析",
                "memory_search": "记忆检索",
            }
            label = tool_labels.get(tool_name, "工具调用")
            status = {
                "tool_started": "running",
                "tool_finished": "completed",
                "tool_failed": "failed",
            }[event_type]
            message = {
                "tool_started": f"{label}已开始",
                "tool_finished": f"{label}已完成",
                "tool_failed": f"{label}失败，可重试",
            }[event_type]
            agent, stage, summary = "single_agent", "tool", message
        elif event_type in event_map:
            agent, stage, status, summary = event_map[event_type]
        else:
            return

        self.sessions.add_run_event(
            thread_id, run_id, "chat", agent, stage, status,
            summary=summary,
            metrics=metrics,
            error_type=str(event.get("error_type") or "")[:120],
        )

    def _build_app(
        self, usage: dict, on_token=None, event_callback=None, cancel_event=None,
        allowed_tool_names: set[str] | None = None,
    ):
        return build_graph(
            api_key=self.api_key,
            model=self.model,
            paper_store=self._paper_store,
            token_usage=usage,
            checkpoint_db=self.cfg.checkpoint_db,
            glm_api_key=self.cfg.glm_key,
            stream_callback=on_token,
            event_callback=event_callback,
            cancel_event=cancel_event,
            profile_manager=self.profile,
            memory_store=self.memory,
            verify_timeout_seconds=self.cfg.verify_timeout_seconds,
            verify_guard=self.verify_guard,
            llm_client=self.llm_client,
            allowed_tool_names=allowed_tool_names,
        )

    @staticmethod
    def _close_app(app) -> None:
        try:
            app.checkpointer.conn.close()
        except Exception:
            pass

    def _lock_for(self, thread_id: str) -> threading.RLock:
        with self._locks_guard:
            if thread_id not in self._session_locks:
                self._session_locks[thread_id] = threading.RLock()
            return self._session_locks[thread_id]

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
  runtime/primary/db/      对话、笔记、记忆和每日检索数据
"""
    print(msg)


def main():
    parser = argparse.ArgumentParser(description="科研助手 Agent")
    parser.add_argument("-m", "--model", default=None, help="模型名称")
    parser.add_argument("--no-rag", action="store_true", help="禁用 RAG")
    parser.add_argument("--debug", action="store_true", help="DEBUG 日志")
    parser.add_argument("--data-dir", default=None, help="运行时数据目录（默认: APP_DATA_DIR 或 runtime）")
    args = parser.parse_args()

    from config import Config
    from logger import setup_logging
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


if __name__ == "__main__":
    main()

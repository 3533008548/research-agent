"""
🌐 科研助手 Web UI — 极简聊天界面

基于 Gradio Blocks + 受控 Chatbot 状态，支持：
  - 斜杠命令: /model /tokens /indexed /new /help
  - ChromaDB RAG 论文检索
  - 公式实时 MathJax 渲染
  - 状态栏实时更新 + 工作区面板

启动:
  python web_ui.py
  python web_ui.py -m deepseek-v4-pro --port 8080
"""

import argparse
import html
import os
import re
import sys
import shutil
import hashlib
import json
import time
from pathlib import Path

# ── Windows GBK 兼容 ──
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
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
from badcase_store import BadcaseStore
from research_documents import ResearchDocumentStore
from run_timeline import RunTimelineService
from ui_request_guard import BrowserRunGuard


# Gradio does not enable inline delimiters unless they are supplied explicitly.
# Keep display delimiters before `$...$` so a block expression is never split
# into two malformed inline fragments.
CHAT_LATEX_DELIMITERS = [
    {"left": "$$", "right": "$$", "display": True},
    {"left": "\\[", "right": "\\]", "display": True},
    {"left": "$", "right": "$", "display": False},
    {"left": "\\(", "right": "\\)", "display": False},
]


# Gradio 6 applies these at launch time.  Keep them as module-level values so
# the ASGI entrypoint can pass the exact same presentation settings to
# ``mount_gradio_app``.
UI_THEME = gr.themes.Soft()
UI_CSS = """
/* A quiet workbench: conversation in the centre, context on the left, run
   inspection on the right.  Avoid broad colour overrides so Gradio keeps the
   established primary/stop button semantics. */
:root { color-scheme: light; }
.gradio-container,
.gradio-container button,
.gradio-container input,
.gradio-container textarea,
.gradio-container select,
.gradio-container .prose {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Noto Sans SC", Arial, sans-serif;
}
.gradio-container {
    max-width: none !important;
    min-height: 100vh;
    padding: 0 !important;
    background: #f7f8fb;
    color: #182230;
}
.gradio-container .main { max-width: 1560px !important; padding: 1rem 1.25rem 1.5rem !important; }
.workbench-header {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    padding: 0.5rem 0 1rem;
}
.workbench-mark {
    display: grid;
    width: 2.35rem;
    height: 2.35rem;
    place-items: center;
    border-radius: 0.8rem;
    background: #e7efff;
    font-size: 1.2rem;
}
.workbench-title { margin: 0; font-size: 1.06rem; font-weight: 680; letter-spacing: -0.01em; }
.workbench-subtitle { margin: 0.1rem 0 0; color: #687386; font-size: 0.78rem; }
.status-bar { align-self: center; margin-left: auto; padding: 0.3rem 0.65rem; font-size: 0.72rem; color: #687386; text-align: right; }
.status-bar p { margin: 0; }
footer { display: none !important; }
.quit-btn { align-self: center; min-width: 3.7rem !important; max-width: 4.5rem !important; }
.app-shell { gap: 1rem !important; align-items: flex-start !important; }
.workspace-sidebar, .workspace-inspector {
    gap: 0.75rem !important;
    border: 1px solid #e7eaf0;
    border-radius: 0.9rem;
    padding: 0.8rem !important;
    background: rgba(255, 255, 255, 0.88);
    box-shadow: 0 1px 2px rgba(24, 34, 48, 0.025);
}
.workspace-sidebar { order: -1; }
.workspace-main { min-width: 0 !important; gap: 0.75rem !important; }
.workspace-eyebrow { margin: 0.15rem 0 -0.25rem; color: #687386; font-size: 0.76rem; }
.workspace-heading { margin: 0; font-size: 1.05rem; font-weight: 650; }
.sidebar-label, .inspector-label { margin: 0.15rem 0 -0.2rem; font-size: 0.76rem; font-weight: 650; color: #49566b; }
.session-actions, .composer-actions, .research-actions { gap: 0.5rem !important; }
.session-actions button { min-width: 0 !important; }
.composer-shell {
    gap: 0.45rem !important;
    border: 1px solid #e0e5ee;
    border-radius: 1rem;
    padding: 0.55rem !important;
    background: #ffffff;
    box-shadow: 0 10px 24px rgba(24, 34, 48, 0.055);
}
.composer-shell textarea { min-height: 3rem !important; }
.research-tools, .utility-panel, .inspector-panel {
    border: 1px solid #e7eaf0 !important;
    border-radius: 0.7rem !important;
    background: #fff !important;
}
.research-tools { margin-top: 0.15rem; }
.utility-panel, .inspector-panel { margin: 0 !important; }
.utility-panel > button, .inspector-panel > button, .research-tools > button {
    padding: 0.65rem 0.7rem !important;
    font-size: 0.82rem !important;
}
.utility-panel > .wrap, .inspector-panel > .wrap, .research-tools > .wrap { padding-top: 0.1rem !important; }
#research-chat {
    height: clamp(360px, 54vh, 540px) !important;
    min-height: 360px !important;
    border: 1px solid #e3e8f1;
    border-radius: 1rem;
    background: #fff;
    overflow: hidden;
    box-shadow: 0 1px 2px rgba(24, 34, 48, 0.02);
}
#research-chat .wrap { padding: 0.3rem; }
.daily-panel { max-height: 15rem; overflow: auto; font-size: 0.82rem; }
.paper-list { max-height: 19rem; overflow: auto; }
.settings-stack { gap: 0.5rem !important; }
.agent-run-list { display: grid; gap: 0.55rem; }
.agent-run-card { border: 1px solid #e5e7eb; border-radius: 0.7rem; padding: 0.65rem 0.75rem; background: #fff; }
.agent-run-title { display: flex; align-items: center; gap: 0.5rem; }
.agent-run-meta { color: #6b7280; font-size: 0.75rem; margin-top: 0.2rem; }
.agent-run-badge { border-radius: 999px; padding: 0.08rem 0.45rem; font-size: 0.72rem; font-weight: 600; background: #e5e7eb; color: #374151; }
.status-running, .status-waiting, .status-streaming { background: #dbeafe; color: #1d4ed8; }
.status-completed { background: #dcfce7; color: #166534; }
.status-failed, .status-cancelled, .status-partial_failed { background: #fee2e2; color: #b91c1c; }
.status-partial, .status-skipped, .status-resumed { background: #fef3c7; color: #92400e; }
.agent-run-events { list-style: none; margin: 0.55rem 0 0; padding: 0; display: grid; gap: 0.35rem; }
.agent-run-event { display: grid; grid-template-columns: 0.55rem 1fr auto; gap: 0.4rem; align-items: center; font-size: 0.82rem; }
.agent-run-event small { color: #6b7280; font-size: 0.7rem; }
.agent-event-dot { display: inline-block; width: 0.45rem; height: 0.45rem; border-radius: 50%; background: #9ca3af; }
.agent-event-dot.status-running, .agent-event-dot.status-waiting, .agent-event-dot.status-streaming { background: #2563eb; }
.agent-event-dot.status-completed { background: #16a34a; }
.agent-event-dot.status-failed, .agent-event-dot.status-cancelled, .agent-event-dot.status-partial_failed { background: #dc2626; }
.agent-event-dot.status-partial, .agent-event-dot.status-skipped, .agent-event-dot.status-resumed { background: #d97706; }
.agent-run-hint, .agent-run-empty { color: #6b7280; font-size: 0.8rem; margin-top: 0.6rem; }
@media (max-width: 1120px) {
    .gradio-container .main { padding: 0.8rem !important; }
    .app-shell { flex-wrap: wrap !important; }
    .workspace-main { order: -2; flex-basis: 100% !important; }
    .workspace-sidebar, .workspace-inspector { order: initial; flex: 1 1 19rem !important; }
    .status-bar { display: none; }
}
@media (max-width: 720px) {
    .gradio-container .main { padding: 0.55rem !important; }
    .workspace-sidebar, .workspace-inspector { flex-basis: 100% !important; }
    .workbench-header { padding-bottom: 0.65rem; }
    #research-chat { height: 48vh !important; min-height: 360px !important; }
    .composer-actions, .research-actions { flex-wrap: wrap !important; }
}
"""
# The native MultimodalTextbox owns drag/drop.  Do not install a document-wide
# handler here: it would compete with Gradio's uploader and make the main
# composer feel less direct.
UI_JS = ""


def build_ui(*, cfg=None, agent=None, launch: bool = True):
    parser = argparse.ArgumentParser(description="科研助手 Web UI")
    parser.add_argument(
        "-m", "--model",
        default=None,
        help="模型名称",
    )
    parser.add_argument("--port", type=int, default=7860, help="端口号")
    parser.add_argument(
        "--host",
        default=os.getenv("UI_HOST", "127.0.0.1"),
        help="监听地址（Docker 中使用 0.0.0.0）",
    )
    parser.add_argument("--data-dir", default=None, help="运行时数据目录（默认: APP_DATA_DIR 或 runtime）")
    parser.add_argument("--no-rag", action="store_true", help="禁用 RAG")
    parser.add_argument("--debug", action="store_true", help="DEBUG 日志")
    args = parser.parse_args() if cfg is None else parser.parse_args([])

    from logger import setup_logging
    setup_logging(debug=args.debug)
    from config import Config

    cli = {
        "model": args.model,
        "rag_enabled": False if args.no_rag else None,
        "ui_debug": True if args.debug else None,
        "data_dir": args.data_dir,
    }
    cfg = cfg or Config.load({k: v for k, v in cli.items() if v is not None})

    print(f"🤖 模型: {cfg.model}")
    agent = agent or ResearchAgent(cfg=cfg)
    badcases = BadcaseStore(str(cfg.runtime_paths.badcases_db))
    research_documents = ResearchDocumentStore(cfg.runtime_paths)
    pending_conflicts = {}
    from scheduler import Scheduler
    scheduler = Scheduler(
        cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds,
    )
    timeline = RunTimelineService(agent.sessions, scheduler)
    import threading

    daily_lock = threading.Lock()
    daily_task = {"thread": None, "cancel_event": None, "api_run_id": None}
    browser_run_guard = BrowserRunGuard()
    # When mounted under FastAPI, Gradio is deliberately an HTTP client of the
    # same public run boundary.  Standalone ``python web_ui.py`` remains a
    # developer fallback and continues to use the in-process agent.
    api_run_client_enabled = bool(
        not launch
        and (
            os.getenv("REDIS_URL", "").strip()
            or os.getenv("API_RUN_CLIENT_ENABLED", "").strip().lower() in {"1", "true", "yes"}
        )
    )
    api_run_base = os.getenv("INTERNAL_API_URL", "http://127.0.0.1:7860").rstrip("/")
    api_run_token = os.getenv("API_AUTH_TOKEN", "").strip()
    _api_runs_by_browser: dict[str, str] = {}
    _api_runs_lock = threading.RLock()

    def _api_headers() -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_run_token:
            headers["X-API-Key"] = api_run_token
        return headers

    def _remember_api_run(browser_id: str, run_id: str) -> None:
        with _api_runs_lock:
            _api_runs_by_browser[browser_id] = run_id

    def _forget_api_run(browser_id: str, run_id: str | None = None) -> None:
        with _api_runs_lock:
            active = _api_runs_by_browser.get(browser_id)
            if active and (run_id is None or active == run_id):
                _api_runs_by_browser.pop(browser_id, None)

    def _browser_id(request: gr.Request | None) -> str:
        """取得 Gradio 浏览器标签页的稳定标识；仅用于请求失效控制。"""
        return getattr(request, "session_hash", None) or "anonymous-browser"

    def _invalidate_browser_request(request: gr.Request | None) -> None:
        browser_id = _browser_id(request)
        with _api_runs_lock:
            run_id = _api_runs_by_browser.get(browser_id)
        if run_id and api_run_client_enabled:
            try:
                import requests
                requests.post(
                    f"{api_run_base}/api/v1/runs/{run_id}/cancel",
                    headers=_api_headers(), timeout=(1, 2),
                )
            except Exception:
                # Browser invalidation still prevents stale output; the durable
                # worker's cancellation marker is a best-effort delivery here.
                pass
        browser_run_guard.invalidate(browser_id)

    def _format_daily_results(
        results: list[dict], heading: str, *, brief: str = "", critique: dict | None = None,
        run_id: str = "",
    ) -> str:
        if not results:
            suffix = f"\n\n运行：`{run_id}`" if run_id else ""
            return f"{heading}\n\n📭 未找到新论文。{suffix}"
        lines = [f"{heading}\n"]
        if brief:
            lines.append(f"> {brief}\n")
        for r in results[:5]:
            sources = ", ".join(r.get("sources") or [r.get("source", "")])
            year = r.get("year") or "年份未知"
            citations = r.get("citation_count")
            citation_text = f" · 引用 {citations}" if citations is not None else ""
            lines.append(f"- **{r['title'][:100]}**  `[{sources}]`")
            lines.append(f"  {year}{citation_text}")
            reason = (r.get("curation") or {}).get("reason", "")
            if reason:
                lines.append(f"  推荐理由：{reason}")
            tags = (r.get("curation") or {}).get("tags", [])
            if tags:
                lines.append(f"  标签：{' · '.join(tags)}")
            if r.get("url"):
                lines.append(f"  [查看论文]({r['url']})")
        if len(results) > 5:
            lines.append(f"\n...及另外 {len(results)-5} 篇")
        warnings = (critique or {}).get("warnings") or []
        if warnings:
            lines.append("\n**质量提示**：" + "；".join(warnings[:2]))
        if run_id:
            lines.append(f"\n运行：`{run_id}`。可用 `/daily resume` 继续未完成任务。")
        return "\n".join(lines)

    def _daily_search_bg(
        task_type: str, keyword: str | None = None, resume: bool = False,
        cancel_event: threading.Event | None = None,
    ):
        """长时检索只在后台线程运行，完成后写入独立任务面板而非聊天记录。"""
        worker = None
        try:
            from daily_orchestrator import DailyResearchOrchestrator
            worker = Scheduler(
                scheduler.db_path,
                request_timeout_seconds=cfg.daily_request_timeout_seconds,
            )
            orchestrator = DailyResearchOrchestrator(
                scheduler=worker,
                llm_client=agent.llm_client,
                model=agent.model,
                request_timeout_seconds=cfg.daily_request_timeout_seconds,
                max_keyword_concurrency=cfg.daily_keyword_concurrency,
                max_results_per_keyword=cfg.daily_max_results_per_keyword,
                daily_sources=cfg.daily_sources,
                openalex_api_key=cfg.openalex_api_key,
                ieee_api_key=cfg.ieee_api_key,
            )

            def _daily_progress(event: dict) -> None:
                agent.runtime_status["daily_progress"] = (
                    f"📰 {event.get('stage', 'daily')}：{event.get('message', '处理中')}"
                )

            kind = "search" if task_type == "search" else task_type
            result = orchestrator.run(
                kind,
                keyword=keyword,
                paper_store=agent.paper_store,
                resume=resume,
                cancel_event=cancel_event,
                on_progress=_daily_progress,
            )
            heading = {
                "retry": "**📰 每日检索结果（已重试）**",
                "search": f"**🔍 临时检索结果 — {keyword}**",
            }.get(kind, "**📰 每日论文速递**")
            agent.runtime_status["daily_progress"] = (
                f"📰 {result.status} · {len(result.candidates)} 篇推荐"
            )
            agent.runtime_status["daily_message"] = _format_daily_results(
                result.candidates, heading, brief=result.brief,
                critique=result.critique, run_id=result.run_id,
            )
            agent.runtime_status["daily_ready"] = True
        except Exception as e:
            agent.runtime_status["daily_progress"] = "⚠️ 每日检索失败"
            agent.runtime_status["daily_message"] = (
                f"⚠️ 每日检索失败: {type(e).__name__}: {e}"
            )
            agent.runtime_status["daily_ready"] = True
        finally:
            if worker:
                worker.close()
            daily_task["cancel_event"] = None
            daily_lock.release()

    def _start_daily_task(
        task_type: str, keyword: str | None = None, *, resume: bool = False,
    ) -> bool:
        if not daily_lock.acquire(blocking=False):
            return False
        if api_run_client_enabled:
            try:
                import requests
                response = requests.post(
                    f"{api_run_base}/api/v1/runs",
                    headers=_api_headers(),
                    json={
                        "kind": "daily",
                        "daily_kind": "resume" if resume else task_type,
                        "keyword": keyword,
                    },
                    timeout=(3, 10),
                )
                response.raise_for_status()
                run_id = str(response.json()["run_id"])
                daily_task["api_run_id"] = run_id
                agent.runtime_status["daily_ready"] = False
                agent.runtime_status["daily_progress"] = f"📰 queued · {run_id}"
                agent.runtime_status["daily_message"] = "📰 每日检索已提交到共享队列。"
                return True
            except Exception as exc:
                agent.runtime_status["daily_ready"] = True
                agent.runtime_status["daily_message"] = f"⚠️ 每日检索 API 请求失败：{type(exc).__name__}"
                return False
            finally:
                daily_lock.release()
        agent.runtime_status["daily_ready"] = False
        agent.runtime_status["daily_message"] = "📰 任务已开始，可继续使用其他功能。"
        cancel_event = threading.Event()
        daily_task["cancel_event"] = cancel_event
        try:
            worker = threading.Thread(
                target=_daily_search_bg,
                args=(task_type, keyword, resume, cancel_event), daemon=True,
                name=f"daily-{task_type}",
            )
            daily_task["thread"] = worker
            worker.start()
            return True
        except Exception:
            daily_lock.release()
            raise

    if cfg.daily_search_enabled:
        _start_daily_task("daily", resume=True)

    # ── 帮助文本 ──
    HELP_TEXT = """
**命令列表**

| 命令 | 说明 |
|------|------|
| `/help` | 显示此帮助 |
| `/model` | 模型、上下文、Token、RAG 状态 |
| `/tokens` | Token 消耗 + 费用统计 |
| `/new` | 开始新对话 |
| `/retry` | 重新发送本次运行中最后一个模型请求 |
| `/profile` | 查看用户画像 |

**📁 研究档案**

明确说“把这份研究方案保存为文档”即可生成可下载 Word 文档和可检索 Markdown；
之后可说“查一下之前保存的方案”，Agent 会按需检索，不会将档案自动塞进对话上下文。

**📚 论文**
| 命令 | 说明 |
|------|------|
| `/indexed` | 已索引论文列表 |

**📰 每日检索**
| 命令 | 说明 |
|------|------|
| `/daily` | 今日检索结果 |
| `/daily search <词>` | 立即进行一次临时检索 |
| `/daily on` | 启用每日检索 |
| `/daily off` | 暂停每日检索 |
| `/daily add <词>` | 添加关键词 (支持 AND) |
| `/daily unread` | 待读清单 |
| `/daily want <N>` | 标记想读 |
| `/daily read <N>` | 标记已读 |
| `/daily skip <N>` | 跳过 |
| `/daily retry` | 重新检索 |
| `/daily resume` | 继续上次未完成任务 |
| `/daily status` | 查看进度 |
    """.strip()

    # ═══ 聊天函数 ═══

    def _build_file_cmd(text: str, dest: Path, ext: str) -> str:
        """根据文件类型构建 Agent 命令"""
        if ext == ".pdf":
            cmd = f"read_pdf {dest}"
        else:
            cmd = f"describe_image {dest}"
        return cmd if not text else f"{cmd}\n{text}"

    def assistant_reply(
        message, session_id: str,
        request: gr.Request | None = None,
    ):
        # Web 请求必须携带浏览器级 session_state。不能回退到 agent.thread_id，
        # 否则状态更新乱序时可能把消息写入另一个会话。
        if not session_id or not agent.sessions.get(session_id):
            yield "⚠️ 会话已失效，请重新选择或新建会话。"
            return
        msg = ""
        # 处理多模态输入（文本+文件）
        files = []
        if isinstance(message, dict):
            text = message.get("text", "")
            files = message.get("files", [])
        else:
            text = message.strip() if isinstance(message, str) else ""

        # 保存上传文件 + 构建命令（含校验）
        for fpath in files:
            fname = Path(fpath).name
            fsize = Path(fpath).stat().st_size
            ext = Path(fpath).suffix.lower()
            if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
                yield f"⚠️ 不支持的文件类型: {ext}"; return

            # 大小校验（100MB）
            if fsize > 100 * 1024 * 1024:
                yield f"⚠️ 文件过大（{fsize/1024/1024:.0f}MB，上限 100MB）"; return

            # 内容 hash
            file_hash = hashlib.md5(Path(fpath).read_bytes()).hexdigest()
            base_dir = Path(cfg.papers_dir) if ext == ".pdf" else Path(cfg.images_dir)
            dest = cfg.runtime_paths.safe_child(base_dir, fname)

            # 检查内容重复
            dup_path = None
            for existing in base_dir.glob("*"):
                if existing.is_file() and existing.suffix.lower() == ext:
                    try:
                        if hashlib.md5(existing.read_bytes()).hexdigest() == file_hash:
                            dup_path = existing; break
                    except Exception:
                        pass

            # 处理冲突
            if dup_path and dup_path.name != fname:
                yield f"⚠️ 文件内容与 `{dup_path.name}` 完全相同。回复「**保存**」继续上传或「**跳过**」取消。"
                pending_conflicts[(session_id, fpath)] = {
                    "dest": dest, "hash": file_hash, "existing": dup_path, "text": text,
                }
                return
            elif dest.exists():
                yield f"⚠️ `{fname}` 已存在。回复「**覆盖**」替换或「**重命名**」自动改名。"
                pending_conflicts[(session_id, fpath)] = {
                    "dest": dest, "hash": file_hash, "existing": dest, "text": text,
                }
                return

            # 无冲突 → 直接保存
            shutil.copy(fpath, dest)
            text = _build_file_cmd(text, dest, ext)

        # 检查是否有待处理的冲突回复
        msg = text.strip()
        if msg in ("覆盖", "重命名", "保存", "跳过") and pending_conflicts:
            for conflict_key, info in list(pending_conflicts.items()):
                conflict_session_id, fpath = conflict_key
                if conflict_session_id != session_id:
                    continue
                if msg == "覆盖":
                    shutil.copy(fpath, info["dest"])
                    text = _build_file_cmd(info["text"], info["dest"], info["dest"].suffix.lower())
                elif msg == "重命名":
                    new_name = f"{info['dest'].stem}_1{info['dest'].suffix}"
                    new_dest = info["dest"].parent / new_name
                    shutil.copy(fpath, new_dest)
                    text = _build_file_cmd(info["text"], new_dest, new_dest.suffix.lower())
                elif msg == "保存":
                    shutil.copy(fpath, info["dest"])
                    text = _build_file_cmd(info["text"], info["dest"], info["dest"].suffix.lower())
                elif msg == "跳过":
                    yield "✅ 已跳过。"; return
                pending_conflicts.pop(conflict_key)
                break

        msg = text.strip()

        # ── 基础命令 ──
        retry_request = None
        if msg == "/retry":
            retry_request = agent.get_retry_input(session_id)
            if not retry_request:
                yield "⚠️ 本次启动后没有可重试的模型请求，请重新发送问题。"; return
            msg = retry_request["user_input"]

        if msg == "/help":
            yield HELP_TEXT; return

        if msg == "/model":
            tu = agent.get_usage(session_id)
            rag = f"{agent.paper_store.paper_count} 篇论文" if agent.paper_store else "未启用"
            yield f"**模型**: {agent.model}\n\n**Tokens**: {tu['total']:,} ({tu['calls']} 次调用)\n\n**RAG**: {rag}"
            return

        if msg == "/tokens":
            tu = agent.get_usage(session_id)
            accum = tu.get("cost", 0)
            last = tu.get("last_round_cost", 0)
            cached = tu.get("cached", 0)
            lines = [
                f"**Token 消耗统计**\n", f"| 指标 | 数值 |\n|------|------|",
                f"| API 调用 | {tu['calls']} 次 |", f"| 输入 tokens | {tu['prompt']:,} |",
            ]
            if cached:
                lines.append(f"| 缓存命中 | {cached:,} (¥0.02/1M) |")
            lines.extend([
                f"| 输出 tokens | {tu['completion']:,} |", f"| 合计 | {tu['total']:,} |",
                f"| 本轮费用 | ¥{last:.4f} |", f"| 累计费用 | ¥{accum:.4f} |",
            ])
            yield "\n".join(lines); return

        if msg == "/indexed":
            if not agent.paper_store:
                yield "RAG 未启用。"; return
            papers = agent.paper_store.list_papers()
            if not papers:
                yield "📭 尚未索引论文。"; return
            lines = [f"**已索引论文** ({len(papers)} 篇)\n"]
            for i, p in enumerate(papers, 1):
                lines.append(f"{i}. {p['title']} ({p['chunks']} chunks)")
            yield "\n".join(lines); return

        if msg in ("/new", "/reset"):
            yield "请使用会话栏的「＋ 新建会话」按钮，新对话会显示在会话列表中。"; return

        if msg == "/profile":
            yield agent.profile.read(); return

        if msg == "/daily":
            today = scheduler.get_today_results()
            if not today:
                yield "📭 今日尚未检索，或已检索但无新论文。"; return
            lines = ["**📰 今日论文速递**\n"]
            for s in today:
                lines.append(f"🔑 `{s['keyword']}` → {s['new_count']} 篇新论文")
            yield "\n".join(lines); return

        if msg.startswith("/daily search"):
            kw = msg[13:].strip()
            if not kw:
                yield "用法: /daily search <关键词>"; return
            error = scheduler.validate_keyword(kw)
            if error:
                yield f"❌ {error}"; return
            if not _start_daily_task("search", kw):
                yield "📰 已有每日检索任务在运行，请等待它完成。"; return
            yield "📰 临时检索已转入后台，可继续聊天；结果会显示在页面顶部的任务面板。"; return

        if msg.startswith("/daily add "):
            kw = msg[11:].strip()
            yield scheduler.add_keyword(kw); return

        if msg == "/daily unread":
            unread = scheduler.get_unread_papers(days=3)
            if not unread:
                yield "✅ 没有待读论文。"; return
            lines = ["**📋 待读清单 (最近 3 天)**\n"]
            for i, u in enumerate(unread, 1):
                lines.append(f"{i}. `{u['keyword']}` — {u['paper_title'][:60]}")
            lines.append("\n回复 `/daily want N` 标记想读，`/daily read N` 标记已读，`/daily skip N` 跳过")
            yield "\n".join(lines); return

        if msg in ("/daily on", "/daily start"):
            cfg.runtime_paths.update_settings({"daily_search_enabled": True})
            yield "✅ 每日检索已启用。下次启动自动运行。"; return

        if msg == "/daily retry":
            if not _start_daily_task("retry"):
                yield "📰 已有每日检索任务在运行，请等待它完成。"; return
            yield "📰 重试任务已转入后台，可继续聊天；结果会显示在页面顶部的任务面板。"; return

        if msg == "/daily resume":
            if not _start_daily_task("daily", resume=True):
                yield "📰 已有每日检索任务在运行，请等待它完成。"; return
            yield "📰 正在继续上次未完成的每日检索；已保存候选不会重复请求。"; return

        if msg == "/daily status":
            api_run_id = daily_task.get("api_run_id")
            if api_run_id and api_run_client_enabled:
                try:
                    import requests
                    response = requests.get(
                        f"{api_run_base}/api/v1/runs/{api_run_id}",
                        headers=_api_headers(), timeout=(3, 10),
                    )
                    response.raise_for_status()
                    api_run = response.json()
                    state = str(api_run.get("status") or "unknown")
                    if state in {"completed", "failed", "cancelled", "partial_failed"}:
                        agent.runtime_status["daily_ready"] = True
                        daily_task["api_run_id"] = None
                    yield f"📰 `{api_run_id}`：{state}"
                    return
                except Exception:
                    pass
            progress = agent.runtime_status.get("daily_progress") or scheduler.get_progress()
            if progress:
                yield progress; return
            yield "📰 每日检索未在运行。使用 /daily on 启用。"; return

        if msg in ("/daily off", "/daily stop"):
            cfg.runtime_paths.update_settings({"daily_search_enabled": False})
            api_run_id = daily_task.get("api_run_id")
            if api_run_id and api_run_client_enabled:
                try:
                    import requests
                    requests.post(
                        f"{api_run_base}/api/v1/runs/{api_run_id}/cancel",
                        headers=_api_headers(), timeout=(1, 3),
                    )
                    daily_task["api_run_id"] = None
                    yield "⏸ 已请求停止共享队列中的每日检索。"
                    return
                except Exception:
                    pass
            active_cancel = daily_task.get("cancel_event")
            if active_cancel:
                active_cancel.set()
                yield "⏹ 已停止当前每日检索并关闭自动运行；已保存候选可用 `/daily resume` 继续。"; return
            yield "⏸ 每日检索已暂停。"; return

        if msg.startswith("/daily want "):
            n = msg[12:].strip()
            if not n.isdigit():
                yield "用法: /daily want <编号>"; return
            unread = scheduler.get_unread_papers(days=3)
            idx = int(n) - 1
            if 0 <= idx < len(unread):
                u = unread[idx]
                scheduler.mark_want_read(u["keyword"], u["paper_title"])
                yield f"✅ 已标记想读: {u['paper_title'][:60]}"; return
            yield "❌ 编号无效"; return

        if msg.startswith("/daily skip "):
            n = msg[12:].strip()
            if not n.isdigit():
                yield "用法: /daily skip <编号>"; return
            unread = scheduler.get_unread_papers(days=3)
            idx = int(n) - 1
            if 0 <= idx < len(unread):
                u = unread[idx]
                scheduler.mark_skip(u["keyword"], u["paper_title"])
                yield f"🗑 已跳过: {u['paper_title'][:60]}"; return
            yield "❌ 编号无效"; return

        if msg.startswith("/daily read "):
            n = msg[12:].strip()
            if not n.isdigit():
                yield "用法: /daily read <编号>"; return
            unread = scheduler.get_unread_papers(days=3)
            idx = int(n) - 1
            if 0 <= idx < len(unread):
                u = unread[idx]
                scheduler.mark_read(u["keyword"], u["paper_title"])
                yield f"📖 已标记已读: {u['paper_title'][:60]}"; return
            yield "❌ 编号无效"; return

        research_match = re.match(
            r"^/research(?:\s+--sources=(both|local|public))?\s*(.*)$",
            msg,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if research_match:
            research_scope = research_match.group(1) or "both"
            research_query = research_match.group(2).strip()
            resume_research = research_query.lower() in {"continue", "继续"}
            if not research_query and not resume_research:
                yield "用法：`/research <研究问题>`，或输入 `/research continue` 继续上一次未完成的研究。"
                return

            if api_run_client_enabled and not resume_research:
                browser_id = _browser_id(request)
                request_generation = browser_run_guard.begin(browser_id)
                run_id = ""
                partial = "## 🔬 深度研究\n\n正在排队…"
                if browser_run_guard.is_current(browser_id, request_generation):
                    yield partial
                try:
                    import requests

                    response = requests.post(
                        f"{api_run_base}/api/v1/runs",
                        headers=_api_headers(),
                        json={
                            "kind": "research", "session_id": session_id,
                            "query": research_query, "scope": research_scope,
                        },
                        timeout=(3, 10),
                    )
                    response.raise_for_status()
                    run_id = str(response.json()["run_id"])
                    _remember_api_run(browser_id, run_id)
                    event_name = "status"
                    with requests.get(
                        f"{api_run_base}/api/v1/runs/{run_id}/events",
                        headers=_api_headers(), stream=True, timeout=(3, 600),
                    ) as stream:
                        stream.raise_for_status()
                        for raw_line in stream.iter_lines(decode_unicode=True):
                            if not browser_run_guard.is_current(browser_id, request_generation):
                                return
                            line = str(raw_line or "")
                            if line.startswith("event: "):
                                event_name = line[7:].strip()
                                continue
                            if not line.startswith("data: "):
                                continue
                            try:
                                event = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            if event_name == "status":
                                stage = str(event.get("stage") or "research")
                                state = str(event.get("status") or "running")
                                partial = f"## 🔬 深度研究\n\n**{stage}**：{state}"
                                yield partial
                            elif event_name == "error":
                                yield f"⚠️ 深度研究失败：{event.get('error_type', 'UnknownError')}"
                            elif event_name == "done":
                                answer = str(event.get("answer") or partial)
                                yield re.sub(r'(?<!\$)\$([^\$]*[\_^][^\$]*)\$(?!\$)', r'$$\1$$', answer)
                                return
                except Exception as exc:
                    if browser_run_guard.is_current(browser_id, request_generation):
                        yield f"⚠️ 深度研究 API 请求失败：{type(exc).__name__}"
                finally:
                    _forget_api_run(browser_id, run_id or None)
                    browser_run_guard.finish(browser_id, request_generation)
                return

            browser_id = _browser_id(request)
            request_generation = browser_run_guard.begin(browser_id)
            cancel_event = browser_run_guard.cancellation_event(browser_id, request_generation)
            import queue
            q = queue.Queue()
            final = []

            def _research_progress(event: dict) -> None:
                q.put(("progress", event))

            def _run_research():
                try:
                    result = agent.research(
                        "" if resume_research else research_query,
                        scope=research_scope,
                        session_id=session_id,
                        resume=resume_research,
                        cancel_event=cancel_event,
                        on_progress=_research_progress,
                    )
                except BaseException as exc:
                    result = f"❌ 深度研究后台错误: {type(exc).__name__}: {exc}"
                finally:
                    final.append(result)
                    q.put(("done", None))

            threading.Thread(target=_run_research, daemon=True, name="deep-research").start()
            partial = "## 🧭 深度研究\n\n正在启动研究任务…"
            latest_stage = "启动中"
            latest_message = "正在启动研究任务…"
            last_heartbeat = time.monotonic()
            if browser_run_guard.is_current(browser_id, request_generation):
                yield partial
            try:
                while True:
                    if not browser_run_guard.is_current(browser_id, request_generation):
                        return
                    try:
                        event, value = q.get(timeout=0.5)
                    except queue.Empty:
                        # Avoid flooding Gradio with identical updates while a
                        # model request is queued or a tool is running. A low
                        # frequency heartbeat still makes long waits visible.
                        if time.monotonic() - last_heartbeat >= 3:
                            partial = (
                                f"## 🧭 深度研究\n\n**{latest_stage}**：{latest_message}"
                                "\n\n_仍在处理中；可随时点击“停止”保留已有证据。_"
                            )
                            last_heartbeat = time.monotonic()
                            if browser_run_guard.is_current(browser_id, request_generation):
                                yield partial
                        continue
                    if event == "done":
                        break
                    latest_stage = value.get("stage", "research")
                    latest_message = value.get("message", "正在处理")
                    partial = f"## 🧭 深度研究\n\n**{latest_stage}**：{latest_message}"
                    last_heartbeat = time.monotonic()
                    if browser_run_guard.is_current(browser_id, request_generation):
                        yield partial
                if browser_run_guard.is_current(browser_id, request_generation):
                    yield re.sub(r'(?<!\$)\$([^\$]*[\\_^][^\$]*)\$(?!\$)', r'$$\1$$', final[0])
            except GeneratorExit:
                return
            finally:
                browser_run_guard.finish(browser_id, request_generation)
            return

        # 正常对话（流式）
        if api_run_client_enabled:
            browser_id = _browser_id(request)
            request_generation = browser_run_guard.begin(browser_id)
            run_id = ""
            partial = "⌛ 正在排队处理请求…"
            received_model_token = False
            if browser_run_guard.is_current(browser_id, request_generation):
                yield partial
            try:
                import requests

                response = requests.post(
                    f"{api_run_base}/api/v1/runs",
                    headers=_api_headers(),
                    json={"kind": "chat", "session_id": session_id, "message": msg},
                    timeout=(3, 10),
                )
                response.raise_for_status()
                run_id = str(response.json()["run_id"])
                _remember_api_run(browser_id, run_id)
                event_name = "status"
                with requests.get(
                    f"{api_run_base}/api/v1/runs/{run_id}/events",
                    headers=_api_headers(), stream=True, timeout=(3, 300),
                ) as stream:
                    stream.raise_for_status()
                    for raw_line in stream.iter_lines(decode_unicode=True):
                        if not browser_run_guard.is_current(browser_id, request_generation):
                            return
                        line = str(raw_line or "")
                        if line.startswith("event: "):
                            event_name = line[7:].strip()
                            continue
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if event_name == "token":
                            text = str(event.get("text") or "")
                            if text:
                                if not received_model_token:
                                    partial = ""
                                    received_model_token = True
                                partial += text
                                yield partial
                        elif event_name == "error":
                            partial = f"⚠️ 后台请求失败：{event.get('error_type', 'UnknownError')}"
                            yield partial
                        elif event_name == "done":
                            answer = str(event.get("answer") or partial)
                            yield re.sub(r'(?<!\$)\$([^\$]*[\_^][^\$]*)\$(?!\$)', r'$$\1$$', answer)
                            return
            except Exception as exc:
                if browser_run_guard.is_current(browser_id, request_generation):
                    yield f"⚠️ API 对话请求失败：{type(exc).__name__}"
            finally:
                _forget_api_run(browser_id, run_id or None)
                browser_run_guard.finish(browser_id, request_generation)
            return

        browser_id = _browser_id(request)
        request_generation = browser_run_guard.begin(browser_id)
        cancel_event = browser_run_guard.cancellation_event(browser_id, request_generation)

        def can_update_chat() -> bool:
            return browser_run_guard.is_current(browser_id, request_generation)

        import queue, threading
        q = queue.Queue()
        final = []

        def _run():
            try:
                r = agent.step(
                    msg,
                    on_token=lambda token: q.put(("token", token)),
                    session_id=session_id,
                    cancel_event=cancel_event,
                )
            except BaseException as exc:
                r = f"❌ 后台请求异常: {type(exc).__name__}: {exc}"
            finally:
                final.append(r)
                q.put(("done", None))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        partial = "⏳ 正在处理请求…"
        received_model_token = False
        if can_update_chat():
            yield partial
        try:
            while True:
                if not can_update_chat():
                    return
                try:
                    event, value = q.get(timeout=0.1)
                except queue.Empty:
                    if can_update_chat():
                        yield partial
                    continue
                if event == "done":
                    break
                if not received_model_token:
                    partial = ""
                    received_model_token = True
                partial += value
                if can_update_chat():
                    yield partial
            if can_update_chat():
                yield re.sub(r'(?<!\$)\$([^\$]*[\\_^][^\$]*)\$(?!\$)', r'$$\1$$', final[0])
        except GeneratorExit:
            return
        finally:
            browser_run_guard.finish(browser_id, request_generation)

    def _message_text(message) -> str:
        """把多模态输入转换为聊天框中安全、简短的用户消息。"""
        if isinstance(message, dict):
            text = str(message.get("text") or "").strip()
            files = [Path(path).name for path in (message.get("files") or [])]
            attachment_text = "\n".join(f"📎 {name}" for name in files)
            return "\n".join(part for part in (attachment_text, text) if part)
        return str(message or "").strip()

    def prepare_user_message(message, history: list[dict]):
        """立即显示用户消息，并将原始上传路径只保存到本次请求状态。"""
        shown = _message_text(message)
        history = list(history or [])
        if not shown:
            return gr.update(), history, history, None
        updated = history + [{"role": "user", "content": shown}]
        return gr.update(value=None), updated, updated, message

    def prepare_research_message(message, history: list[dict], scope: str):
        """Show the natural-language question while sending an explicit research command."""
        shown = _message_text(message)
        history = list(history or [])
        if not shown:
            return gr.update(), history, history, None
        if isinstance(message, dict) and message.get("files"):
            # The researcher has deliberately bounded tool access. File import
            # remains an explicit normal-chat action so its path never becomes
            # part of a slash command or a worker prompt accidentally.
            updated = history + [{"role": "assistant", "content": "⚠️ 请先在普通对话中上传并索引文件，再发起深度研究。"}]
            return gr.update(), updated, updated, None
        if isinstance(message, dict):
            pending = dict(message)
        else:
            pending = {"text": str(message), "files": []}
        pending["text"] = f"/research --sources={scope} {str(pending.get('text') or '').strip()}"
        updated = history + [{"role": "user", "content": shown}]
        return gr.update(value=None), updated, updated, pending

    def prepare_continue_research(history: list[dict]):
        history = list(history or [])
        updated = history + [{"role": "user", "content": "继续上次深度研究"}]
        return updated, updated, {"text": "/research continue", "files": []}

    def stream_reply(
        message,
        history: list[dict],
        session_id: str,
        request: gr.Request | None = None,
    ):
        """将 Agent 的逐段文本更新为受控聊天历史，不依赖 ChatInterface 内部 State。"""
        if not _message_text(message):
            return
        base_history = list(history or [])
        for reply in assistant_reply(message, session_id, request):
            updated = base_history + [{"role": "assistant", "content": reply}]
            yield updated, updated

    def stop_active_reply(request: gr.Request | None = None):
        """停止当前浏览器的流式输出，并向 Agent 传播取消令牌。"""
        _invalidate_browser_request(request)

    def refresh_status(history, session_id: str | None = None):
        session_id = session_id or agent.thread_id
        tu = agent.get_usage(session_id)
        rag = f"{agent.paper_store.paper_count} 篇" if agent.paper_store else "关"
        last = tu.get("last_prompt", 0)
        limit = tu.get("context_limit", 131072)
        limit_k = f"{limit//1000000}M" if limit >= 1000000 else f"{limit//1000}K"
        pct = min(round(last / limit * 100), 99) if last else 0
        vs = tu.get("verify_status", "")
        api = tu.get("api_status", "")
        bw = tu.get("budget_warning", "")
        signals = [value for value in (api, vs, bw) if value]
        signal_text = f" · **{signals[-1]}**" if signals else ""
        return (
            f"模型：**{agent.model}** · 上下文：{pct}% ({last:,}/{limit_k}) · "
            f"Tokens：{tu['total']:,} · RAG：{rag}{signal_text}"
        )

    def refresh_daily_panel() -> str:
        """后台任务结果独立展示，避免被写入任意一个会话的聊天记录。"""
        message = agent.runtime_status.get("daily_message", "")
        progress = agent.runtime_status.get("daily_progress", "")
        if message:
            return f"### 📰 每日检索任务\n\n{message}\n\n{progress}"
        return ""

    def refresh_current_agent_run(session_id: str | None) -> str:
        """Refresh the compact status card without reading chat history."""
        return timeline.render_latest_chat(session_id or agent.thread_id)

    def refresh_run_center(scope: str, session_id: str | None) -> str:
        if scope == "daily":
            return timeline.render_daily_runs()
        return timeline.render_session_runs(session_id or agent.thread_id)

    def _latest_session_run_id(session_id: str | None) -> str:
        """Choose the newest user-visible run without reading its content."""
        if not session_id:
            return ""
        runs = [
            *agent.sessions.list_chat_runs(session_id, limit=1),
            *agent.sessions.list_research_runs(session_id, limit=1),
        ]
        if not runs:
            return ""
        latest = max(
            runs,
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("created_at") or "")),
        )
        return str(latest.get("run_id") or "")

    def _local_badcase_run(run_id: str) -> dict | None:
        if run_id.startswith("chat-"):
            run = agent.sessions.get_chat_run(run_id)
            kind = "chat"
            if run:
                return {
                    "run_id": run_id, "kind": kind, "session_id": run["thread_id"],
                    "status": run["status"], "model": run.get("model", ""),
                    "duration_ms": run.get("duration_ms"), "metrics": run.get("metrics", {}),
                    "error_type": run.get("error_type", ""),
                    "events": agent.sessions.get_run_events(run_id, run["thread_id"]),
                }
        if run_id.startswith("research-"):
            run = agent.sessions.get_research_run(run_id)
            if run:
                trace = run.get("trace") if isinstance(run.get("trace"), dict) else {}
                usage = trace.get("usage") if isinstance(trace.get("usage"), dict) else {}
                return {
                    "run_id": run_id, "kind": "research", "session_id": run["thread_id"],
                    "status": run["status"], "model": agent.model,
                    "duration_ms": trace.get("duration_ms"), "metrics": usage,
                    "error_type": trace.get("error_type", ""),
                    "events": agent.sessions.get_run_events(run_id, run["thread_id"]),
                }
        if run_id.startswith("daily-"):
            run = scheduler.get_daily_run(run_id)
            if run:
                result = run.get("result") if isinstance(run.get("result"), dict) else {}
                candidates = run.get("candidates") if isinstance(run.get("candidates"), list) else []
                selected = result.get("selected") if isinstance(result.get("selected"), list) else []
                events = [
                    {
                        "event_type": event.get("event_type"),
                        "stage": event.get("agent"),
                        "status": event.get("status"),
                        "metrics": event.get("details"),
                        "metadata": event.get("metadata"),
                    }
                    for event in scheduler.get_daily_agent_events(run_id)
                ]
                return {
                    "run_id": run_id, "kind": "daily", "session_id": "",
                    "status": run["status"], "model": "",
                    "metrics": {
                        "candidate_count": len(candidates), "selected_count": len(selected),
                    },
                    "error_type": str(run.get("error_text") or "").split(":", 1)[0],
                    "events": events,
                }
        return None

    def report_badcase(
        session_id: str | None, run_id: str, category: str, note: str,
    ) -> str:
        selected_run_id = (run_id or "").strip() or _latest_session_run_id(session_id)
        if not selected_run_id:
            return "⚠️ 当前会话没有可标记的运行任务。"
        try:
            if api_run_client_enabled:
                import requests

                response = requests.post(
                    f"{api_run_base}/api/v1/runs/{selected_run_id}/badcases",
                    headers=_api_headers(), json={"category": category, "note": note},
                    timeout=(3, 10),
                )
                response.raise_for_status()
                candidate = response.json()
            else:
                run = _local_badcase_run(selected_run_id)
                if not run:
                    return "⚠️ 未找到该运行任务。"
                candidate, _created = badcases.create_candidate(
                    run, category=category, note=note,
                )
        except Exception as exc:
            return f"⚠️ 标记问题失败：{type(exc).__name__}"
        return (
            f"✅ 已加入本地 Badcase 候选池：`{candidate['candidate_id']}`。"
            "仅保存脱敏运行快照；请在人工审核后再转成合成回归样例。"
        )

    def _session_choices():
        return [
            (f"{session['title']} · {session['updated_at'][:16]}", session["thread_id"])
            for session in agent.list_sessions()
        ]

    def refresh_chat_status(history, session_id: str):
        """聊天框变化后只刷新状态，避免程序化 change 反向切换会话。"""
        return refresh_status(history, session_id)

    def new_session(request: gr.Request | None = None):
        _invalidate_browser_request(request)
        session = agent.create_session()
        return (
            gr.update(choices=_session_choices(), value=session["thread_id"]),
            [],
            [],
            session["thread_id"],
            refresh_status([], session["thread_id"]),
            gr.update(value=False),
        )

    def switch_session(session_id: str, request: gr.Request | None = None):
        _invalidate_browser_request(request)
        if not agent.sessions.get(session_id):
            return (
                [], [], agent.thread_id, refresh_status([], agent.thread_id),
            )
        history = agent.get_history(session_id)
        return (
            history, history, session_id, refresh_status(history, session_id),
        )

    def delete_current_session(
        session_id: str, confirmed: bool, request: gr.Request | None = None,
    ):
        if not confirmed:
            return (
                gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(value=False),
                "⚠️ 请先勾选“确认永久删除当前会话”。",
            )
        _invalidate_browser_request(request)
        agent.delete_session(session_id)
        badcases.delete_for_session(session_id)
        for key in [key for key in pending_conflicts if key[0] == session_id]:
            pending_conflicts.pop(key, None)
        remaining = agent.list_sessions()
        next_session_id = remaining[0]["thread_id"] if remaining else agent.thread_id
        history = agent.get_history(next_session_id)
        return (
            gr.update(choices=_session_choices(), value=next_session_id),
            history,
            history,
            next_session_id,
            gr.update(value=False),
            refresh_status(history, next_session_id),
        )

    # ═══ 论文卡片 ───

    def _research_document_choices() -> list[tuple[str, str]]:
        return [
            (
                f"{item['title']} · 第 {item['revision']} 版",
                item["document_id"],
            )
            for item in research_documents.list(limit=50)
        ]

    def research_documents_html() -> str:
        documents = research_documents.list(limit=20)
        if not documents:
            return (
                "<p style='color:#999;padding:0.5rem 0;'>暂无研究档案。"
                "在对话中明确要求“把这份研究方案保存为文档”即可创建。</p>"
            )
        cards = []
        for document in documents:
            title = html.escape(str(document["title"]))
            summary = html.escape(str(document.get("summary") or ""))
            cards.append(
                "<div style='padding:0.55rem 0;border-bottom:1px solid #edf0f4;'>"
                f"<strong>{title}</strong><br>"
                f"<small style='color:#6b7280;'>第 {document['revision']} 版 · {document['updated_at']}</small><br>"
                f"<span style='color:#49566b;font-size:0.8rem;'>{summary}</span>"
                "</div>"
            )
        return "<div class='paper-list'>" + "".join(cards) + "</div>"

    def refresh_research_documents():
        return (
            research_documents_html(),
            gr.update(choices=_research_document_choices(), value=None),
            "选择一份档案可预览 Markdown 并下载 Word 文档。",
            None,
        )

    def show_research_document(document_id: str):
        document = research_documents.read(document_id)
        if document is None:
            return "⚠️ 未找到该档案，请刷新列表。", None
        return document["content"], document["docx_path"]

    def paper_cards_html():
        if not agent.paper_store:
            return "<p style='color:#999;padding:1rem;'>RAG 未启用</p>"
        papers = agent.paper_store.list_papers()
        if not papers:
            return "<p style='color:#999;padding:1rem;'>暂无索引论文。在对话中输入 read_pdf 添加。</p>"
        html = '<div style="display:grid;gap:10px;padding:0.5rem;">'
        for p in papers:
            html += (
                f'<div style="border:1px solid #e0e0e0;border-radius:8px;padding:12px;background:#fafafa;">'
                f'<strong style="font-size:0.95rem;">{p["title"][:80]}</strong>'
                f'<div style="color:#888;font-size:0.8rem;margin:4px 0;">'
                f'{p["chunks"]} chunks | {p.get("indexed_at", "")[:16]}'
                f'</div></div>'
            )
        html += '</div>'
        return html

    # ═══ 设置函数 ───

    def save_config(model, rag_enabled, max_pages, daily_enabled_val):
        cfg.runtime_paths.update_settings({
            "model": model,
            "rag_enabled": bool(rag_enabled),
            "pdf_max_pages": int(max_pages),
            "daily_search_enabled": bool(daily_enabled_val),
        })
        return "✅ 已保存到运行时设置，重启服务后生效。"

    # ═══ 构建界面 ═══

    with gr.Blocks(title="🔬 Research Assistant", elem_classes=["research-app"]) as demo:
        # gr.State 是浏览器请求级状态，避免两个标签页切换到彼此的会话。
        session_state = gr.State(value=agent.thread_id)
        with gr.Row(elem_classes=["workbench-header"]):
            gr.HTML(
                '<div class="workbench-mark">🔬</div>'
                '<div><p class="workbench-title">Research Assistant</p>'
                '<p class="workbench-subtitle">论文证据、研究推理与可追溯运行</p></div>',
            )
            status = gr.Markdown(
                refresh_status([], agent.thread_id), elem_classes=["status-bar"], scale=12,
            )
            quit_btn = gr.Button("退出", size="sm", elem_classes=["quit-btn"])

        # The main column is declared first so the chat composer remains the
        # first textarea in the DOM for keyboard and browser-test ergonomics.
        # CSS places the workspace column visually on the left.
        with gr.Row(elem_classes=["app-shell"]):
            with gr.Column(scale=8, min_width=520, elem_classes=["workspace-main"]):
                gr.HTML(
                    '<p class="workspace-eyebrow">研究工作台</p>'
                    '<h2 class="workspace-heading">开始一项研究任务</h2>',
                )
                # 受控聊天状态：不使用 ChatInterface 的私有 chatbot_state，
                # 会话切换后的可见历史与请求历史只有同一个 browser State。
                initial_history = agent.get_history(agent.thread_id)
                chat_history_state = gr.State(value=initial_history)
                chatbot = gr.Chatbot(
                    value=initial_history,
                    height=520,
                    show_label=False,
                    placeholder="从一个研究问题、论文或假设开始。",
                    render_markdown=True,
                    latex_delimiters=CHAT_LATEX_DELIMITERS,
                    elem_id="research-chat",
                )
                with gr.Column(elem_classes=["composer-shell"]):
                    with gr.Row(elem_classes=["composer-actions"]):
                        chat_input = gr.MultimodalTextbox(
                            placeholder="提问、输入命令，或拖入 PDF / 图片…",
                            container=False, scale=9,
                            file_types=[".pdf", ".png", ".jpg", ".jpeg"],
                            submit_btn="发送", stop_btn=False,
                        )
                        stop_reply_btn = gr.Button("停止", variant="stop", scale=1)
                    with gr.Accordion("深度研究", open=False, elem_classes=["research-tools"]):
                        gr.Markdown("规划并并行收集本地与公开证据；长任务可在右侧检查器查看进度。")
                        with gr.Row(elem_classes=["research-actions"]):
                            research_scope = gr.Dropdown(
                                label="来源范围",
                                choices=[
                                    ("本地论文 + 公开文献", "both"),
                                    ("仅本地论文", "local"),
                                    ("仅公开文献", "public"),
                                ],
                                value="both", scale=3,
                            )
                            deep_research_btn = gr.Button("开始深度研究", variant="secondary", scale=2)
                            continue_research_btn = gr.Button("继续上次", scale=2)
                pending_message = gr.State(value=None)

                submit_event = chat_input.submit(
                    fn=prepare_user_message,
                    inputs=[chat_input, chat_history_state],
                    outputs=[chat_input, chatbot, chat_history_state, pending_message],
                    queue=False,
                )
                reply_event = submit_event.then(
                    fn=stream_reply,
                    inputs=[pending_message, chat_history_state, session_state],
                    outputs=[chatbot, chat_history_state],
                    concurrency_limit=4,
                    show_progress="hidden",
                )
                research_submit_event = deep_research_btn.click(
                    fn=prepare_research_message,
                    inputs=[chat_input, chat_history_state, research_scope],
                    outputs=[chat_input, chatbot, chat_history_state, pending_message],
                    queue=False,
                )
                research_reply_event = research_submit_event.then(
                    fn=stream_reply,
                    inputs=[pending_message, chat_history_state, session_state],
                    outputs=[chatbot, chat_history_state],
                    concurrency_limit=4,
                    show_progress="hidden",
                )
                continue_submit_event = continue_research_btn.click(
                    fn=prepare_continue_research,
                    inputs=[chat_history_state],
                    outputs=[chatbot, chat_history_state, pending_message],
                    queue=False,
                )
                continue_reply_event = continue_submit_event.then(
                    fn=stream_reply,
                    inputs=[pending_message, chat_history_state, session_state],
                    outputs=[chatbot, chat_history_state],
                    concurrency_limit=4,
                    show_progress="hidden",
                )
                stop_reply_btn.click(
                    fn=stop_active_reply,
                    outputs=[],
                    queue=False,
                    cancels=[reply_event, research_reply_event, continue_reply_event],
                )
                chatbot.change(
                    fn=refresh_chat_status, inputs=[chat_history_state, session_state],
                    outputs=[status],
                )

            with gr.Column(scale=3, min_width=270, elem_classes=["workspace-sidebar"]):
                gr.HTML('<p class="sidebar-label">当前工作区</p>')
                session_picker = gr.Dropdown(
                    label="会话", choices=_session_choices(), value=agent.thread_id, interactive=True,
                )
                with gr.Row(elem_classes=["session-actions"]):
                    new_session_btn = gr.Button("＋ 新建会话", variant="primary", scale=1)
                with gr.Accordion("会话管理", open=False, elem_classes=["utility-panel"]):
                    gr.Markdown("删除会同时清除该会话的历史、运行记录和关联 Badcase 候选。")
                    delete_confirm = gr.Checkbox(label="确认永久删除当前会话", value=False)
                    delete_session_btn = gr.Button("删除当前会话", variant="stop")

                with gr.Accordion("论文库", open=False, elem_classes=["utility-panel"]):
                    paper_list = gr.HTML(value=paper_cards_html(), elem_classes=["paper-list"])
                    refresh_papers_btn = gr.Button("刷新论文库", size="sm")
                refresh_papers_btn.click(fn=paper_cards_html, outputs=[paper_list], queue=False)

                with gr.Accordion("研究档案", open=False, elem_classes=["utility-panel"]):
                    research_document_list = gr.HTML(value=research_documents_html())
                    refresh_research_documents_btn = gr.Button("刷新研究档案", size="sm")
                    research_document_picker = gr.Dropdown(
                        choices=_research_document_choices(),
                        label="查看或下载",
                        interactive=True,
                    )
                    research_document_preview = gr.Markdown(
                        "选择一份档案可预览 Markdown 并下载 Word 文档。",
                    )
                    research_document_download = gr.File(
                        label="Word 文档",
                        interactive=False,
                    )
                refresh_research_documents_btn.click(
                    fn=refresh_research_documents,
                    outputs=[
                        research_document_list,
                        research_document_picker,
                        research_document_preview,
                        research_document_download,
                    ],
                    queue=False,
                )
                research_document_picker.change(
                    fn=show_research_document,
                    inputs=[research_document_picker],
                    outputs=[research_document_preview, research_document_download],
                    queue=False,
                )

                with gr.Accordion("每日检索关键词", open=False, elem_classes=["utility-panel"]):
                    kw_list = [k["keyword"] for k in scheduler.list_keywords()] if scheduler.list_keywords() else []
                    kw_dd = gr.Dropdown(
                        label="已有关键词", choices=kw_list,
                        value=kw_list[0] if kw_list else None, interactive=True,
                    )
                    kw_input = gr.Textbox(label="新关键词", placeholder="例如：TSN scheduling reinforcement learning")
                    with gr.Row(elem_classes=["session-actions"]):
                        kw_add_btn = gr.Button("添加", scale=1)
                        kw_del_btn = gr.Button("删除", variant="secondary", scale=1)
                    kw_msg = gr.Markdown("")

                    def _add_kw(kw):
                        msg = scheduler.add_keyword(kw)
                        kws = [k["keyword"] for k in scheduler.list_keywords()]
                        return gr.update(choices=kws, value=kws[0] if kws else None), "", msg

                    def _del_kw(kw):
                        scheduler.remove_keyword(kw)
                        kws = [k["keyword"] for k in scheduler.list_keywords()]
                        return gr.update(choices=kws, value=kws[0] if kws else None), f"🗑 已删除: {kw}"

                kw_add_btn.click(fn=_add_kw, inputs=[kw_input], outputs=[kw_dd, kw_input, kw_msg])
                kw_del_btn.click(fn=_del_kw, inputs=[kw_dd], outputs=[kw_dd, kw_msg])

                with gr.Accordion("设置", open=False, elem_classes=["utility-panel"]):
                    with gr.Column(elem_classes=["settings-stack"]):
                        model_dd = gr.Dropdown(
                            label="模型", value=cfg.model,
                            choices=["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
                            allow_custom_value=True,
                        )
                        rag_toggle = gr.Checkbox(label="启用 RAG 论文库", value=cfg.rag_enabled)
                        max_pages_slider = gr.Slider(
                            label="PDF 最大页数", minimum=5, maximum=100, step=5, value=cfg.pdf_max_pages,
                        )
                        daily_enabled = gr.Checkbox(label="启用每日自动检索", value=cfg.daily_search_enabled)
                        save_cfg_btn = gr.Button("保存设置", variant="primary", size="sm")
                        cfg_msg = gr.Markdown("")
                save_cfg_btn.click(
                    fn=save_config,
                    inputs=[model_dd, rag_toggle, max_pages_slider, daily_enabled],
                    outputs=[cfg_msg],
                )

            with gr.Column(scale=3, min_width=285, elem_classes=["workspace-inspector"]):
                gr.HTML('<p class="inspector-label">运行检查器</p>')
                with gr.Accordion("本轮 Agent 状态", open=True, elem_classes=["inspector-panel"]):
                    current_run_panel = gr.HTML(value=refresh_current_agent_run(agent.thread_id))
                    refresh_current_run_btn = gr.Button("刷新本轮状态", size="sm")
                refresh_current_run_btn.click(
                    fn=refresh_current_agent_run,
                    inputs=[session_state],
                    outputs=[current_run_panel],
                    show_progress="hidden",
                    queue=False,
                )

                with gr.Accordion("全部运行任务", open=False, elem_classes=["inspector-panel"]):
                    gr.Markdown("只展示脱敏的状态、耗时与错误类别。")
                    run_center_scope = gr.Radio(
                        label="范围", choices=[("当前会话", "session"), ("每日检索", "daily")], value="session",
                    )
                    run_center_panel = gr.HTML(value=refresh_run_center("session", agent.thread_id))
                    refresh_run_center_btn = gr.Button("刷新运行记录", size="sm")
                run_center_scope.change(
                    fn=refresh_run_center,
                    inputs=[run_center_scope, session_state],
                    outputs=[run_center_panel],
                    show_progress="hidden",
                    queue=False,
                )
                refresh_run_center_btn.click(
                    fn=refresh_run_center,
                    inputs=[run_center_scope, session_state],
                    outputs=[run_center_panel],
                    show_progress="hidden",
                )

                with gr.Accordion("每日检索", open=False, elem_classes=["inspector-panel"]):
                    daily_panel = gr.Markdown(refresh_daily_panel(), elem_classes=["daily-panel"])
                    daily_refresh_btn = gr.Button("刷新每日任务", size="sm")
                daily_refresh_btn.click(
                    fn=refresh_daily_panel, outputs=[daily_panel], show_progress="hidden", queue=False,
                )

                with gr.Accordion("标记问题（Badcase）", open=False, elem_classes=["inspector-panel"]):
                    gr.Markdown(
                        "留空时标记当前会话最新任务；也可填入聊天、研究或每日任务的运行 ID。"
                        "不会自动复制问题、回答、PDF 或工具原始结果。"
                    )
                    badcase_run_id = gr.Textbox(label="运行 ID（可选）", max_lines=1)
                    badcase_category = gr.Dropdown(
                        label="问题类型",
                        choices=[
                            ("检索遗漏", "retrieval_miss"),
                            ("引用质量", "citation_quality"),
                            ("回答质量", "answer_quality"),
                            ("工具失败", "tool_failure"),
                            ("性能问题", "performance"),
                            ("安全问题", "safety"),
                            ("其他", "other"),
                        ],
                        value="answer_quality",
                    )
                    badcase_note = gr.Textbox(
                        label="脱敏备注（可选）", max_lines=3,
                        placeholder="请勿粘贴原始问题、回答、论文正文或密钥。",
                    )
                    report_badcase_btn = gr.Button("标记问题", variant="secondary")
                    badcase_status = gr.Markdown()
                report_badcase_btn.click(
                    fn=report_badcase,
                    inputs=[session_state, badcase_run_id, badcase_category, badcase_note],
                    outputs=[badcase_status],
                    show_progress="hidden",
                )

        # These callbacks also update the workspace controls after a session change.
        session_picker.change(
            fn=switch_session, inputs=[session_picker],
            outputs=[chatbot, chat_history_state, session_state, status],
            queue=False,
        )
        new_session_btn.click(
            fn=new_session,
            outputs=[
                session_picker, chatbot, chat_history_state,
                session_state, status, delete_confirm,
            ],
            queue=False,
        )
        delete_session_btn.click(
            fn=delete_current_session,
            inputs=[session_state, delete_confirm],
            outputs=[
                session_picker, chatbot, chat_history_state,
                session_state, delete_confirm, status,
            ],
            queue=False,
        )
        quit_btn.click(fn=lambda: (demo.close(), os._exit(0)), outputs=[])

    launch_fn = demo.launch if launch else (lambda **_kwargs: demo)
    launch_fn(
        server_name=args.host if args else os.getenv("UI_HOST", "127.0.0.1"),
        server_port=args.port if args else int(os.getenv("UI_PORT", "7860")),
        share=False, show_error=True,
        theme=UI_THEME,
        css=UI_CSS,
        js=UI_JS,
    )
    return demo


if __name__ == "__main__":
    build_ui()

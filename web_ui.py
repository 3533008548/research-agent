"""
🌐 科研助手 Web UI — 极简聊天界面

基于 Gradio Blocks + 受控 Chatbot 状态，支持：
  - 斜杠命令: /model /tokens /indexed /new /help
  - 笔记命令: /note topics /note add /note list /note del
  - ChromaDB RAG 论文检索
  - 公式实时 MathJax 渲染
  - 状态栏实时更新 + 弹出笔记面板

启动:
  python web_ui.py
  python web_ui.py -m deepseek-v4-pro --port 8080
"""

import argparse
import os
import re
import sys
import shutil
import hashlib
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
from notes import NoteStore
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


def build_ui():
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
    args = parser.parse_args()

    from logger import setup_logging
    setup_logging(debug=args.debug)
    from config import Config

    cli = {
        "model": args.model,
        "rag_enabled": False if args.no_rag else None,
        "ui_debug": True if args.debug else None,
        "data_dir": args.data_dir,
    }
    cfg = Config.load({k: v for k, v in cli.items() if v is not None})

    print(f"🤖 模型: {cfg.model}")
    agent = ResearchAgent(cfg=cfg)
    notes = NoteStore(cfg.notes_db)
    current_topic = {"name": "默认"}
    pending_conflicts = {}
    from scheduler import Scheduler
    scheduler = Scheduler(
        cfg.daily_db, request_timeout_seconds=cfg.daily_request_timeout_seconds,
    )
    import threading

    daily_lock = threading.Lock()
    daily_task = {"thread": None, "cancel_event": None}
    browser_run_guard = BrowserRunGuard()

    def _browser_id(request: gr.Request | None) -> str:
        """取得 Gradio 浏览器标签页的稳定标识；仅用于请求失效控制。"""
        return getattr(request, "session_hash", None) or "anonymous-browser"

    def _invalidate_browser_request(request: gr.Request | None) -> None:
        browser_run_guard.invalidate(_browser_id(request))

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

**📚 论文 / 笔记**
| 命令 | 说明 |
|------|------|
| `/indexed` | 已索引论文列表 |
| `/note topics` | 列出笔记话题 |
| `/note topic <名>` | 切换话题 |
| `/note add <内容>` | 添加笔记 |
| `/note list` | 当前话题笔记 |
| `/note del <编号>` | 删除笔记 |
| `/note link <id> <论文>` | 笔记关联论文 |

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

        if msg.startswith("/note"):
            yield _handle_note_cmd(msg); return

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
            progress = agent.runtime_status.get("daily_progress") or scheduler.get_progress()
            if progress:
                yield progress; return
            yield "📰 每日检索未在运行。使用 /daily on 启用。"; return

        if msg in ("/daily off", "/daily stop"):
            cfg.runtime_paths.update_settings({"daily_search_enabled": False})
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
                        context=_build_context(),
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
        browser_id = _browser_id(request)
        request_generation = browser_run_guard.begin(browser_id)
        cancel_event = browser_run_guard.cancellation_event(browser_id, request_generation)

        def can_update_chat() -> bool:
            return browser_run_guard.is_current(browser_id, request_generation)

        ctx = retry_request["context"] if retry_request else _build_context()
        request_topic = retry_request["topic"] if retry_request else current_topic["name"]
        import queue, threading
        q = queue.Queue()
        final = []

        def _run():
            try:
                r = agent.step(
                    msg, context=ctx if ctx else None,
                    on_token=lambda token: q.put(("token", token)),
                    session_id=session_id, topic=request_topic,
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

    def _handle_note_cmd(msg: str) -> str:
        parts = msg[5:].strip()  # 去掉 "/note"
        if not parts:
            return "用法:\n- `/note topics` 查看话题\n- `/note topic <名>` 切换话题\n- `/note add <内容>` 添加笔记\n- `/note list` 列出笔记\n- `/note del <编号>` 删除笔记"

        if parts == "topics":
            topics = notes.list_topics()
            if not topics:
                return "📭 还没有笔记话题。"
            lines = [f"**笔记话题** ({len(topics)})\n"]
            for t in topics:
                lines.append(f"- {t['name']}（{t['count']} 条笔记）")
            return "\n".join(lines)

        if parts.startswith("topic "):
            name = parts[6:].strip()
            current_topic["name"] = name
            notes.ensure_topic(name)
            return f"✅ 当前笔记话题: **{name}**"

        if parts.startswith("add "):
            content = parts[4:].strip()
            if not content:
                return "用法: `/note add <笔记内容>`"
            tid = notes.add(current_topic["name"], content)
            return f"✅ 笔记 #{tid} 已保存到话题「{current_topic['name']}」"

        if parts == "list":
            all_notes = notes.list_notes(current_topic["name"])
            if not all_notes:
                return f"📭 话题「{current_topic['name']}」还没有笔记。"
            lines = [f"**{current_topic['name']}** 的笔记：\n"]
            for n in all_notes:
                preview = n['content'].replace('\n', ' ')[:80]
                lines.append(f"#{n['id']}  {preview}")
            return "\n".join(lines)

        if parts.startswith("del "):
            nid = parts[4:].strip()
            if not nid.isdigit():
                return "用法: `/note del <编号>`"
            ok = notes.delete(int(nid))
            return f"🗑️ 笔记 #{nid} 已删除。" if ok else f"❌ 未找到笔记 #{nid}"

        if parts.startswith("link "):
            args_link = parts[5:].strip().split(" ", 1)
            if len(args_link) < 2 or not args_link[0].isdigit():
                return "用法: `/note link <笔记编号> <论文标题>`"
            nid = int(args_link[0])
            paper = args_link[1].strip()
            note = notes.get(nid)
            if not note:
                return f"❌ 未找到笔记 #{nid}"
            notes.set_paper(nid, paper)
            return f"✅ 笔记 #{nid} 已关联论文「{paper}」"

        return f"❌ 未知命令: /note {parts}\n输入 `/note` 查看用法。"

    # ═══ 上下文构建（打通三库关联） ═══

    def _build_context() -> str | None:
        """构建当前话题的上下文，注入 Agent"""
        topic = current_topic["name"]
        if topic == "默认":
            return None

        parts = [f"[当前研究话题: {topic}]\n"]

        # 关联论文
        if agent.paper_store:
            papers = agent.paper_store.list_papers()
            if papers:
                names = [p["title"] for p in papers[:5]]
                parts.append(f"已索引论文 ({len(papers)} 篇): " + ", ".join(names))

        # 关联笔记
        topic_notes = notes.list_notes(topic)
        if topic_notes:
            recent = topic_notes[-3:]  # 最近 3 条
            parts.append("\n最近笔记:")
            for n in recent:
                preview = n["content"].replace("\n", " ")[:100]
                parts.append(f"  - {preview}")

        return "\n".join(parts)

    def refresh_status(history, session_id: str | None = None):
        session_id = session_id or agent.thread_id
        tu = agent.get_usage(session_id)
        rag = f"{agent.paper_store.paper_count} 篇" if agent.paper_store else "关"
        last = tu.get("last_prompt", 0)
        limit = tu.get("context_limit", 131072)
        limit_k = f"{limit//1000000}M" if limit >= 1000000 else f"{limit//1000}K"
        pct = min(round(last / limit * 100), 99) if last else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        vs = tu.get("verify_status", "")
        vs_str = f"**{vs}** | " if vs else ""
        api = tu.get("api_status", "")
        api_str = f"**{api}** | " if api else ""
        daily = agent.runtime_status.get("daily_progress", "")
        daily_str = f"**{daily}** | " if daily else ""
        ready = agent.runtime_status.get("daily_ready", False)
        ready_str = "**📰 结果就绪** | " if ready else ""
        bw = tu.get("budget_warning", "")
        bw_str = f"**{bw}** | " if bw else ""
        return (
            f"**模型**: {agent.model} | "
            f"**上下文**: `{bar}` {pct}% ({last:,}/{limit_k}) | "
            f"{api_str}"
            f"{vs_str}"
            f"{daily_str}"
            f"{ready_str}"
            f"{bw_str}"
            f"**Tokens**: {tu['total']:,} | "
            f"**话题**: {current_topic['name']} | "
            f"**RAG**: {rag}"
        )

    def refresh_daily_panel() -> str:
        """后台任务结果独立展示，避免被写入任意一个会话的聊天记录。"""
        message = agent.runtime_status.get("daily_message", "")
        progress = agent.runtime_status.get("daily_progress", "")
        if message:
            return f"### 📰 每日检索任务\n\n{message}\n\n{progress}"
        return ""

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
            return [], [], agent.thread_id, refresh_status([], agent.thread_id)
        history = agent.get_history(session_id)
        return history, history, session_id, refresh_status(history, session_id)

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

    # ═══ 笔记面板函数 ═══

    def note_topic_list():
        topics = notes.list_topics()
        return gr.update(choices=[t["name"] for t in topics], value=current_topic["name"])

    def note_save(topic_name: str, content: str):
        if not content.strip():
            return gr.update(), note_list_html(topic_name)
        notes.add(topic_name, content.strip())
        return gr.update(value=""), note_list_html(topic_name)

    def note_list_html(topic_name: str = None) -> str:
        t = topic_name or current_topic["name"]
        all_notes = notes.list_notes(t)
        if not all_notes:
            return f"<p style='color:#999;'>📭 话题「{t}」还没有笔记</p>"
        html = "<div style='max-height:300px;overflow-y:auto;'>"
        for n in all_notes:
            content = n["content"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html += (
                f"<div style='border-bottom:1px solid #eee;padding:6px 0;'>"
                f"<small style='color:#999;'>{n['created_at'][:16]}</small>"
                f"<p style='margin:2px 0;white-space:pre-wrap;'>{content}</p>"
                f"</div>"
            )
        html += "</div>"
        return html

    def note_topic_change(topic_name: str):
        current_topic["name"] = topic_name
        return note_list_html(topic_name)

    # ═══ 论文卡片 ───

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

    with gr.Blocks(title="🔬 Research Assistant") as demo:
        # gr.State 是浏览器请求级状态，避免两个标签页切换到彼此的会话。
        session_state = gr.State(value=agent.thread_id)

        gr.HTML(
            '<div class="main-header">'
            '<h1>🔬 Research Assistant</h1>'
            '</div>'
        )

        with gr.Row():
            status = gr.Markdown(refresh_status([], agent.thread_id), elem_classes=["status-bar"], scale=20)
            quit_btn = gr.Button("⏻ 退出", scale=1, size="sm", min_width=60, elem_classes=["quit-btn"])
        daily_panel = gr.Markdown(refresh_daily_panel())
        daily_timer = gr.Timer(value=1.0)
        daily_timer.tick(fn=refresh_daily_panel, outputs=[daily_panel], show_progress="hidden")

        with gr.Tabs():
            # ── Tab 1: 对话 ──
            with gr.Tab("💬 对话"):
                with gr.Row():
                    session_picker = gr.Dropdown(
                        label="会话", choices=_session_choices(), value=agent.thread_id,
                        scale=5, interactive=True,
                    )
                    new_session_btn = gr.Button("＋ 新建会话", variant="primary", scale=1)
                    delete_confirm = gr.Checkbox(
                        label="确认永久删除当前会话", value=False, scale=2,
                    )
                    delete_session_btn = gr.Button("删除会话", variant="stop", scale=1)
                # 拖拽覆盖层（拖文件时显示）
                gr.HTML("""
                <div id="drop-overlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
                background:rgba(0,0,0,0.5);z-index:9999;align-items:center;justify-content:center;">
                <div style="background:#fff;padding:40px 80px;border-radius:16px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.2);">
                <p style="font-size:1.8rem;margin:0;">📥 释放文件以上传</p><p style="color:#999;margin-top:8px;">PDF、PNG、JPG</p></div></div>
                """)
                # 隐藏的文件上传组件（拖拽释放后接收文件）
                file_upload = gr.File(
                    label="", file_types=[".pdf", ".png", ".jpg", ".jpeg"],
                    visible=False, elem_id="drop-file-input",
                )
                # 受控聊天状态：不使用 ChatInterface 的私有 chatbot_state，
                # 会话切换后的可见历史与请求历史只有同一个 browser State。
                initial_history = agent.get_history(agent.thread_id)
                chat_history_state = gr.State(value=initial_history)
                chatbot = gr.Chatbot(
                    value=initial_history,
                    height=420,
                    render_markdown=True,
                    latex_delimiters=CHAT_LATEX_DELIMITERS,
                )
                with gr.Row():
                    chat_input = gr.MultimodalTextbox(
                        placeholder="输入问题或命令... 可拖拽/粘贴文件",
                        container=False, scale=7,
                        file_types=[".pdf", ".png", ".jpg", ".jpeg"],
                        submit_btn="发送", stop_btn=False,
                    )
                    stop_reply_btn = gr.Button("停止", variant="stop", scale=1)
                with gr.Row():
                    research_scope = gr.Dropdown(
                        label="深度研究来源",
                        choices=[
                            ("本地论文 + 公开文献", "both"),
                            ("仅本地论文", "local"),
                            ("仅公开文献", "public"),
                        ],
                        value="both", scale=3,
                    )
                    deep_research_btn = gr.Button("🧭 深度研究", variant="secondary", scale=2)
                    continue_research_btn = gr.Button("继续上次研究", scale=2)
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

            # ── Tab 2: 论文库 ──
            with gr.Tab("📚 论文库"):
                gr.Markdown("### 已索引论文")
                paper_list = gr.HTML(value=paper_cards_html())
                with gr.Row():
                    refresh_papers_btn = gr.Button("🔄 刷新", scale=1)
                refresh_papers_btn.click(
                    fn=lambda: paper_cards_html(), outputs=[paper_list],
                )

            # ── Tab 3: 笔记 ──
            with gr.Tab("📝 笔记"):
                with gr.Row():
                    topic_dd = gr.Dropdown(
                        label="话题", scale=2, allow_custom_value=True,
                        choices=[t["name"] for t in notes.list_topics()],
                        value=current_topic["name"],
                    )
                    note_input = gr.Textbox(
                        label="新笔记", placeholder="输入笔记内容...",
                        lines=2, scale=5,
                    )
                    note_save_btn = gr.Button("保存", scale=1, variant="primary")
                note_display = gr.HTML(value=note_list_html(current_topic["name"]))
                note_save_btn.click(
                    fn=note_save, inputs=[topic_dd, note_input],
                    outputs=[note_input, note_display],
                )
                topic_dd.change(
                    fn=note_topic_change, inputs=[topic_dd],
                    outputs=[note_display],
                )
                demo.load(fn=note_topic_list, outputs=[topic_dd])

            # ── Tab 4: 设置 ──
            with gr.Tab("⚙ 设置"):
                gr.Markdown("### 基础设置")
                model_dd = gr.Dropdown(
                    label="模型", value=cfg.model,
                    choices=["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
                    allow_custom_value=True,
                )
                rag_toggle = gr.Checkbox(label="启用 RAG 论文库", value=cfg.rag_enabled)
                max_pages_slider = gr.Slider(label="PDF 最大页数", minimum=5, maximum=100, step=5, value=cfg.pdf_max_pages)
                daily_enabled = gr.Checkbox(label="启用每日自动检索", value=cfg.daily_search_enabled)
                save_cfg_btn = gr.Button("💾 保存配置", variant="primary")
                cfg_msg = gr.Markdown("")
                save_cfg_btn.click(fn=save_config, inputs=[model_dd, rag_toggle, max_pages_slider, daily_enabled], outputs=[cfg_msg])

                gr.Markdown("### 📰 每日论文检索")
                kw_list = [k["keyword"] for k in scheduler.list_keywords()] if scheduler.list_keywords() else []
                kw_dd = gr.Dropdown(label="已有关键词", choices=kw_list, value=kw_list[0] if kw_list else None, interactive=True)
                kw_input = gr.Textbox(label="新关键词", placeholder="例如: TSN scheduling reinforcement learning")
                kw_add_btn = gr.Button("➕ 添加关键词", scale=1)
                kw_del_btn = gr.Button("🗑 删除选中", scale=1)
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

        quit_btn.click(fn=lambda: (demo.close(), os._exit(0)), outputs=[])

    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=False, show_error=True,
        theme=gr.themes.Soft(),
        css="""
        .main-header { text-align: center; padding: 0.5rem 0; }
        .main-header h1 { font-size: 1.3rem; font-weight: 600; }
        .status-bar { padding: 0.3rem 1rem; font-size: 0.75rem; color: #666; }
        footer { display: none !important; }
        .quit-btn { margin-top: -2px; min-width: 60px !important; max-width: 70px !important; }
        #drop-overlay { display: none !important; }
        #drop-overlay.show { display: flex !important; }
        """,
        js="""
        function() {
            var overlay = document.getElementById('drop-overlay');
            var dragCount = 0;
            document.addEventListener('dragenter', function(e) { e.preventDefault(); dragCount++; overlay.classList.add('show'); });
            document.addEventListener('dragleave', function(e) { e.preventDefault(); dragCount--; if (dragCount <= 0) { dragCount = 0; overlay.classList.remove('show'); } });
            document.addEventListener('dragover', function(e) { e.preventDefault(); });
            document.addEventListener('drop', function(e) { e.preventDefault(); dragCount = 0; overlay.classList.remove('show'); });
        }
        """,
    )


if __name__ == "__main__":
    build_ui()

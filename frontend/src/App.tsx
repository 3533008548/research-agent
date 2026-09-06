import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import {
  cancelRun,
  createDailyKeyword,
  createRun,
  createSession,
  deleteDailyKeyword,
  deleteSession,
  downloadResearchDocument,
  getResearchDocument,
  getRun,
  getSessionMessages,
  getWorkspaceSettings,
  listDailyKeywords,
  listPapers,
  listResearchDocuments,
  listSessions,
  streamRun,
  updateWorkspaceSettings,
} from "./api";
import {
  ApiError,
  type DailyKeyword,
  type Message,
  type Paper,
  type ResearchDocument,
  type ResearchDocumentDetail,
  type Run,
  type RunEvent,
  type Session,
  type WorkspaceSettings,
} from "./types";

const MarkdownContent = lazy(() => import("./MarkdownContent"));

const API_TOKEN_KEY = "research-agent.api-token";
const ACTIVE_SESSION_KEY = "research-agent.active-session";

type ResearchMode = "chat" | "research";
type WorkspacePanel = "documents" | "papers" | "keywords" | "settings";
type Notice = { kind: "error" | "info"; text: string } | null;
type DisplayMessage = Message & { id: string; streaming?: boolean };
type WorkbenchTab =
  | { id: "chat"; kind: "chat"; title: string }
  | { id: string; kind: "document"; title: string; document: ResearchDocumentDetail };

const TOOL_LABELS: Record<string, string> = {
  search_papers: "检索论文",
  read_pdf: "读取 PDF",
  describe_image: "理解图片",
  paper_card: "生成论文卡片",
  query_papers: "检索本地论文库",
};

function userFacingError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status === 401
      ? "需要 API 令牌。请在左下角填入 API_AUTH_TOKEN 后重试。"
      : error.message;
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "事件流已停止。";
  }
  return "连接服务失败，请确认 FastAPI 服务正在运行。";
}

function displayMessages(messages: Message[]): DisplayMessage[] {
  return messages.map((message, index) => ({
    ...message,
    id: `${index}-${message.role}`,
  }));
}

function toolLabel(name: string | undefined): string {
  if (!name) {
    return "工具调用";
  }
  return TOOL_LABELS[name] || name;
}

function statusLabel(status: string | undefined): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    cancelling: "正在取消",
    completed: "已完成",
    cancelled: "已取消",
    failed: "执行失败",
  };
  return labels[status || ""] || status || "等待开始";
}

function modeLabel(mode: ResearchMode): string {
  return mode === "research" ? "深度研究" : "对话";
}

function durationLabel(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "";
  }
  return value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${Math.round(value)} 毫秒`;
}

function appendRunEvent(events: RunEvent[], event: RunEvent): RunEvent[] {
  // Token events belong to the chat transcript, not the operational timeline.
  // Redis also emits an initial state and its queued copy, so adjacent identical
  // state transitions add no information to the inspector.
  if (event.type === "token") {
    return events;
  }
  const previous = events.at(-1);
  const isRepeatedStatus = event.type === "status"
    && previous?.type === "status"
    && previous.status === event.status
    && previous.stage === event.stage;
  const isRepeatedCompletion = event.type === "done"
    && previous?.type === "done"
    && previous.status === event.status;
  return isRepeatedStatus || isRepeatedCompletion ? events : [...events, event];
}

function ChatMessage({ message }: { message: DisplayMessage }) {
  return (
    <article className={`message message-${message.role}`}>
      <div className="message-avatar" aria-hidden="true">
        {message.role === "user" ? "你" : "研"}
      </div>
      <div className="message-body">
        <p className="message-role">{message.role === "user" ? "你" : "Research Agent"}</p>
        {message.content ? (
          <Suspense fallback={<p className="markdown-loading">{message.content}</p>}>
            <MarkdownContent content={message.content} />
          </Suspense>
        ) : (
          <p className="streaming-placeholder">正在生成回答<span aria-hidden="true">…</span></p>
        )}
      </div>
    </article>
  );
}

function WorkspacePanel({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className={`workspace-panel ${open ? "open" : ""}`}>
      <button className="workspace-panel-toggle" type="button" onClick={onToggle} aria-expanded={open}>
        <span>{title}</span>
        <span className="workspace-panel-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && <div className="workspace-panel-content">{children}</div>}
    </section>
  );
}

function RunInspector({
  run,
  events,
  onCancel,
  cancelling,
}: {
  run: Run | null;
  events: RunEvent[];
  onCancel: () => void;
  cancelling: boolean;
}) {
  return (
    <aside className="run-inspector" aria-label="运行检查器">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">运行检查器</p>
          <h2>本轮任务</h2>
        </div>
        {run && <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>}
      </div>

      {!run ? (
        <p className="empty-panel">开始一项对话或深度研究后，这里会显示脱敏的运行状态和工具活动。</p>
      ) : (
        <>
          <dl className="run-summary">
            <div><dt>类型</dt><dd>{run.kind === "research" ? "深度研究" : "对话"}</dd></div>
            <div><dt>耗时</dt><dd>{durationLabel(run.duration_ms) || "进行中"}</dd></div>
            {run.model && <div><dt>模型</dt><dd>{run.model}</dd></div>}
          </dl>
          {!["completed", "cancelled", "failed"].includes(run.status) && (
            <button className="button button-secondary button-full" onClick={onCancel} disabled={cancelling}>
              {cancelling ? "正在请求取消…" : "停止本轮任务"}
            </button>
          )}
          <ol className="run-events">
            {events.map((event, index) => (
              <li key={`${event.type}-${index}`} className="run-event">
                <span className={`event-dot event-${event.type}`} aria-hidden="true" />
                <div>
                  <strong>
                    {event.type === "tool" ? toolLabel(event.tool) : statusLabel(event.status)}
                  </strong>
                  <p>
                    {event.type === "tool"
                      ? `工具${event.status === "completed" ? "已完成" : "正在执行"}${durationLabel(event.duration_ms) ? ` · ${durationLabel(event.duration_ms)}` : ""}`
                      : event.type === "error"
                        ? event.error_type || "运行出错"
                        : event.stage || (event.type === "done" ? "已生成最终结果" : "任务状态更新")}
                  </p>
                </div>
              </li>
            ))}
          </ol>
          {run.error_type && <p className="run-error">错误类型：{run.error_type}</p>}
        </>
      )}
    </aside>
  );
}

export function App() {
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem(API_TOKEN_KEY) || "");
  const [apiKeyDraft, setApiKeyDraft] = useState(() => sessionStorage.getItem(API_TOKEN_KEY) || "");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    () => localStorage.getItem(ACTIVE_SESSION_KEY),
  );
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<ResearchMode>("chat");
  const [currentRun, setCurrentRun] = useState<Run | null>(null);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSending, setIsSending] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [openWorkspacePanel, setOpenWorkspacePanel] = useState<WorkspacePanel | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState<WorkspacePanel | null>(null);
  const [documents, setDocuments] = useState<ResearchDocument[]>([]);
  const [workbenchTabs, setWorkbenchTabs] = useState<WorkbenchTab[]>([
    { id: "chat", kind: "chat", title: "对话" },
  ]);
  const [activeWorkbenchTabId, setActiveWorkbenchTabId] = useState("chat");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [keywords, setKeywords] = useState<DailyKeyword[]>([]);
  const [keywordDraft, setKeywordDraft] = useState("");
  const [workspaceSettings, setWorkspaceSettings] = useState<WorkspaceSettings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState({
    model: "",
    rag_enabled: true,
    pdf_max_pages: 15,
    daily_search_enabled: false,
  });
  const abortRef = useRef<AbortController | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const activeWorkbenchTab = workbenchTabs.find((tab) => tab.id === activeWorkbenchTabId) || workbenchTabs[0];
  const selectedDocumentId = activeWorkbenchTab?.kind === "document"
    ? activeWorkbenchTab.document.document_id
    : null;

  useEffect(() => {
    const list = messageListRef.current;
    if (list) {
      list.scrollTop = list.scrollHeight;
    }
  }, [messages]);

  const loadSession = useCallback(async (sessionId: string) => {
    const history = await getSessionMessages(apiKey, sessionId);
    setActiveSessionId(sessionId);
    localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    setMessages(displayMessages(history));
    setCurrentRun(null);
    setRunEvents([]);
  }, [apiKey]);

  const loadWorkspace = useCallback(async () => {
    setIsLoading(true);
    try {
      const nextSessions = await listSessions(apiKey);
      setSessions(nextSessions);
      const remembered = localStorage.getItem(ACTIVE_SESSION_KEY);
      const selected = nextSessions.find((item) => item.thread_id === remembered) || nextSessions[0];
      if (selected) {
        await loadSession(selected.thread_id);
      } else {
        setActiveSessionId(null);
        setMessages([]);
      }
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
      setSessions([]);
      setMessages([]);
    } finally {
      setIsLoading(false);
    }
  }, [apiKey, loadSession]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  async function toggleWorkspacePanel(panel: WorkspacePanel) {
    if (openWorkspacePanel === panel) {
      setOpenWorkspacePanel(null);
      return;
    }
    setOpenWorkspacePanel(panel);
    setWorkspaceLoading(panel);
    try {
      if (panel === "documents") {
        setDocuments(await listResearchDocuments(apiKey));
      } else if (panel === "papers") {
        setPapers(await listPapers(apiKey));
      } else if (panel === "keywords") {
        setKeywords(await listDailyKeywords(apiKey));
      } else {
        const settings = await getWorkspaceSettings(apiKey);
        setWorkspaceSettings(settings);
        setSettingsDraft({
          model: settings.model,
          rag_enabled: settings.rag_enabled,
          pdf_max_pages: settings.pdf_max_pages,
          daily_search_enabled: settings.daily_search_enabled,
        });
      }
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setWorkspaceLoading(null);
    }
  }

  async function selectResearchDocument(documentId: string) {
    try {
      const document = await getResearchDocument(apiKey, documentId);
      const tabId = `document:${document.document_id}`;
      setWorkbenchTabs((current) => {
        const existing = current.find((tab) => tab.id === tabId);
        if (existing?.kind === "document") {
          return current.map((tab) => tab.id === tabId ? { ...tab, title: document.title, document } : tab);
        }
        return [...current, { id: tabId, kind: "document", title: document.title, document }];
      });
      setActiveWorkbenchTabId(tabId);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  function closeWorkbenchTab(tabId: string) {
    if (tabId === "chat") {
      return;
    }
    const closingIndex = workbenchTabs.findIndex((tab) => tab.id === tabId);
    const fallbackTabId = workbenchTabs[closingIndex - 1]?.id || "chat";
    setWorkbenchTabs((current) => current.filter((tab) => tab.id !== tabId));
    setActiveWorkbenchTabId((current) => current === tabId ? fallbackTabId : current);
  }

  async function downloadDocument(document: ResearchDocumentDetail) {
    try {
      await downloadResearchDocument(apiKey, document);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function addDailyKeyword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const keyword = keywordDraft.trim();
    if (!keyword) {
      return;
    }
    try {
      await createDailyKeyword(apiKey, keyword);
      setKeywords(await listDailyKeywords(apiKey));
      setKeywordDraft("");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function removeDailyKeyword(keyword: string) {
    if (!window.confirm(`删除每日检索关键词“${keyword}”？`)) {
      return;
    }
    try {
      await deleteDailyKeyword(apiKey, keyword);
      setKeywords((current) => current.filter((item) => item.keyword !== keyword));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function saveWorkspaceSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const settings = await updateWorkspaceSettings(apiKey, settingsDraft);
      setWorkspaceSettings(settings);
      setSettingsDraft({
        model: settings.model,
        rag_enabled: settings.rag_enabled,
        pdf_max_pages: settings.pdf_max_pages,
        daily_search_enabled: settings.daily_search_enabled,
      });
      setNotice({ kind: "info", text: "设置已保存；重启服务后生效。" });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function selectSession(sessionId: string) {
    if (isSending || sessionId === activeSessionId) {
      return;
    }
    try {
      await loadSession(sessionId);
      setActiveWorkbenchTabId("chat");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startSession() {
    if (isSending) {
      return;
    }
    try {
      const session = await createSession(apiKey);
      setSessions((current) => [session, ...current]);
      await loadSession(session.thread_id);
      setActiveWorkbenchTabId("chat");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function removeActiveSession() {
    if (!activeSessionId || isSending || !window.confirm("确定删除当前会话吗？此操作会同时删除该会话的运行记录。")) {
      return;
    }
    try {
      await deleteSession(apiKey, activeSessionId);
      const remaining = sessions.filter((item) => item.thread_id !== activeSessionId);
      setSessions(remaining);
      if (remaining[0]) {
        await loadSession(remaining[0].thread_id);
      } else {
        localStorage.removeItem(ACTIVE_SESSION_KEY);
        setActiveSessionId(null);
        setMessages([]);
        setCurrentRun(null);
        setRunEvents([]);
      }
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  function appendToken(text: string) {
    setMessages((current) => {
      const last = current.at(-1);
      if (last?.role !== "assistant" || !last.streaming) {
        return [...current, { id: `stream-${Date.now()}`, role: "assistant", content: text, streaming: true }];
      }
      return [...current.slice(0, -1), { ...last, content: last.content + text }];
    });
  }

  function completeAnswer(answer: string | undefined) {
    setMessages((current) => {
      const last = current.at(-1);
      if (last?.role === "assistant" && last.streaming) {
        return [...current.slice(0, -1), { ...last, content: answer ?? last.content, streaming: false }];
      }
      return current;
    });
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || isSending) {
      return;
    }
    if (!activeSessionId) {
      setNotice({ kind: "info", text: "请先新建一个会话。" });
      return;
    }

    const requestMode = mode;
    setDraft("");
    setIsSending(true);
    setNotice(null);
    setRunEvents([]);
    setMessages((current) => [
      ...current,
      { id: `user-${Date.now()}`, role: "user", content },
      { id: `assistant-${Date.now()}`, role: "assistant", content: "", streaming: true },
    ]);

    try {
      const started = await createRun(apiKey, requestMode, activeSessionId, content);
      setCurrentRun({
        run_id: started.run_id,
        kind: started.kind,
        session_id: started.session_id,
        status: started.status,
        model: "",
        answer: "",
        duration_ms: null,
        metrics: {},
        error_type: "",
        created_at: "",
        updated_at: "",
        completed_at: null,
        events: [],
      });
      const controller = new AbortController();
      abortRef.current = controller;
      await streamRun(apiKey, started.stream_url, (runEvent) => {
        setRunEvents((current) => appendRunEvent(current, runEvent));
        if (runEvent.type === "token") {
          appendToken(runEvent.text || "");
        }
        if (runEvent.type === "status" || runEvent.type === "done") {
          setCurrentRun((current) => current && { ...current, status: runEvent.status || current.status });
        }
        if (runEvent.type === "done") {
          completeAnswer(runEvent.answer);
        }
        if (runEvent.type === "error") {
          completeAnswer("运行未能完成，请查看右侧错误信息。");
          setCurrentRun((current) => current && { ...current, status: "failed", error_type: runEvent.error_type || "RunError" });
        }
      }, controller.signal);
      const detail = await getRun(apiKey, started.run_id);
      setCurrentRun(detail);
      completeAnswer(detail.answer || undefined);
      const refreshedSessions = await listSessions(apiKey);
      setSessions(refreshedSessions);
    } catch (error) {
      const message = userFacingError(error);
      completeAnswer(`⚠️ ${message}`);
      setNotice({ kind: "error", text: message });
      setCurrentRun((current) => current && { ...current, status: "failed" });
    } finally {
      abortRef.current = null;
      setIsSending(false);
    }
  }

  async function stopCurrentRun() {
    if (!currentRun || !isSending) {
      return;
    }
    try {
      const cancelled = await cancelRun(apiKey, currentRun.run_id);
      setCurrentRun((current) => current && { ...current, status: cancelled.status });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  function applyApiKey() {
    const nextKey = apiKeyDraft.trim();
    if (nextKey) {
      sessionStorage.setItem(API_TOKEN_KEY, nextKey);
    } else {
      sessionStorage.removeItem(API_TOKEN_KEY);
    }
    setApiKey(nextKey);
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/app/" aria-label="Research Agent 首页">
          <span className="brand-mark" aria-hidden="true">R</span>
          <span>Research Agent</span>
        </a>
        <p>论文证据、研究推理与可追溯运行</p>
        <a className="legacy-link" href="/">旧版界面</a>
      </header>

      <div className="workspace">
        <aside className="session-sidebar" aria-label="会话列表">
          <div className="sidebar-heading">
            <div><p className="eyebrow">工作区</p><h2>会话</h2></div>
            <button className="icon-button" onClick={() => void startSession()} disabled={isSending} aria-label="新建会话">＋</button>
          </div>
          <nav className="session-list">
            {sessions.map((session) => (
              <button
                key={session.thread_id}
                className={`session-item ${session.thread_id === activeSessionId ? "active" : ""}`}
                onClick={() => void selectSession(session.thread_id)}
                disabled={isSending}
              >
                <strong>{session.title}</strong>
                <span>{session.preview || "尚未开始"}</span>
              </button>
            ))}
          </nav>
          {activeSessionId && (
            <button className="text-button danger" onClick={() => void removeActiveSession()} disabled={isSending}>
              删除当前会话
            </button>
          )}
          <div className="sidebar-utilities" aria-label="工作区工具">
            <WorkspacePanel
              title="科研档案"
              open={openWorkspacePanel === "documents"}
              onToggle={() => void toggleWorkspacePanel("documents")}
            >
              {workspaceLoading === "documents" ? (
                <p className="panel-hint">正在读取档案…</p>
              ) : documents.length ? (
                <div className="workspace-list">
                  {documents.map((document) => (
                    <button
                      key={document.document_id}
                      className={`workspace-list-item ${selectedDocumentId === document.document_id ? "selected" : ""}`}
                      type="button"
                      onClick={() => void selectResearchDocument(document.document_id)}
                      title={document.title}
                    >
                      <strong>{document.title}</strong>
                      <span>{document.summary || "未填写摘要"}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="panel-hint">暂无档案。可在对话中要求 Agent 保存研究方案。</p>
              )}
            </WorkspacePanel>

            <WorkspacePanel
              title="论文库"
              open={openWorkspacePanel === "papers"}
              onToggle={() => void toggleWorkspacePanel("papers")}
            >
              {workspaceLoading === "papers" ? (
                <p className="panel-hint">正在读取论文库…</p>
              ) : papers.length ? (
                <div className="workspace-list">
                  {papers.map((paper) => (
                    <div className="workspace-list-item paper-item" key={paper.paper_id}>
                      <strong>{paper.title}</strong>
                      <span>{paper.chunks} 个检索片段</span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="panel-hint">本地论文库为空。</p>
              )}
            </WorkspacePanel>

            <WorkspacePanel
              title="每日检索关键词"
              open={openWorkspacePanel === "keywords"}
              onToggle={() => void toggleWorkspacePanel("keywords")}
            >
              {workspaceLoading === "keywords" ? (
                <p className="panel-hint">正在读取关键词…</p>
              ) : (
                <>
                  <form className="keyword-form" onSubmit={(event) => void addDailyKeyword(event)}>
                    <input
                      value={keywordDraft}
                      onChange={(event) => setKeywordDraft(event.target.value)}
                      placeholder="例如：TSN scheduling"
                      aria-label="新关键词"
                    />
                    <button className="button button-secondary" type="submit" disabled={!keywordDraft.trim()}>添加</button>
                  </form>
                  {keywords.length ? (
                    <div className="keyword-list">
                      {keywords.map((item) => (
                        <div className="keyword-item" key={item.keyword}>
                          <span>{item.keyword}</span>
                          <button className="text-button danger" type="button" onClick={() => void removeDailyKeyword(item.keyword)}>删除</button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="panel-hint">尚未设置每日检索关键词。</p>
                  )}
                </>
              )}
            </WorkspacePanel>

            <WorkspacePanel
              title="设置"
              open={openWorkspacePanel === "settings"}
              onToggle={() => void toggleWorkspacePanel("settings")}
            >
              {workspaceLoading === "settings" ? (
                <p className="panel-hint">正在读取设置…</p>
              ) : (
                <form className="settings-form" onSubmit={(event) => void saveWorkspaceSettings(event)}>
                  <label>模型
                    <input value={settingsDraft.model} onChange={(event) => setSettingsDraft((current) => ({ ...current, model: event.target.value }))} />
                  </label>
                  <label>PDF 最大页数
                    <input type="number" min="5" max="100" value={settingsDraft.pdf_max_pages} onChange={(event) => setSettingsDraft((current) => ({ ...current, pdf_max_pages: Number(event.target.value) || 5 }))} />
                  </label>
                  <label className="checkbox-label">
                    <input type="checkbox" checked={settingsDraft.rag_enabled} onChange={(event) => setSettingsDraft((current) => ({ ...current, rag_enabled: event.target.checked }))} />
                    启用 RAG 论文库
                  </label>
                  <label className="checkbox-label">
                    <input type="checkbox" checked={settingsDraft.daily_search_enabled} onChange={(event) => setSettingsDraft((current) => ({ ...current, daily_search_enabled: event.target.checked }))} />
                    启用每日自动检索
                  </label>
                  <button className="button button-primary" type="submit">保存设置</button>
                  {workspaceSettings?.restart_required && <p className="panel-hint">保存后重启服务生效。</p>}
                </form>
              )}
              <div className="api-key-panel">
                <label htmlFor="api-key">API 令牌（可选）</label>
                <div className="api-key-row">
                  <input
                    id="api-key"
                    type="password"
                    autoComplete="off"
                    value={apiKeyDraft}
                    onChange={(event) => setApiKeyDraft(event.target.value)}
                    placeholder="API_AUTH_TOKEN"
                  />
                  <button className="button button-secondary" type="button" onClick={applyApiKey}>应用</button>
                </div>
                <p>仅保存在当前浏览器标签页中。</p>
              </div>
            </WorkspacePanel>
          </div>
        </aside>

        <section className="conversation" aria-label="研究工作台">
          <div className="workbench-tabs" role="tablist" aria-label="工作台页面">
            {workbenchTabs.map((tab) => (
              <div className={`workbench-tab ${tab.id === activeWorkbenchTab?.id ? "active" : ""}`} key={tab.id}>
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab.id === activeWorkbenchTab?.id}
                  onClick={() => setActiveWorkbenchTabId(tab.id)}
                  title={tab.title}
                >
                  {tab.kind === "document" && <span aria-hidden="true">▣</span>}
                  <span>{tab.title}</span>
                </button>
                {tab.kind !== "chat" && (
                  <button
                    className="workbench-tab-close"
                    type="button"
                    onClick={() => closeWorkbenchTab(tab.id)}
                    aria-label={`关闭 ${tab.title}`}
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>

          {notice && <p className={`notice notice-${notice.kind}`}>{notice.text}</p>}
          {activeWorkbenchTab?.kind === "document" ? (
            <article className="document-page" aria-label={`科研档案：${activeWorkbenchTab.document.title}`}>
              <header className="document-page-heading">
                <div>
                  <p className="eyebrow">科研档案</p>
                  <h1>{activeWorkbenchTab.document.title}</h1>
                  <p className="document-page-meta">
                    修订 {activeWorkbenchTab.document.revision} · 更新于 {activeWorkbenchTab.document.updated_at || activeWorkbenchTab.document.created_at}
                  </p>
                </div>
                <button className="button button-secondary" type="button" onClick={() => void downloadDocument(activeWorkbenchTab.document)}>
                  下载 Word
                </button>
              </header>
              <div className="document-page-content">
                <Suspense fallback={<p className="markdown-loading">{activeWorkbenchTab.document.content}</p>}>
                  <MarkdownContent content={activeWorkbenchTab.document.content} />
                </Suspense>
              </div>
            </article>
          ) : (
            <>
              <div className="conversation-heading">
                <div>
                  <p className="eyebrow">研究工作台</p>
                  <h1>{activeSessionId ? "开始一项研究任务" : "新建会话以开始"}</h1>
                </div>
                <div className="mode-switch" aria-label="任务类型">
                  {(["chat", "research"] as const).map((item) => (
                    <button
                      key={item}
                      className={mode === item ? "selected" : ""}
                      onClick={() => setMode(item)}
                      disabled={isSending}
                    >
                      {modeLabel(item)}
                    </button>
                  ))}
                </div>
              </div>

              <div className="message-list" ref={messageListRef} aria-live="polite">
                {isLoading ? (
                  <p className="empty-state">正在加载会话…</p>
                ) : messages.length ? (
                  messages.map((message) => <ChatMessage key={message.id} message={message} />)
                ) : (
                  <div className="empty-state">
                    <h2>从一个研究问题、论文或假设开始。</h2>
                    <p>对话用于快速讨论；深度研究会规划并收集本地与公开证据。</p>
                  </div>
                )}
              </div>

              <form className="composer" onSubmit={(event) => void sendMessage(event)}>
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder={mode === "research" ? "输入研究问题，开始深度研究…" : "提出问题，开始对话…"}
                  rows={3}
                  disabled={isSending || !activeSessionId}
                />
                <div className="composer-footer">
                  <span>{mode === "research" ? "将检索本地论文与公开文献" : "支持引用、公式与 Markdown"}</span>
                  <button className="button button-primary" type="submit" disabled={isSending || !draft.trim() || !activeSessionId}>
                    {isSending ? "生成中…" : mode === "research" ? "开始研究" : "发送"}
                  </button>
                </div>
              </form>
            </>
          )}
        </section>

        <RunInspector run={currentRun} events={runEvents} onCancel={() => void stopCurrentRun()} cancelling={isSending} />
      </div>
    </main>
  );
}

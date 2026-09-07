import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";

import {
  cancelRun,
  createBadcase,
  createDailyRun,
  createDailyKeyword,
  createRun,
  createSession,
  deleteDailyKeyword,
  deleteSession,
  downloadResearchDocument,
  getDailyRunDetail,
  getResearchDocument,
  getRun,
  getSessionMessages,
  getSessionUsage,
  getDailyDigest,
  getWorkspaceSettings,
  listDailyKeywords,
  listDailyRuns,
  listBadcases,
  listPapers,
  listResearchDocuments,
  listSessionRuns,
  listSessions,
  streamRun,
  updateDailyPaperStatus,
  updateWorkspaceSettings,
  uploadWorkspaceFile,
} from "./api";
import {
  ApiError,
  type BadcaseCandidate,
  type BadcaseCategory,
  type DailyDigest,
  type DailyKeyword,
  type DailyPaper,
  type DailyRunDetail,
  type DailyRunKind,
  type Message,
  type Paper,
  type ResearchDocument,
  type ResearchDocumentDetail,
  type ResearchScope,
  type Run,
  type RunEvent,
  type Session,
  type SessionUsage,
  type WorkspaceSettings,
  type WorkspaceUpload,
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
  | { id: string; kind: "document"; title: string; document: ResearchDocumentDetail }
  | { id: "daily"; kind: "daily"; title: string }
  | { id: string; kind: "dailyRun"; title: string; detail: DailyRunDetail }
  | { id: "runs"; kind: "runs"; title: string };

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
    partial_failed: "部分完成",
  };
  return labels[status || ""] || status || "等待开始";
}

function modeLabel(mode: ResearchMode): string {
  return mode === "research" ? "深度研究" : "对话";
}

function dailyPaperStatusLabel(status: DailyPaper["status"]): string {
  return ({ new: "未处理", want_read: "想读", read: "已读", skipped: "已跳过" })[status];
}

function badcaseCategoryLabel(category: BadcaseCategory): string {
  return ({
    retrieval_miss: "召回遗漏",
    citation_quality: "引用质量",
    answer_quality: "回答质量",
    tool_failure: "工具失败",
    performance: "性能",
    safety: "安全",
    other: "其他",
  })[category];
}

function dailyRunTitle(detail: DailyRunDetail): string {
  const date = detail.created_at ? detail.created_at.slice(0, 10) : "未命名";
  return `每日检索 · ${date}`;
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
  onOpenRunCenter,
  cancelling,
}: {
  run: Run | null;
  events: RunEvent[];
  onCancel: () => void;
  onOpenRunCenter: () => void;
  cancelling: boolean;
}) {
  return (
    <aside className="run-inspector" aria-label="运行检查器">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">运行检查器</p>
          <h2>本轮任务</h2>
        </div>
        <div className="run-inspector-actions">
          <button className="text-button" type="button" onClick={onOpenRunCenter}>历史运行</button>
          {run && <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>}
        </div>
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
  const [sessionUsage, setSessionUsage] = useState<SessionUsage | null>(null);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<ResearchMode>("chat");
  const [researchScope, setResearchScope] = useState<ResearchScope>("both");
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
  const [attachment, setAttachment] = useState<WorkspaceUpload | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [dailyDigest, setDailyDigest] = useState<DailyDigest>({ progress: "", papers: [] });
  const [dailyRuns, setDailyRuns] = useState<Run[]>([]);
  const [dailyLoading, setDailyLoading] = useState(false);
  const [dailyAction, setDailyAction] = useState<DailyRunKind | null>(null);
  const [dailySearchDraft, setDailySearchDraft] = useState("");
  const [runCenterRuns, setRunCenterRuns] = useState<Run[]>([]);
  const [badcases, setBadcases] = useState<BadcaseCandidate[]>([]);
  const [runCenterLoading, setRunCenterLoading] = useState(false);
  const [badcaseSubmitting, setBadcaseSubmitting] = useState<string | null>(null);
  const [badcaseCategories, setBadcaseCategories] = useState<Record<string, BadcaseCategory>>({});
  const [manualBadcaseRunId, setManualBadcaseRunId] = useState("");
  const [manualBadcaseCategory, setManualBadcaseCategory] = useState<BadcaseCategory>("answer_quality");
  const [manualBadcaseNote, setManualBadcaseNote] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
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
    const [history, usage] = await Promise.all([
      getSessionMessages(apiKey, sessionId),
      getSessionUsage(apiKey, sessionId),
    ]);
    setActiveSessionId(sessionId);
    localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    setMessages(displayMessages(history));
    setSessionUsage(usage);
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
        setSessionUsage(null);
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
        const [nextKeywords, nextDailyRuns] = await Promise.all([
          listDailyKeywords(apiKey),
          listDailyRuns(apiKey),
        ]);
        setKeywords(nextKeywords);
        setDailyRuns(nextDailyRuns);
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

  async function selectDailyRun(runId: string) {
    try {
      const detail = await getDailyRunDetail(apiKey, runId);
      const tabId = `daily-run:${detail.run_id}`;
      const title = dailyRunTitle(detail);
      setWorkbenchTabs((current) => {
        const existing = current.find((tab) => tab.id === tabId);
        if (existing?.kind === "dailyRun") {
          return current.map((tab) => tab.id === tabId ? { ...tab, title, detail } : tab);
        }
        return [...current, { id: tabId, kind: "dailyRun", title, detail }];
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

  async function uploadAttachment(file: File) {
    if (isUploading || isSending) {
      return;
    }
    setIsUploading(true);
    try {
      const upload = await uploadWorkspaceFile(apiKey, file);
      setAttachment(upload);
      setNotice({
        kind: "info",
        text: upload.duplicate
          ? `已找到已有文件：${upload.filename}。发送后会直接使用它。`
          : `已添加 ${upload.filename}。发送后会开始${upload.kind === "pdf" ? "解析与索引" : "图片理解"}。`,
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setIsUploading(false);
    }
  }

  function selectAttachment(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (file) {
      void uploadAttachment(file);
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

  async function loadDailyCenter() {
    setDailyLoading(true);
    try {
      const [digest, runs] = await Promise.all([
        getDailyDigest(apiKey),
        listDailyRuns(apiKey),
      ]);
      setDailyDigest(digest);
      setDailyRuns(runs);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setDailyLoading(false);
    }
  }

  async function openDailyCenter() {
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "daily")
      ? current
      : [...current, { id: "daily", kind: "daily", title: "每日检索" }]);
    setActiveWorkbenchTabId("daily");
    await loadDailyCenter();
  }

  async function updateDailyPaper(paper: DailyPaper, status: Exclude<DailyPaper["status"], "new">) {
    try {
      await updateDailyPaperStatus(apiKey, paper.keyword, paper.title, status);
      setDailyDigest((current) => ({
        ...current,
        papers: current.papers.map((item) => item.keyword === paper.keyword && item.title === paper.title
          ? { ...item, status }
          : item),
      }));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startDailyAction(kind: DailyRunKind) {
    if (isSending) {
      return;
    }
    const keyword = dailySearchDraft.trim();
    if (kind === "search" && !keyword) {
      setNotice({ kind: "info", text: "请输入要临时检索的关键词。" });
      return;
    }

    setDailyAction(kind);
    setIsSending(true);
    setNotice(null);
    setRunEvents([]);
    try {
      const started = await createDailyRun(apiKey, kind, keyword || undefined);
      setCurrentRun({
        run_id: started.run_id,
        kind: started.kind,
        session_id: null,
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
        if (runEvent.type === "status" || runEvent.type === "done") {
          setCurrentRun((current) => current && { ...current, status: runEvent.status || current.status });
        }
        if (runEvent.type === "error") {
          setCurrentRun((current) => current && {
            ...current,
            status: "failed",
            error_type: runEvent.error_type || "RunError",
          });
        }
      }, controller.signal);
      setCurrentRun(await getRun(apiKey, started.run_id));
      if (kind === "search") {
        setDailySearchDraft("");
      }
      await loadDailyCenter();
    } catch (error) {
      const message = userFacingError(error);
      setNotice({ kind: "error", text: message });
      setCurrentRun((current) => current && { ...current, status: "failed" });
    } finally {
      abortRef.current = null;
      setIsSending(false);
      setDailyAction(null);
    }
  }

  async function loadRunCenter() {
    setRunCenterLoading(true);
    try {
      const [sessionRuns, nextDailyRuns, nextBadcases, usage] = await Promise.all([
        activeSessionId ? listSessionRuns(apiKey, activeSessionId) : Promise.resolve([] as Run[]),
        listDailyRuns(apiKey),
        listBadcases(apiKey),
        activeSessionId ? getSessionUsage(apiKey, activeSessionId) : Promise.resolve(null),
      ]);
      setRunCenterRuns([...sessionRuns, ...nextDailyRuns].sort((left, right) => (
        `${right.updated_at}${right.created_at}`.localeCompare(`${left.updated_at}${left.created_at}`)
      )));
      setDailyRuns(nextDailyRuns);
      setBadcases(nextBadcases);
      setSessionUsage(usage);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setRunCenterLoading(false);
    }
  }

  async function openRunCenter() {
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "runs")
      ? current
      : [...current, { id: "runs", kind: "runs", title: "运行中心" }]);
    setActiveWorkbenchTabId("runs");
    await loadRunCenter();
  }

  async function markRunAsBadcase(run: Run) {
    const category = badcaseCategories[run.run_id] || "answer_quality";
    setBadcaseSubmitting(run.run_id);
    try {
      const candidate = await createBadcase(apiKey, run.run_id, category);
      setBadcases((current) => [candidate, ...current.filter((item) => item.candidate_id !== candidate.candidate_id)]);
      setNotice({ kind: "info", text: `已将该运行标记为 Badcase：${badcaseCategoryLabel(category)}。` });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setBadcaseSubmitting(null);
    }
  }

  async function markManualBadcase() {
    const runId = manualBadcaseRunId.trim();
    if (!runId) {
      setNotice({ kind: "info", text: "请填写要标记的运行 ID。" });
      return;
    }
    const requestId = `manual:${runId}`;
    setBadcaseSubmitting(requestId);
    try {
      const candidate = await createBadcase(apiKey, runId, manualBadcaseCategory, manualBadcaseNote);
      setBadcases((current) => [candidate, ...current.filter((item) => item.candidate_id !== candidate.candidate_id)]);
      setManualBadcaseNote("");
      setNotice({ kind: "info", text: `已将 ${runId} 标记为 Badcase。` });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setBadcaseSubmitting(null);
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
        setSessionUsage(null);
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

  async function sendMessage(
    event: FormEvent<HTMLFormElement> | undefined,
    resumeResearch = false,
  ) {
    event?.preventDefault();
    const content = resumeResearch ? "" : draft.trim();
    const pendingAttachment = resumeResearch ? null : attachment;
    if ((!content && !pendingAttachment && !resumeResearch) || isSending || isUploading) {
      return;
    }
    if (!activeSessionId) {
      setNotice({ kind: "info", text: "请先新建一个会话。" });
      return;
    }

    const requestMode: ResearchMode = resumeResearch ? "research" : mode;
    if (requestMode === "research" && pendingAttachment) {
      setNotice({ kind: "info", text: "请先在普通对话中发送并索引附件，再发起深度研究。" });
      return;
    }
    const shownContent = resumeResearch ? "继续上次深度研究" : [
      pendingAttachment ? `📎 ${pendingAttachment.filename}` : "",
      content,
    ].filter(Boolean).join("\n");
    if (!resumeResearch) {
      setDraft("");
    }
    setIsSending(true);
    setNotice(null);
    setRunEvents([]);
    setMessages((current) => [
      ...current,
      { id: `user-${Date.now()}`, role: "user", content: shownContent },
      { id: `assistant-${Date.now()}`, role: "assistant", content: "", streaming: true },
    ]);

    try {
      const started = await createRun(
        apiKey, requestMode, activeSessionId, content, pendingAttachment?.upload_id,
        researchScope, resumeResearch,
      );
      if (!resumeResearch) {
        setAttachment(null);
      }
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
      const [refreshedSessions, usage] = await Promise.all([
        listSessions(apiKey),
        getSessionUsage(apiKey, activeSessionId),
      ]);
      setSessions(refreshedSessions);
      setSessionUsage(usage);
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
              title="每日检索"
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
                  <button className="text-button" type="button" onClick={() => void openDailyCenter()}>
                    打开今日检索
                  </button>
                  {dailyRuns.length > 0 && (
                    <div className="workspace-list daily-report-list">
                      <p className="workspace-list-label">最近报告</p>
                      {dailyRuns.slice(0, 6).map((run) => (
                        <button
                          className="workspace-list-item"
                          key={run.run_id}
                          type="button"
                          onClick={() => void selectDailyRun(run.run_id)}
                          title={run.run_id}
                        >
                          <strong>{run.created_at ? `每日检索 · ${run.created_at.slice(0, 10)}` : "每日检索报告"}</strong>
                          <span>{statusLabel(run.status)} · {run.metrics.selected_count ?? 0} 篇入选</span>
                        </button>
                      ))}
                    </div>
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
                  {tab.kind === "daily" && <span aria-hidden="true">◌</span>}
                  {tab.kind === "dailyRun" && <span aria-hidden="true">▧</span>}
                  {tab.kind === "runs" && <span aria-hidden="true">◫</span>}
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
          ) : activeWorkbenchTab?.kind === "dailyRun" ? (
            <article className="document-page" aria-label={`每日检索报告：${activeWorkbenchTab.title}`}>
              <header className="document-page-heading">
                <div>
                  <p className="eyebrow">每日检索报告</p>
                  <h1>{activeWorkbenchTab.title}</h1>
                  <p className="document-page-meta">
                    <span className={`status status-${activeWorkbenchTab.detail.status}`}>{statusLabel(activeWorkbenchTab.detail.status)}</span>
                    {" · "}{activeWorkbenchTab.detail.run_id}
                  </p>
                </div>
              </header>
              <div className="document-page-content daily-report-page">
                {activeWorkbenchTab.detail.brief && (
                  <section className="daily-report-brief">
                    <h2>本次摘要</h2>
                    <p>{activeWorkbenchTab.detail.brief}</p>
                  </section>
                )}
                {activeWorkbenchTab.detail.warnings.length > 0 && (
                  <section className="daily-report-warnings">
                    <h2>质量提示</h2>
                    <ul>{activeWorkbenchTab.detail.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
                  </section>
                )}
                <section className="daily-report-papers">
                  <h2>论文推荐</h2>
                  {activeWorkbenchTab.detail.papers.length ? (
                    <div className="daily-paper-list">
                      {activeWorkbenchTab.detail.papers.map((paper) => (
                        <article className="daily-paper" key={`${paper.title}:${paper.url}`}>
                          <div className="daily-paper-main">
                            <p className="daily-paper-meta">
                              {paper.selected ? "已入选" : "候选"}
                              {paper.source ? ` · ${paper.source}` : ""}
                              {paper.year ? ` · ${paper.year}` : ""}
                              {paper.citation_count !== null ? ` · 引用 ${paper.citation_count}` : ""}
                            </p>
                            {paper.url ? (
                              <a className="daily-paper-title" href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a>
                            ) : (
                              <strong className="daily-paper-title">{paper.title}</strong>
                            )}
                            {paper.reason && <p className="daily-paper-reason">推荐理由：{paper.reason}</p>}
                            {paper.tags.length > 0 && <p className="daily-paper-tags">{paper.tags.map((tag) => <span key={tag}>{tag}</span>)}</p>}
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p>该任务尚未产生可展示的论文推荐。</p>
                  )}
                </section>
              </div>
            </article>
          ) : activeWorkbenchTab?.kind === "runs" ? (
            <article className="utility-page" aria-label="运行中心">
              <header className="utility-page-heading">
                <div>
                  <p className="eyebrow">可追溯运行</p>
                  <h1>运行中心</h1>
                  <p>查看当前会话及每日检索的近期任务；问题运行可记录为不含内容的 Badcase 候选。</p>
                </div>
                <button className="button button-secondary" type="button" onClick={() => void loadRunCenter()} disabled={runCenterLoading}>
                  {runCenterLoading ? "刷新中…" : "刷新"}
                </button>
              </header>
              <div className="utility-page-content">
                <section className="token-summary" aria-label="当前会话 Token 统计">
                  <div className="section-heading">
                    <div><h2>当前会话 Token</h2><p>来自持久化的对话与深度研究统计；每日检索暂不计入。</p></div>
                  </div>
                  {sessionUsage ? (
                    <div className="token-summary-grid">
                      <div><span>输入</span><strong>{sessionUsage.prompt.toLocaleString()}</strong></div>
                      <div><span>输出</span><strong>{sessionUsage.completion.toLocaleString()}</strong></div>
                      <div><span>合计</span><strong>{sessionUsage.total.toLocaleString()}</strong></div>
                      <div><span>模型调用</span><strong>{sessionUsage.calls.toLocaleString()}</strong></div>
                    </div>
                  ) : (
                    <p className="empty-panel">选择一个会话后显示其 Token 统计。</p>
                  )}
                </section>
                <section className="daily-section run-center-section">
                  <div className="section-heading">
                    <div><h2>近期运行</h2><p>当前会话与每日检索各保留最近 20 条。</p></div>
                  </div>
                  {runCenterLoading ? (
                    <p className="empty-panel">正在读取运行记录…</p>
                  ) : runCenterRuns.length ? (
                    <div className="run-center-list">
                      {runCenterRuns.map((run) => (
                        <article className="run-center-row" key={run.run_id}>
                          <div className="run-center-main">
                            <div className="run-center-meta">
                              <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>
                              <span>{run.kind === "daily" ? "每日检索" : run.kind === "research" ? "深度研究" : "对话"}</span>
                              <span>{durationLabel(run.duration_ms) || "未完成"}</span>
                            </div>
                            <strong title={run.run_id}>{run.run_id}</strong>
                            {run.error_type && <p className="run-center-error">{run.error_type}</p>}
                          </div>
                          <div className="badcase-editor">
                            <select
                              aria-label={`${run.run_id} 的 Badcase 分类`}
                              value={badcaseCategories[run.run_id] || "answer_quality"}
                              onChange={(event) => setBadcaseCategories((current) => ({
                                ...current,
                                [run.run_id]: event.target.value as BadcaseCategory,
                              }))}
                            >
                              {(["retrieval_miss", "citation_quality", "answer_quality", "tool_failure", "performance", "safety", "other"] as BadcaseCategory[]).map((category) => (
                                <option key={category} value={category}>{badcaseCategoryLabel(category)}</option>
                              ))}
                            </select>
                            <button
                              className="button button-secondary"
                              type="button"
                              onClick={() => void markRunAsBadcase(run)}
                              disabled={badcaseSubmitting === run.run_id || !["completed", "failed", "cancelled", "partial_failed"].includes(run.status)}
                            >
                              {badcaseSubmitting === run.run_id ? "记录中…" : "标记问题"}
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="empty-panel">当前范围内尚无运行记录。</p>
                  )}
                </section>

                <section className="daily-section">
                  <div className="section-heading">
                    <div><h2>Badcase 候选</h2><p>只保存分类、运行状态和匿名运行指标，不保存对话、论文或模型正文。</p></div>
                  </div>
                  {badcases.length ? (
                    <ol className="badcase-list">
                      {badcases.map((candidate) => (
                        <li key={candidate.candidate_id}>
                          <span className="status">{badcaseCategoryLabel(candidate.category)}</span>
                          <strong title={candidate.run_id}>{candidate.run_id}</strong>
                          <span>{candidate.status === "triage" ? "待整理" : candidate.status === "promoted" ? "已转为评测" : "已忽略"} · {candidate.occurrence_count} 次</span>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="empty-panel">还没有标记的问题运行。</p>
                  )}
                  <form
                    className="manual-badcase-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void markManualBadcase();
                    }}
                  >
                    <div>
                      <h3>手动标记</h3>
                      <p>可粘贴运行卡片中的 run_id；备注只写脱敏现象，不要写入原始问题、回答、论文内容或密钥。</p>
                    </div>
                    <input
                      value={manualBadcaseRunId}
                      onChange={(event) => setManualBadcaseRunId(event.target.value)}
                      placeholder="run_id，例如 chat-… 或 research-…"
                      aria-label="要标记的运行 ID"
                    />
                    <select
                      value={manualBadcaseCategory}
                      onChange={(event) => setManualBadcaseCategory(event.target.value as BadcaseCategory)}
                      aria-label="Badcase 分类"
                    >
                      {(["retrieval_miss", "citation_quality", "answer_quality", "tool_failure", "performance", "safety", "other"] as BadcaseCategory[]).map((category) => (
                        <option key={category} value={category}>{badcaseCategoryLabel(category)}</option>
                      ))}
                    </select>
                    <textarea
                      value={manualBadcaseNote}
                      onChange={(event) => setManualBadcaseNote(event.target.value)}
                      maxLength={500}
                      placeholder="可选脱敏备注（最多 500 字）"
                      aria-label="脱敏备注"
                    />
                    <button
                      className="button button-secondary"
                      type="submit"
                      disabled={!manualBadcaseRunId.trim() || badcaseSubmitting === `manual:${manualBadcaseRunId.trim()}`}
                    >
                      {badcaseSubmitting === `manual:${manualBadcaseRunId.trim()}` ? "记录中…" : "添加候选"}
                    </button>
                  </form>
                </section>
              </div>
            </article>
          ) : activeWorkbenchTab?.kind === "daily" ? (
            <article className="utility-page" aria-label="每日检索中心">
              <header className="utility-page-heading">
                <div>
                  <p className="eyebrow">文献发现</p>
                  <h1>每日检索中心</h1>
                  <p>{dailyDigest.progress || "管理今日文献并保留明确的阅读决策。"}</p>
                </div>
                <button className="button button-secondary" type="button" onClick={() => void loadDailyCenter()} disabled={dailyLoading || isSending}>
                  {dailyLoading ? "刷新中…" : "刷新"}
                </button>
              </header>
              <div className="utility-page-content">
                <section className="daily-actions" aria-label="检索操作">
                  <div className="daily-action-buttons">
                    <button className="button button-primary" type="button" onClick={() => void startDailyAction("daily")} disabled={isSending}>
                      {dailyAction === "daily" ? "检索中…" : "执行今日检索"}
                    </button>
                    <button className="button button-secondary" type="button" onClick={() => void startDailyAction("retry")} disabled={isSending}>
                      {dailyAction === "retry" ? "重试中…" : "重试失败项"}
                    </button>
                    <button className="button button-secondary" type="button" onClick={() => void startDailyAction("resume")} disabled={isSending}>
                      {dailyAction === "resume" ? "继续中…" : "继续未完成"}
                    </button>
                  </div>
                  <form
                    className="daily-search-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void startDailyAction("search");
                    }}
                  >
                    <input
                      value={dailySearchDraft}
                      onChange={(event) => setDailySearchDraft(event.target.value)}
                      placeholder="临时检索关键词，例如：TSN scheduling"
                      disabled={isSending}
                    />
                    <button className="button button-secondary" type="submit" disabled={isSending || !dailySearchDraft.trim()}>
                      {dailyAction === "search" ? "检索中…" : "临时检索"}
                    </button>
                  </form>
                </section>

                <section className="daily-section">
                  <div className="section-heading">
                    <div><h2>今日论文</h2><p>{dailyDigest.papers.length ? `共 ${dailyDigest.papers.length} 篇，可标记后续阅读。` : "尚无今日结果。"}</p></div>
                  </div>
                  {dailyLoading ? (
                    <p className="empty-panel">正在读取今日结果…</p>
                  ) : dailyDigest.papers.length ? (
                    <div className="daily-paper-list">
                      {dailyDigest.papers.map((paper) => (
                        <article className="daily-paper" key={`${paper.keyword}:${paper.title}`}>
                          <div className="daily-paper-main">
                            <p className="daily-paper-meta">{paper.keyword}{paper.source ? ` · ${paper.source}` : ""}</p>
                            {paper.url ? (
                              <a className="daily-paper-title" href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a>
                            ) : (
                              <strong className="daily-paper-title">{paper.title}</strong>
                            )}
                          </div>
                          <div className="daily-paper-actions">
                            <span className={`status daily-paper-status status-${paper.status}`}>{dailyPaperStatusLabel(paper.status)}</span>
                            <button type="button" onClick={() => void updateDailyPaper(paper, "want_read")} disabled={paper.status === "want_read"}>想读</button>
                            <button type="button" onClick={() => void updateDailyPaper(paper, "read")} disabled={paper.status === "read"}>已读</button>
                            <button type="button" onClick={() => void updateDailyPaper(paper, "skipped")} disabled={paper.status === "skipped"}>跳过</button>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="empty-panel">执行今日检索后，匹配论文会出现在这里。</p>
                  )}
                </section>

                <section className="daily-section">
                  <div className="section-heading">
                    <div><h2>最近运行</h2><p>只保留状态和统计，不重复展示论文正文。</p></div>
                  </div>
                  {dailyRuns.length ? (
                    <ol className="daily-run-list">
                      {dailyRuns.map((run) => (
                        <li key={run.run_id}>
                          <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>
                          <button className="daily-run-open" type="button" onClick={() => void selectDailyRun(run.run_id)} title={run.run_id}>
                            {run.created_at ? `查看 ${run.created_at.slice(0, 10)} 报告` : "查看检索报告"}
                          </button>
                          <span>{run.metrics.selected_count ?? 0} 篇入选 · {durationLabel(run.duration_ms) || "处理中"}</span>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="empty-panel">尚无每日检索运行记录。</p>
                  )}
                </section>
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

              {mode === "research" && (
                <div className="research-options" aria-label="深度研究选项">
                  <label>
                    来源范围
                    <select value={researchScope} onChange={(event) => setResearchScope(event.target.value as ResearchScope)} disabled={isSending}>
                      <option value="both">本地论文 + 公开文献</option>
                      <option value="local">仅本地论文库</option>
                      <option value="public">仅公开文献</option>
                    </select>
                  </label>
                  <button
                    className="button button-secondary"
                    type="button"
                    onClick={() => void sendMessage(undefined, true)}
                    disabled={isSending || !activeSessionId}
                  >
                    继续上次研究
                  </button>
                </div>
              )}

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
                <input
                  ref={uploadInputRef}
                  className="file-input"
                  type="file"
                  accept=".pdf,.png,.jpg,.jpeg"
                  onChange={selectAttachment}
                  disabled={isSending || isUploading}
                />
                <div className="composer-attachments">
                  <button
                    className="button button-secondary"
                    type="button"
                    onClick={() => uploadInputRef.current?.click()}
                    disabled={isSending || isUploading}
                  >
                    {isUploading ? "上传中…" : "添加 PDF / 图片"}
                  </button>
                  {attachment && (
                    <span className="attachment-chip">
                      <span title={attachment.filename}>📎 {attachment.filename}</span>
                      <button type="button" onClick={() => setAttachment(null)} disabled={isSending}>×</button>
                    </span>
                  )}
                </div>
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder={mode === "research" ? "输入研究问题，开始深度研究…" : "提出问题，开始对话…"}
                  rows={3}
                  disabled={isSending || !activeSessionId}
                />
                <div className="composer-footer">
                  <span>{attachment ? "发送后会在后台处理附件，进度显示在右侧。" : mode === "research" ? "可选择来源范围，或继续上次未完成研究" : "支持引用、公式与 Markdown"}</span>
                  <button className="button button-primary" type="submit" disabled={isSending || isUploading || (!draft.trim() && !attachment) || !activeSessionId}>
                    {isSending ? "生成中…" : mode === "research" ? "开始研究" : "发送"}
                  </button>
                </div>
              </form>
            </>
          )}
        </section>

        <RunInspector
          run={currentRun}
          events={runEvents}
          onCancel={() => void stopCurrentRun()}
          onOpenRunCenter={() => void openRunCenter()}
          cancelling={isSending}
        />
      </div>
    </main>
  );
}

import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";

import {
  cancelRun,
  createBadcase,
  createDailyRun,
  createDailyKeyword,
  createExperimentRun,
  createRun,
  createRunSteer,
  connectExperimentRepository,
  createSession,
  deleteDailyKeyword,
  deleteExperimentProject,
  deleteSession,
  downloadResearchDocument,
  getDailyRunDetail,
  getExperimentFile,
  getExperimentProject,
  getResearchDocument,
  getRun,
  getSessionMessages,
  getSessionUsage,
  getWorkspaceProfile,
  getDailyDigest,
  getWorkspaceSettings,
  listDailyKeywords,
  listDailyRuns,
  listExperimentProjects,
  listBadcases,
  listPapers,
  listResearchDocuments,
  listSessionRuns,
  listSessions,
  routeResearchDocumentRequest,
  searchExperimentRepositoryCandidates,
  streamRun,
  updateDailyPaperStatus,
  updateWorkspaceSettings,
  useExperimentMethodReconstruction,
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
  type ExperimentFileContent,
  type ExperimentProject,
  type ExperimentProjectDetail,
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
  type UserProfile,
} from "./types";

const MarkdownContent = lazy(() => import("./MarkdownContent"));

const API_TOKEN_KEY = "research-agent.api-token";
const ACTIVE_SESSION_KEY = "research-agent.active-session";

type ResearchMode = "chat" | "research";
type WorkspacePanel = "documents" | "profile" | "papers" | "experiments" | "keywords" | "settings";
type Notice = { kind: "error" | "info"; text: string } | null;
type DisplayMessage = Message & { id: string; streaming?: boolean };
type RepositoryRelationship = "user_provided" | "official_confirmed" | "community" | "candidate_unverified";
type ExperimentRepository = {
  repository_url?: string;
  full_name?: string;
  description?: string;
  default_branch?: string;
  commit_sha?: string;
  stars?: number;
  is_fork?: boolean;
  is_archived?: boolean;
};
type ExperimentCodeSource = {
  mode?: string;
  status?: string;
  candidate_status?: string;
  relationship?: RepositoryRelationship | string;
  repository?: ExperimentRepository;
  candidates?: ExperimentRepository[];
};
type RepositoryPreparation = {
  local_path?: string;
  commit_sha?: string;
  sync_mode?: string;
  dependency_files?: string[];
  entrypoint_candidates?: string[];
  config_candidates?: string[];
  has_submodules?: boolean;
  scan_truncated?: boolean;
  status?: string;
  error?: string;
};
type MethodReconstruction = {
  status?: string;
  basis?: string;
  method_summary?: string;
  components?: Array<{
    name?: string;
    responsibility?: string;
    implementation_hint?: string;
    evidence_ids?: string[];
  }>;
  assumptions?: string[];
  unresolved?: string[];
  source_ref_count?: number;
};
type WorkbenchTab =
  | { id: "chat"; kind: "chat"; title: string }
  | { id: string; kind: "document"; title: string; document: ResearchDocumentDetail }
  | { id: "profile"; kind: "profile"; title: string; profile: UserProfile }
  | { id: "daily"; kind: "daily"; title: string }
  | { id: string; kind: "dailyRun"; title: string; detail: DailyRunDetail }
  | { id: string; kind: "experiment"; title: string; project: ExperimentProjectDetail }
  | { id: "runs"; kind: "runs"; title: string };

const TOOL_LABELS: Record<string, string> = {
  search_papers: "检索论文",
  read_pdf: "读取 PDF",
  describe_image: "理解图片",
  paper_card: "生成论文卡片",
  query_papers: "检索本地论文库",
  list_research_document_sections: "查看档案章节",
  read_research_document_section: "读取档案章节",
  apply_research_document_patch: "安全更新档案",
  compare_papers_to_research_document: "论文档案比对",
  read_research_document_ledger: "查看证据与假设账本",
  apply_research_document_ledger_patch: "更新证据与假设账本",
  review_research_document_innovation: "科研档案创新性审查",
  review_new_paper_impact_on_research_document: "新论文综合审查",
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

function stageLabel(stage: string | undefined, fallback: string): string {
  const labels: Record<string, string> = {
    steer_queued: "已收到运行中补充，等待下一节点使用",
    steer_consumed: "运行中补充已在下一节点使用",
  };
  return labels[stage || ""] || stage || fallback;
}

function modeLabel(mode: ResearchMode): string {
  return mode === "research" ? "深度研究" : "对话";
}

function runKindLabel(kind: Run["kind"]): string {
  return kind === "daily" ? "每日检索" : kind === "research" ? "深度研究" : kind === "experiment" ? "论文复现" : "对话";
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

function ledgerKindLabel(kind: string): string {
  return ({
    research_question: "研究问题",
    hypothesis: "假设",
    innovation_candidate: "创新候选",
    decision: "研究决策",
  } as Record<string, string>)[kind] || kind;
}

function ledgerStatusLabel(status: string): string {
  return ({
    open: "待澄清",
    hypothesis: "假设",
    supported: "有证据支持",
    contested: "存在争议",
    rejected: "已否定",
    decision: "已确认决策",
  } as Record<string, string>)[status] || status;
}

function ledgerRelationLabel(relation: string): string {
  return ({
    supports: "支持",
    contradicts: "冲突",
    conditions: "条件/边界",
    inspiration: "启发",
  } as Record<string, string>)[relation] || relation;
}

function experimentCodeSource(project: ExperimentProjectDetail): ExperimentCodeSource {
  const source = project.spec.code_source;
  return source && typeof source === "object" && !Array.isArray(source)
    ? source as ExperimentCodeSource
    : { mode: "not_checked", status: "not_checked", candidates: [] };
}

function codeSourceLabel(source: ExperimentCodeSource): string {
  if (source.mode === "repository") {
    return ({
      official_confirmed: "官方实现（已确认）",
      community: "社区实现",
      user_provided: "用户提供仓库（待核验）",
      candidate_unverified: "GitHub 候选（待核验）",
    } as Record<string, string>)[String(source.relationship || "")] || "已连接仓库";
  }
  if (source.mode === "method_reconstruction") return "按论文方法还原";
  if (source.mode === "repository_search") return source.status === "candidates_found" ? "已找到候选" : "未找到候选";
  return "尚未检查";
}

function repositoryPreparation(project: ExperimentProjectDetail): RepositoryPreparation | null {
  const value = project.spec.repository_preparation;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RepositoryPreparation
    : null;
}

function methodReconstruction(project: ExperimentProjectDetail): MethodReconstruction | null {
  const value = project.spec.method_reconstruction;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as MethodReconstruction
    : null;
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
  onRetry,
  onOpenRunCenter,
  cancelling,
}: {
  run: Run | null;
  events: RunEvent[];
  onCancel: () => void;
  onRetry: () => void;
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
            <div><dt>类型</dt><dd>{runKindLabel(run.kind)}</dd></div>
            <div><dt>耗时</dt><dd>{durationLabel(run.duration_ms) || "进行中"}</dd></div>
            {run.model && <div><dt>模型</dt><dd>{run.model}</dd></div>}
          </dl>
          {!["completed", "cancelled", "failed"].includes(run.status) && (
            <button
              className="button button-secondary button-full"
              type="button"
              onClick={onCancel}
              disabled={cancelling}
              title="协作式停止当前任务；不是可恢复的进程暂停"
            >
              {cancelling ? "正在暂停…" : "暂停任务"}
            </button>
          )}
          {run.kind === "chat" && ["failed", "cancelled"].includes(run.status) && (
            <button
              className="button button-secondary button-full"
              type="button"
              onClick={onRetry}
            >
              重新发送本轮问题
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
                        : stageLabel(event.stage, event.type === "done" ? "已生成最终结果" : "任务状态更新")}
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
  const [isCancelling, setIsCancelling] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [openWorkspacePanel, setOpenWorkspacePanel] = useState<WorkspacePanel | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState<WorkspacePanel | null>(null);
  const [documents, setDocuments] = useState<ResearchDocument[]>([]);
  const [documentRequest, setDocumentRequest] = useState("");
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [workbenchTabs, setWorkbenchTabs] = useState<WorkbenchTab[]>([
    { id: "chat", kind: "chat", title: "对话" },
  ]);
  const [activeWorkbenchTabId, setActiveWorkbenchTabId] = useState("chat");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [experiments, setExperiments] = useState<ExperimentProject[]>([]);
  const [experimentStarting, setExperimentStarting] = useState<string | null>(null);
  const [experimentFiles, setExperimentFiles] = useState<Record<string, ExperimentFileContent | null>>({});
  const [repositoryUrlDraft, setRepositoryUrlDraft] = useState("");
  const [repositoryRelationship, setRepositoryRelationship] = useState<Exclude<RepositoryRelationship, "candidate_unverified">>("user_provided");
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
  const requestGenerationRef = useRef(0);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const activeWorkbenchTab = workbenchTabs.find((tab) => tab.id === activeWorkbenchTabId) || workbenchTabs[0];
  const selectedDocumentId = activeWorkbenchTab?.kind === "document"
    ? activeWorkbenchTab.document.document_id
    : null;
  const selectedExperimentId = activeWorkbenchTab?.kind === "experiment"
    ? activeWorkbenchTab.project.project_id
    : null;
  const selectedExperimentFile = selectedExperimentId ? experimentFiles[selectedExperimentId] : null;
  const canSteerCurrentRun = Boolean(
    isSending
    && currentRun
    && currentRun.session_id === activeSessionId
    && ["chat", "research"].includes(currentRun.kind)
    && ["queued", "running"].includes(currentRun.status),
  );

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
      } else if (panel === "profile") {
        setProfile(await getWorkspaceProfile(apiKey));
      } else if (panel === "papers") {
        setPapers(await listPapers(apiKey));
      } else if (panel === "experiments") {
        setExperiments(await listExperimentProjects(apiKey));
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

  async function submitResearchDocumentRequest(event: FormEvent<HTMLFormElement>, document: ResearchDocumentDetail) {
    event.preventDefault();
    const request = documentRequest.trim();
    if (!request || isSending) {
      return;
    }
    try {
      const routed = await routeResearchDocumentRequest(apiKey, document.document_id, request);
      setDocumentRequest("");
      setMode("chat");
      setWorkbenchTabs((current) => current.some((tab) => tab.id === "chat") ? current : [...current, { id: "chat", kind: "chat", title: "对话" }]);
      setActiveWorkbenchTabId("chat");
      await sendMessage(undefined, false, routed.message, "chat");
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  function preparePaperRelationReview(paper: Paper) {
    setDraft(
      `【论文关系审查】\n源论文：${paper.title}\nsource_paper_id=${paper.paper_id}\n\n`
      + "请先检查这篇论文与下列目标论文之间是否存在值得记录的关系。必须用两篇论文的页码或检索片段定位证据；只可提出候选，不要自动写入。请说明关系方向、关系类型（方法相似/方法改进/实验可比/结果冲突/明确引用）、依据和不确定性，最后等待我确认。\n\n"
      + "目标论文标题或 paper_id：- （在此填写）",
    );
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "chat") ? current : [...current, { id: "chat", kind: "chat", title: "对话" }]);
    setActiveWorkbenchTabId("chat");
    setNotice({ kind: "info", text: "论文关系审查指令已放入对话框。Agent 会先给出带证据的候选关系，只有你确认后才会保存。" });
  }

  async function selectExperimentProject(projectId: string) {
    try {
      const project = await getExperimentProject(apiKey, projectId);
      const tabId = `experiment:${project.project_id}`;
      setWorkbenchTabs((current) => {
        const existing = current.find((tab) => tab.id === tabId);
        if (existing?.kind === "experiment") {
          return current.map((tab) => tab.id === tabId ? { ...tab, title: project.paper_title, project } : tab);
        }
        return [...current, { id: tabId, kind: "experiment", title: project.paper_title, project }];
      });
      setActiveWorkbenchTabId(tabId);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function openProfile() {
    try {
      const nextProfile = profile || await getWorkspaceProfile(apiKey);
      setProfile(nextProfile);
      setWorkbenchTabs((current) => current.some((tab) => tab.id === "profile")
        ? current.map((tab) => tab.id === "profile"
          ? { id: "profile", kind: "profile", title: "用户画像", profile: nextProfile }
          : tab)
        : [...current, { id: "profile", kind: "profile", title: "用户画像", profile: nextProfile }]);
      setActiveWorkbenchTabId("profile");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function openExperimentFile(projectId: string, relativePath: string) {
    try {
      const file = await getExperimentFile(apiKey, projectId, relativePath);
      setExperimentFiles((current) => ({ ...current, [projectId]: file }));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startPaperReproduction(paper: Paper) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再创建复现实验项目。" });
      }
      return;
    }
    setExperimentStarting(paper.paper_id);
    setNotice({ kind: "info", text: "正在从论文页面级证据生成复现实验项目…" });
    try {
      const started = await createExperimentRun(apiKey, activeSessionId, paper.paper_id);
      if (started.project_id) {
        await selectExperimentProject(started.project_id);
      }
      const controller = new AbortController();
      await streamRun(apiKey, started.stream_url, () => undefined, controller.signal);
      const detail = await getRun(apiKey, started.run_id);
      if (detail.project_id) {
        await selectExperimentProject(detail.project_id);
      }
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed" ? "复现实验项目已生成。" : "复现实验项目需要查看后续待确认项。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function retryExperimentProject(project: ExperimentProjectDetail) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再重试复现实验项目。" });
      }
      return;
    }
    setExperimentStarting(`retry:${project.project_id}`);
    setNotice({ kind: "info", text: "正在重试生成实验规格与代码，上一版文件会保留为修订快照。" });
    try {
      const started = await createExperimentRun(apiKey, activeSessionId, project.paper_id, project.project_id);
      await selectExperimentProject(project.project_id);
      await streamRun(apiKey, started.stream_url, () => undefined, new AbortController().signal);
      const detail = await getRun(apiKey, started.run_id);
      await selectExperimentProject(project.project_id);
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed" ? "复现实验项目已重新生成。" : "重试未完全完成，请查看待确认项。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  function replaceExperimentProject(project: ExperimentProjectDetail) {
    const tabId = `experiment:${project.project_id}`;
    setWorkbenchTabs((current) => current.map((tab) => (
      tab.id === tabId && tab.kind === "experiment"
        ? { ...tab, title: project.paper_title, project }
        : tab
    )));
    setExperimentFiles((current) => {
      const { [project.project_id]: _stale, ...remaining } = current;
      return remaining;
    });
  }

  async function searchExperimentRepositories(project: ExperimentProjectDetail) {
    if (experimentStarting) return;
    setExperimentStarting(`source:${project.project_id}`);
    setNotice({ kind: "info", text: "正在按论文标题检索 GitHub 候选仓库…" });
    try {
      const updated = await searchExperimentRepositoryCandidates(apiKey, project.project_id);
      replaceExperimentProject(updated);
      setExperiments(await listExperimentProjects(apiKey));
      const source = experimentCodeSource(updated);
      setNotice({
        kind: "info",
        text: (source.candidate_status || source.status) === "candidates_found"
          ? "已保存 GitHub 候选。候选不等于官方实现，请核验后再连接。"
          : "未找到候选仓库；这不代表论文一定闭源，可直接提供 GitHub 地址或按论文方法还原。",
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function connectExperimentCodeRepository(
    project: ExperimentProjectDetail,
    repositoryUrl: string,
    relationship: RepositoryRelationship,
  ) {
    const url = repositoryUrl.trim();
    if (!url) {
      setNotice({ kind: "info", text: "请先填写 GitHub 仓库地址。" });
      return;
    }
    if (experimentStarting) return;
    setExperimentStarting(`source:${project.project_id}`);
    setNotice({ kind: "info", text: "正在读取 GitHub 仓库元数据并固定当前 commit…" });
    try {
      const updated = await connectExperimentRepository(apiKey, project.project_id, url, relationship);
      replaceExperimentProject(updated);
      setRepositoryUrlDraft("");
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({ kind: "info", text: "仓库已连接并记录当前 commit；尚未克隆、安装依赖或执行代码。" });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function prepareExperimentRepository(project: ExperimentProjectDetail) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再同步上游仓库。" });
      }
      return;
    }
    setExperimentStarting(`prepare:${project.project_id}`);
    setNotice({ kind: "info", text: "正在固定 commit、克隆并静态分析上游仓库；不会安装依赖或执行代码。" });
    try {
      const started = await createExperimentRun(
        apiKey, activeSessionId, project.paper_id, project.project_id, "prepare_repository",
      );
      await streamRun(apiKey, started.stream_url, () => undefined, new AbortController().signal);
      const detail = await getRun(apiKey, started.run_id);
      await selectExperimentProject(project.project_id);
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed"
          ? "上游仓库已准备完成，请核对依赖、数据与训练入口。"
          : "上游仓库准备未完成，请查看运行中心的错误信息。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function startExperimentMethodReconstruction(project: ExperimentProjectDetail) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再开始论文方法还原。" });
      }
      return;
    }
    setExperimentStarting(`method:${project.project_id}`);
    setNotice({ kind: "info", text: "正在依据论文页面级证据生成方法还原参考实现；不会下载数据、执行训练或声称复现成功。" });
    try {
      const updated = await useExperimentMethodReconstruction(apiKey, project.project_id);
      replaceExperimentProject(updated);
      const started = await createExperimentRun(
        apiKey, activeSessionId, project.paper_id, project.project_id, "reconstruct_method",
      );
      await streamRun(apiKey, started.stream_url, () => undefined, new AbortController().signal);
      const detail = await getRun(apiKey, started.run_id);
      await selectExperimentProject(project.project_id);
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed"
          ? "已生成论文方法还原参考实现，请先核对假设和待确认项。"
          : "方法还原未完全完成，请查看运行中心和待确认项。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  function prepareExperimentConfirmation(project: ExperimentProjectDetail) {
    const unknowns = Array.isArray(project.spec.unknowns)
      ? project.spec.unknowns.map((item) => `- ${String(item)}`).join("\n")
      : "- （请填写要确认的项目）";
    setDraft(
      `【复现实验项目确认】\nproject_id=${project.project_id}\n\n`
      + "请更新这个论文复现实验项目。以下内容由我确认（请把每项改成明确事实后再发送）：\n"
      + "- （在此替换为你已确认的事实；未确认请删除这一行）\n\n"
      + "以下原待确认项中，只有被上述事实明确解决的条目才可移除（请保持原文）：\n"
      + unknowns
      + "\n\n请只写入我确认的内容；同步更新项目规格、配置和 README，不要执行训练或声称指标已复现。",
    );
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "chat") ? current : [...current, { id: "chat", kind: "chat", title: "对话" }]);
    setActiveWorkbenchTabId("chat");
    setNotice({ kind: "info", text: "确认指令已放入对话框。请补全事实后发送，Agent 才会修改该项目。" });
  }

  async function removeExperimentProject(project: ExperimentProjectDetail) {
    if (!window.confirm(`删除“${project.paper_title}”的复现实验项目？这会同时删除该项目的规格、代码和历史修订文件。`)) {
      return;
    }
    try {
      await deleteExperimentProject(apiKey, project.project_id);
      setExperiments((current) => current.filter((item) => item.project_id !== project.project_id));
      setExperimentFiles((current) => {
        const { [project.project_id]: _removed, ...remaining } = current;
        return remaining;
      });
      closeWorkbenchTab(`experiment:${project.project_id}`);
      setNotice({ kind: "info", text: "复现实验项目及其对应文件已删除。" });
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
        project_id: started.project_id,
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
        steers: [],
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
    if (sessionId === activeSessionId) {
      return;
    }
    try {
      await abandonActiveRunForNavigation();
      await loadSession(sessionId);
      setActiveWorkbenchTabId("chat");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startSession() {
    try {
      await abandonActiveRunForNavigation();
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
    retryContent = "",
    requestedMode?: ResearchMode,
  ) {
    event?.preventDefault();
    const content = resumeResearch ? "" : (retryContent || draft.trim());
    const pendingAttachment = resumeResearch ? null : attachment;
    if ((!content && !pendingAttachment && !resumeResearch) || isUploading) {
      return;
    }
    if (!activeSessionId) {
      setNotice({ kind: "info", text: "请先新建一个会话。" });
      return;
    }

    if (canSteerCurrentRun) {
      if (!content) {
        return;
      }
      if (pendingAttachment) {
        setNotice({ kind: "info", text: "运行中补充暂只支持文本；请先移除已暂存的附件，或等待当前任务结束后再发送。" });
        return;
      }
      try {
        await createRunSteer(apiKey, currentRun!.run_id, content);
        setDraft("");
        setMessages((current) => {
          const guidance = {
            id: `steer-${Date.now()}`,
            role: "user" as const,
            content: `运行中补充：\n${content}`,
          };
          const last = current.at(-1);
          // The assistant bubble must remain last so later stream tokens
          // continue its answer instead of creating a disconnected second one.
          return last?.role === "assistant" && last.streaming
            ? [...current.slice(0, -1), guidance, last]
            : [...current, guidance];
        });
        setRunEvents((current) => appendRunEvent(current, {
          type: "status", status: currentRun!.status, stage: "steer_queued",
        }));
        setNotice({ kind: "info", text: "补充已保存，会在当前任务的下一处理节点使用。" });
      } catch (error) {
        setNotice({ kind: "error", text: userFacingError(error) });
      }
      return;
    }

    if (isSending) {
      return;
    }

    const requestGeneration = requestGenerationRef.current + 1;
    requestGenerationRef.current = requestGeneration;
    const isCurrentRequest = () => requestGenerationRef.current === requestGeneration;

    const requestMode: ResearchMode = resumeResearch ? "research" : (requestedMode || mode);
    if (requestMode === "research" && pendingAttachment) {
      setNotice({ kind: "info", text: "请先在普通对话中发送并索引附件，再发起深度研究。" });
      return;
    }
    const shownContent = resumeResearch ? "继续上次深度研究" : [
      pendingAttachment ? `📎 ${pendingAttachment.filename}` : "",
      content,
    ].filter(Boolean).join("\n");
    if (!resumeResearch && !retryContent) {
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
      if (!isCurrentRequest()) {
        await cancelRun(apiKey, started.run_id).catch(() => undefined);
        return;
      }
      if (!resumeResearch) {
        setAttachment(null);
      }
      setCurrentRun({
        run_id: started.run_id,
        kind: started.kind,
        session_id: started.session_id,
        project_id: started.project_id,
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
        steers: [],
      });
      const controller = new AbortController();
      abortRef.current = controller;
      await streamRun(apiKey, started.stream_url, (runEvent) => {
        if (!isCurrentRequest()) {
          return;
        }
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
      if (!isCurrentRequest()) {
        return;
      }
      const detail = await getRun(apiKey, started.run_id);
      if (!isCurrentRequest()) {
        return;
      }
      setCurrentRun(detail);
      completeAnswer(detail.answer || undefined);
      const [refreshedSessions, usage] = await Promise.all([
        listSessions(apiKey),
        getSessionUsage(apiKey, activeSessionId),
      ]);
      setSessions(refreshedSessions);
      setSessionUsage(usage);
    } catch (error) {
      if (!isCurrentRequest()) {
        return;
      }
      const message = userFacingError(error);
      completeAnswer(`⚠️ ${message}`);
      setNotice({ kind: "error", text: message });
      setCurrentRun((current) => current && { ...current, status: "failed" });
    } finally {
      if (isCurrentRequest()) {
        abortRef.current = null;
        setIsSending(false);
      }
    }
  }

  async function abandonActiveRunForNavigation() {
    requestGenerationRef.current += 1;
    const activeRun = currentRun;
    abortRef.current?.abort();
    abortRef.current = null;
    setIsSending(false);
    setIsCancelling(false);
    setCurrentRun(null);
    setRunEvents([]);
    if (activeRun && !["completed", "cancelled", "failed"].includes(activeRun.status)) {
      try {
        await cancelRun(apiKey, activeRun.run_id);
      } catch {
        // Navigation must not be blocked when the server has already ended the run.
      }
    }
  }

  async function stopCurrentRun() {
    if (!currentRun || !isSending || isCancelling || ["completed", "cancelled", "failed"].includes(currentRun.status)) {
      return;
    }
    setIsCancelling(true);
    try {
      const cancelled = await cancelRun(apiKey, currentRun.run_id);
      setCurrentRun((current) => current && { ...current, status: cancelled.status });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setIsCancelling(false);
    }
  }

  async function retryCurrentChat() {
    if (!currentRun || currentRun.kind !== "chat" || isSending) {
      return;
    }
    const lastUserMessage = [...messages].reverse().find((message) => message.role === "user");
    const content = lastUserMessage?.content.trim() || "";
    if (!content || content.startsWith("📎")) {
      setNotice({ kind: "info", text: "含附件或不可恢复的输入请重新上传后发送；纯文本对话可在此一键重试。" });
      return;
    }
    await sendMessage(undefined, false, content);
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

  const activeExperimentSource = activeWorkbenchTab?.kind === "experiment"
    ? experimentCodeSource(activeWorkbenchTab.project)
    : null;
  const activeRepositoryPreparation = activeWorkbenchTab?.kind === "experiment"
    ? repositoryPreparation(activeWorkbenchTab.project)
    : null;
  const activeMethodReconstruction = activeWorkbenchTab?.kind === "experiment"
    ? methodReconstruction(activeWorkbenchTab.project)
    : null;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Research Agent 首页">
          <span className="brand-mark" aria-hidden="true">R</span>
          <span>Research Agent</span>
        </a>
        <p>论文证据、研究推理与可追溯运行</p>
      </header>

      <div className="workspace">
        <aside className="session-sidebar" aria-label="会话列表">
          <div className="sidebar-heading">
            <div><p className="eyebrow">工作区</p><h2>会话</h2></div>
            <button className="icon-button" onClick={() => void startSession()} aria-label="新建会话">＋</button>
          </div>
          <nav className="session-list">
            {sessions.map((session) => (
              <button
                key={session.thread_id}
                className={`session-item ${session.thread_id === activeSessionId ? "active" : ""}`}
                onClick={() => void selectSession(session.thread_id)}
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
                      <div>
                        <strong>{paper.title}</strong>
                        <span>{paper.chunks} 个检索片段 · {paper.relation_count ? `${paper.relation_count} 条已确认关系` : "暂无已确认关系"}</span>
                      </div>
                      <div className="paper-actions">
                        <button
                          className="text-button paper-reproduce-button"
                          type="button"
                          onClick={() => preparePaperRelationReview(paper)}
                        >
                          关系审查
                        </button>
                        <button
                          className="text-button paper-reproduce-button"
                          type="button"
                          onClick={() => void startPaperReproduction(paper)}
                          disabled={Boolean(experimentStarting)}
                        >
                          {experimentStarting === paper.paper_id ? "生成中…" : "复现实验"}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="panel-hint">本地论文库为空。</p>
              )}
            </WorkspacePanel>

            <WorkspacePanel
              title="用户画像"
              open={openWorkspacePanel === "profile"}
              onToggle={() => void toggleWorkspacePanel("profile")}
            >
              {workspaceLoading === "profile" ? (
                <p className="panel-hint">正在读取用户画像…</p>
              ) : (
                <>
                  <p className="panel-hint">{profile?.content ? "用于跨会话保留稳定研究偏好；可在工作台查看。" : "尚未形成用户画像。"}</p>
                  <button className="text-button" type="button" onClick={() => void openProfile()}>
                    在工作台打开
                  </button>
                </>
              )}
            </WorkspacePanel>

            <WorkspacePanel
              title="复现实验"
              open={openWorkspacePanel === "experiments"}
              onToggle={() => void toggleWorkspacePanel("experiments")}
            >
              {workspaceLoading === "experiments" ? (
                <p className="panel-hint">正在读取实验项目…</p>
              ) : experiments.length ? (
                <div className="workspace-list">
                  {experiments.map((project) => (
                    <button
                      key={project.project_id}
                      className={`workspace-list-item ${selectedExperimentId === project.project_id ? "selected" : ""}`}
                      type="button"
                      onClick={() => void selectExperimentProject(project.project_id)}
                      title={project.paper_title}
                    >
                      <strong>{project.paper_title}</strong>
                      <span>{project.reproduction_level === "exact" ? "精确复现规格" : project.reproduction_level === "approximate" ? "近似复现规格" : "待确认规格"} · {project.status}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="panel-hint">在“论文库”中选择一篇已解析论文，再点击“复现实验”。</p>
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
                  {tab.kind === "profile" && <span aria-hidden="true">◉</span>}
                  {tab.kind === "daily" && <span aria-hidden="true">◌</span>}
                  {tab.kind === "dailyRun" && <span aria-hidden="true">▧</span>}
                  {tab.kind === "experiment" && <span aria-hidden="true">⌘</span>}
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
          {activeWorkbenchTab?.kind === "profile" ? (
            <article className="document-page" aria-label="用户画像">
              <header className="document-page-heading">
                <div>
                  <p className="eyebrow">跨会话偏好</p>
                  <h1>用户画像</h1>
                  <p className="document-page-meta">仅显示 Agent 已保存的稳定偏好，不包含完整对话内容。</p>
                </div>
              </header>
              <div className="document-page-content">
                {activeWorkbenchTab.profile.content ? (
                  <Suspense fallback={<p className="markdown-loading">{activeWorkbenchTab.profile.content}</p>}>
                    <MarkdownContent content={activeWorkbenchTab.profile.content} />
                  </Suspense>
                ) : <p>尚未形成用户画像。</p>}
              </div>
            </article>
          ) : activeWorkbenchTab?.kind === "document" ? (
            <article className="document-page" aria-label={`科研档案：${activeWorkbenchTab.document.title}`}>
              <header className="document-page-heading">
                <div>
                  <p className="eyebrow">科研档案</p>
                  <h1>{activeWorkbenchTab.document.title}</h1>
                  <p className="document-page-meta">
                    修订 {activeWorkbenchTab.document.revision} · 更新于 {activeWorkbenchTab.document.updated_at || activeWorkbenchTab.document.created_at} · 四章节模板
                  </p>
                </div>
                <div className="document-page-actions">
                  <button className="button button-secondary" type="button" onClick={() => void downloadDocument(activeWorkbenchTab.document)}>
                    下载 Word
                  </button>
                </div>
              </header>
              <section className="document-safety-note" aria-label="科研档案安全编辑说明">
                <strong>安全编辑已启用</strong>
                <span>Agent 只能在读取过的章节内提交带版本校验的局部补丁；每次写入前会保留完整历史快照。</span>
              </section>
              <section className="document-request" aria-label="围绕当前科研档案提问">
                <div>
                  <p className="eyebrow">档案助手</p>
                  <h2>直接描述你想做什么</h2>
                  <p>例如“补充研究方案”“和新导入论文比对”“梳理证据与假设”或“审查创新性”。若要根据新论文更新档案，请带上论文标题或 ID；系统会先综合审查，再请求确认写入。</p>
                </div>
                <form onSubmit={(event) => void submitResearchDocumentRequest(event, activeWorkbenchTab.document)}>
                  <textarea
                    value={documentRequest}
                    onChange={(event) => setDocumentRequest(event.target.value)}
                    placeholder="围绕这份科研档案提出问题或修改要求…"
                    rows={2}
                    disabled={isSending || !activeSessionId}
                  />
                  <div>
                    <span>会自动关联当前档案，不会把全文塞入对话上下文。</span>
                    <button className="button button-primary" type="submit" disabled={!documentRequest.trim() || isSending || !activeSessionId}>
                      发送给 Agent
                    </button>
                  </div>
                </form>
              </section>
              <section className="document-outline" aria-label="科研档案章节目录">
                {activeWorkbenchTab.document.sections.map((section) => (
                  <div className="document-outline-item" key={section.section_id}>
                    <strong>{section.heading}</strong>
                    <span>{section.content_length} 字符 · {section.summary || "待补充"}</span>
                  </div>
                ))}
              </section>
              <div className="document-page-content">
                <Suspense fallback={<p className="markdown-loading">{activeWorkbenchTab.document.content}</p>}>
                  <MarkdownContent content={activeWorkbenchTab.document.content} />
                </Suspense>
              </div>
                {activeWorkbenchTab.document.versions.length > 1 && (
                <section className="document-version-history" aria-label="版本历史">
                  <h2>版本历史</h2>
                  <p>当前内容及每个写入前的完整快照均可由 Agent 在你确认后恢复。</p>
                  <ul>
                    {activeWorkbenchTab.document.versions.map((version) => (
                      <li key={`${version.revision}-${version.updated_at}`}>
                        第 {version.revision} 版 · {version.current ? "当前版本" : "可恢复快照"} · {version.updated_at}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              <section className="research-ledger" aria-label="证据与假设账本">
                <header className="research-ledger-heading">
                  <div>
                    <p className="eyebrow">研究判断索引</p>
                    <h2>证据与假设账本</h2>
                  </div>
                  <span>账本修订 {activeWorkbenchTab.document.ledger.ledger_revision}</span>
                </header>
                <p className="research-ledger-summary">
                  {activeWorkbenchTab.document.ledger.summary.items} 项判断 · 已支持 {activeWorkbenchTab.document.ledger.summary.supported} · 存在争议 {activeWorkbenchTab.document.ledger.summary.contested} · 待复核 {activeWorkbenchTab.document.ledger.summary.stale_links}
                </p>
                {activeWorkbenchTab.document.ledger.items.length ? (
                  <div className="research-ledger-list">
                    {activeWorkbenchTab.document.ledger.items.map((item) => (
                      <article className={`research-ledger-item ${item.section_current ? "" : "stale"}`} key={item.item_id}>
                        <div className="research-ledger-item-meta">
                          <span>{item.heading}</span>
                          <span>{ledgerKindLabel(item.kind)} · {ledgerStatusLabel(item.status)}</span>
                          {!item.section_current && <span className="ledger-stale">正文已改动，需复核</span>}
                        </div>
                        <strong>{item.statement}</strong>
                        {item.falsification && <p>可证伪条件：{item.falsification}</p>}
                        {item.evidence.length > 0 && (
                          <ul>
                            {item.evidence.map((evidence, index) => (
                              <li key={`${item.item_id}-${index}`}>
                                {ledgerRelationLabel(evidence.relation)}：{evidence.title || evidence.paper_id || "未命名论文"}
                                {evidence.page ? ` · 第 ${evidence.page} 页` : ""}
                                {evidence.chunk_index !== null ? ` · 切块 ${evidence.chunk_index}` : ""}
                                {evidence.note ? ` · ${evidence.note}` : ""}
                              </li>
                            ))}
                          </ul>
                        )}
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="research-ledger-empty">尚未记录研究判断。可在上方直接要求梳理证据与假设；Agent 会先提出条目，只有你确认后才会写入。</p>
                )}
              </section>
            </article>
          ) : activeWorkbenchTab?.kind === "experiment" ? (
            <article className="document-page experiment-page" aria-label={`论文复现实验：${activeWorkbenchTab.project.paper_title}`}>
              <header className="document-page-heading">
                <div>
                  <p className="eyebrow">论文代码复现</p>
                  <h1>{activeWorkbenchTab.project.paper_title}</h1>
                  <p className="document-page-meta">
                    <span className={`status status-${activeWorkbenchTab.project.status}`}>{activeWorkbenchTab.project.status}</span>
                    {" · "}{activeWorkbenchTab.project.reproduction_level === "exact" ? "精确复现规格" : activeWorkbenchTab.project.reproduction_level === "approximate" ? "近似复现规格" : "待确认规格"}
                    {" · 修订 "}{activeWorkbenchTab.project.revision}
                  </p>
                </div>
                <div className="experiment-page-actions">
                  <button className="button button-secondary" type="button" onClick={() => void selectExperimentProject(activeWorkbenchTab.project.project_id)}>
                    刷新项目
                  </button>
                  {activeWorkbenchTab.project.status !== "ready" && (
                    <button
                      className="button button-primary"
                      type="button"
                      onClick={() => void retryExperimentProject(activeWorkbenchTab.project)}
                      disabled={Boolean(experimentStarting)}
                    >
                      {experimentStarting === `retry:${activeWorkbenchTab.project.project_id}` ? "重试中…" : "重试生成"}
                    </button>
                  )}
                  <button
                    className="button button-danger"
                    type="button"
                    onClick={() => void removeExperimentProject(activeWorkbenchTab.project)}
                    disabled={["queued", "running"].includes(activeWorkbenchTab.project.status)}
                    title={["queued", "running"].includes(activeWorkbenchTab.project.status) ? "任务执行中，请先等待完成" : "删除项目及其文件"}
                  >
                    删除项目
                  </button>
                </div>
              </header>
              <div className="document-page-content experiment-page-content">
                <section className="experiment-summary">
                  <h2>当前状态</h2>
                  <p>{activeWorkbenchTab.project.summary || "正在等待任务生成实验规格。"}</p>
                  <p className="experiment-note">首版只生成证据驱动的规格、代码与静态校验，不会下载数据、执行训练或宣称论文指标已复现。</p>
                </section>
                <section className="experiment-source">
                  <div className="experiment-source-heading">
                    <div>
                      <h2>代码来源</h2>
                      <p>{activeExperimentSource ? codeSourceLabel(activeExperimentSource) : "尚未检查"}</p>
                    </div>
                    <div className="experiment-source-actions">
                      <button
                        className="button button-secondary"
                        type="button"
                        onClick={() => void searchExperimentRepositories(activeWorkbenchTab.project)}
                        disabled={Boolean(experimentStarting)}
                      >
                        {experimentStarting === `source:${activeWorkbenchTab.project.project_id}` ? "检索中…" : "查找 GitHub 候选"}
                      </button>
                      <button
                        className="button button-secondary"
                        type="button"
                        onClick={() => void startExperimentMethodReconstruction(activeWorkbenchTab.project)}
                        disabled={Boolean(experimentStarting)}
                      >
                        {experimentStarting === `method:${activeWorkbenchTab.project.project_id}` ? "还原中…" : "按论文方法还原"}
                      </button>
                    </div>
                  </div>
                  {activeExperimentSource?.mode === "repository" && activeExperimentSource.repository?.repository_url ? (
                    <div className="experiment-repository-connected">
                      <a href={activeExperimentSource.repository.repository_url} target="_blank" rel="noreferrer">
                        {activeExperimentSource.repository.full_name || activeExperimentSource.repository.repository_url}
                      </a>
                      <span>{activeExperimentSource.repository.commit_sha ? `commit ${activeExperimentSource.repository.commit_sha.slice(0, 12)}` : "未取得 commit"}</span>
                      <button
                        className="button button-primary"
                        type="button"
                        onClick={() => void prepareExperimentRepository(activeWorkbenchTab.project)}
                        disabled={Boolean(experimentStarting) || !activeExperimentSource.repository.commit_sha}
                      >
                        {experimentStarting === `prepare:${activeWorkbenchTab.project.project_id}` ? "准备中…" : "同步并分析上游代码"}
                      </button>
                      <p>会克隆固定 commit 并解析 README、依赖文件与训练入口；不会安装依赖、下载数据或执行代码。</p>
                    </div>
                  ) : activeExperimentSource?.mode === "method_reconstruction" ? (
                    <p className="experiment-note">当前产物是论文方法还原，不是原作者代码复现；关键缺失信息会继续显示在待确认项中。</p>
                  ) : (
                    <p className="experiment-note">可按论文标题检索候选，或直接粘贴一个 GitHub 仓库地址。检索结果只代表候选，不会自动标为官方实现。</p>
                  )}
                  {Array.isArray(activeExperimentSource?.candidates) && activeExperimentSource.candidates.length > 0 && (
                    <div className="experiment-repository-candidates">
                      {activeExperimentSource.candidates.map((candidate) => (
                        <div key={candidate.repository_url || candidate.full_name} className="experiment-repository-candidate">
                          <div>
                            <a href={candidate.repository_url} target="_blank" rel="noreferrer">{candidate.full_name || candidate.repository_url}</a>
                            <p>{candidate.description || "GitHub 未提供简介"}{typeof candidate.stars === "number" ? ` · ★ ${candidate.stars}` : ""}</p>
                          </div>
                          <button
                            className="button button-secondary"
                            type="button"
                            onClick={() => void connectExperimentCodeRepository(activeWorkbenchTab.project, candidate.repository_url || "", "candidate_unverified")}
                            disabled={Boolean(experimentStarting) || !candidate.repository_url}
                          >
                            使用此候选
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  {activeRepositoryPreparation && (
                    <div className="experiment-repository-plan">
                      <div>
                        <strong>{activeRepositoryPreparation.status === "failed" ? "上游仓库准备失败" : "上游仓库已准备"}</strong>
                        <span>{activeRepositoryPreparation.local_path || "repository/source"}{activeRepositoryPreparation.commit_sha ? ` · ${activeRepositoryPreparation.commit_sha.slice(0, 12)}` : ""}</span>
                      </div>
                      <p>依赖声明：{activeRepositoryPreparation.dependency_files?.join("、") || "未找到"}</p>
                      <p>训练/评测入口候选：{activeRepositoryPreparation.entrypoint_candidates?.join("、") || "未找到"}</p>
                      {activeRepositoryPreparation.has_submodules && <p>检测到 submodule，尚未初始化。</p>}
                      {activeRepositoryPreparation.scan_truncated && <p>仓库文件较多，静态清单已按上限截断。</p>}
                      {activeRepositoryPreparation.status === "failed" && <p>最近错误：{activeRepositoryPreparation.error || "未知错误"}</p>}
                      <p className="experiment-note">下一阶段需先确认数据集位置、执行入口与资源预算，再显式安装依赖和运行。</p>
                    </div>
                  )}
                  {activeMethodReconstruction && (
                    <div className="experiment-method-plan">
                      <div>
                        <strong>论文方法还原参考实现</strong>
                        <span>{activeMethodReconstruction.source_ref_count || 0} 条页面级证据定位</span>
                      </div>
                      <p>{activeMethodReconstruction.method_summary || "尚未可靠提取方法概述，请先查看待确认项。"}</p>
                      {activeMethodReconstruction.components?.length ? (
                        <ul>
                          {activeMethodReconstruction.components.map((component, index) => (
                            <li key={`${component.name || "component"}-${index}`}>
                              <strong>{component.name || "未命名组件"}</strong>
                              {component.responsibility ? `：${component.responsibility}` : ""}
                              {component.evidence_ids?.length ? <span>（证据：{component.evidence_ids.join("、")}）</span> : <span>（未定位到直接证据）</span>}
                            </li>
                          ))}
                        </ul>
                      ) : <p>未能可靠拆出方法组件，已保留可编辑骨架。</p>}
                      {activeMethodReconstruction.assumptions?.length ? <p>工程假设：{activeMethodReconstruction.assumptions.join("；")}</p> : null}
                      <p className="experiment-note">这里只完成源代码草案和静态检查。补齐数据、参数或实现事实后，可通过“在对话中确认”写回项目。</p>
                    </div>
                  )}
                  <form
                    className="experiment-repository-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void connectExperimentCodeRepository(activeWorkbenchTab.project, repositoryUrlDraft, repositoryRelationship);
                    }}
                  >
                    <input
                      value={repositoryUrlDraft}
                      onChange={(event) => setRepositoryUrlDraft(event.target.value)}
                      placeholder="https://github.com/owner/repo"
                      aria-label="GitHub 仓库地址"
                      disabled={Boolean(experimentStarting)}
                    />
                    <select
                      value={repositoryRelationship}
                      onChange={(event) => setRepositoryRelationship(event.target.value as Exclude<RepositoryRelationship, "candidate_unverified">)}
                      aria-label="仓库来源性质"
                      disabled={Boolean(experimentStarting)}
                    >
                      <option value="user_provided">用户提供，待核验</option>
                      <option value="official_confirmed">我已确认是官方实现</option>
                      <option value="community">社区实现</option>
                    </select>
                    <button className="button button-primary" type="submit" disabled={Boolean(experimentStarting) || !repositoryUrlDraft.trim()}>
                      连接仓库
                    </button>
                  </form>
                </section>
                <section className="experiment-grid">
                  <div>
                    <h2>待确认项</h2>
                    {Array.isArray(activeWorkbenchTab.project.spec.unknowns) && activeWorkbenchTab.project.spec.unknowns.length ? (
                      <ul>{activeWorkbenchTab.project.spec.unknowns.map((item, index) => <li key={`${String(item)}-${index}`}>{String(item)}</li>)}</ul>
                    ) : <p>没有记录待确认项。</p>}
                  </div>
                  <div>
                    <h2>验证结果</h2>
                    {activeWorkbenchTab.project.validation.valid ? <p className="validation-ok">已通过代码静态验证。</p> : (
                      <ul className="validation-errors">
                        {(activeWorkbenchTab.project.validation.errors || ["尚未完成静态验证。"]).map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
                      </ul>
                    )}
                  </div>
                </section>
                <section className="experiment-confirmation">
                  <div>
                    <h2>如何确认待确认项？</h2>
                    <p>点击右侧按钮后，会在当前会话的输入框中放入一段项目专属指令。补全数据集、参数、实现约束等已知事实，再发送给 Agent；它只会写入明确确认的信息。</p>
                  </div>
                  <button className="button button-primary" type="button" onClick={() => prepareExperimentConfirmation(activeWorkbenchTab.project)}>
                    在对话中确认
                  </button>
                </section>
                <section className="experiment-spec-section">
                  <h2>论文证据范围</h2>
                  <p>{Array.isArray(activeWorkbenchTab.project.spec.source_refs) ? `已保留 ${activeWorkbenchTab.project.spec.source_refs.length} 条页面级来源定位；具体参数和数据集仅在证据充分时写入规格。` : "尚未提取来源定位。"}</p>
                </section>
                <section className="experiment-files-section">
                  <h2>项目文件</h2>
                  {activeWorkbenchTab.project.files.length ? (
                    <div className="experiment-file-layout">
                      <div className="experiment-file-list">
                        {activeWorkbenchTab.project.files.map((file) => (
                          <button
                            key={file.path}
                            type="button"
                            className={selectedExperimentFile?.path === file.path ? "selected" : ""}
                            onClick={() => void openExperimentFile(activeWorkbenchTab.project.project_id, file.path)}
                          >
                            <strong>{file.path}</strong><span>{Math.max(1, Math.ceil(file.size_bytes / 1024))} KB</span>
                          </button>
                        ))}
                      </div>
                      <pre className="experiment-file-preview"><code>{selectedExperimentFile?.content || "选择一个文件查看内容。"}</code></pre>
                    </div>
                  ) : <p>项目尚未生成代码文件。</p>}
                </section>
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
                              <span>{runKindLabel(run.kind)}</span>
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
                  placeholder={canSteerCurrentRun
                    ? "补充当前任务的约束、线索或更正…"
                    : mode === "research" ? "输入研究问题，开始深度研究…" : "提出问题，开始对话…"}
                  rows={3}
                  disabled={(!canSteerCurrentRun && isSending) || !activeSessionId}
                />
                <div className="composer-footer">
                  <span>{canSteerCurrentRun
                    ? "补充会在下一模型或研究节点使用，不能打断已经发出的请求。"
                    : attachment ? "发送后会在后台处理附件，进度显示在右侧。"
                      : mode === "research" ? "可选择来源范围，或继续上次未完成研究" : "支持引用、公式与 Markdown"}</span>
                  <div className="composer-actions">
                    {isSending && currentRun && !["completed", "cancelled", "failed"].includes(currentRun.status) && (
                      <button
                        className="button button-secondary"
                        type="button"
                        onClick={() => void stopCurrentRun()}
                        disabled={isCancelling}
                        title="协作式停止当前任务；深度研究已收集的证据会被保留"
                      >
                        {isCancelling ? "正在暂停…" : "暂停任务"}
                      </button>
                    )}
                    <button
                      className="button button-primary"
                      type="submit"
                      disabled={isUploading || (!draft.trim() && !attachment) || !activeSessionId || (isSending && !canSteerCurrentRun) || (canSteerCurrentRun && Boolean(attachment))}
                    >
                      {canSteerCurrentRun ? "发送补充" : isSending ? "任务进行中…" : mode === "research" ? "开始研究" : "发送"}
                    </button>
                  </div>
                </div>
              </form>
            </>
          )}
        </section>

        <RunInspector
          run={currentRun}
          events={runEvents}
          onCancel={() => void stopCurrentRun()}
          onRetry={() => void retryCurrentChat()}
          onOpenRunCenter={() => void openRunCenter()}
          cancelling={isCancelling}
        />
      </div>
    </main>
  );
}

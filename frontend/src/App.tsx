import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  getWorkspaceProfile,
  getWorkspaceSettings,
  listPapers,
  listResearchDocuments,
  listSessions,
  updateWorkspaceSettings,
} from "./api";
import {
  experimentCodeSource,
  methodReconstruction,
  modeLabel,
  repositoryPreparation,
} from "./lib/labels";
import { ChatMessage } from "./components/ChatMessage";
import { RunInspector } from "./components/RunInspector";
import { DailyPanel } from "./components/workspace/DailyPanel";
import { ExperimentPanel } from "./components/workspace/ExperimentPanel";
import { DocumentPanel } from "./components/workspace/DocumentPanel";
import { DailyRunPanel, ProfilePanel } from "./components/workspace/SimplePanels";
import { RunsPanel } from "./components/workspace/RunsPanel";
import { userFacingError } from "./lib/errors";
import type { Notice, WorkbenchTab, WorkspacePanel } from "./lib/workspace";
import { useConversation } from "./hooks/useConversation";
import { useDailyWorkspace } from "./hooks/useDailyWorkspace";
import { useDocuments } from "./hooks/useDocuments";
import { useRunCenter } from "./hooks/useRunCenter";
import { useExperiments } from "./hooks/useExperiments";
import { SessionSidebar } from "./components/workspace/SessionSidebar";
import {
  type Paper,
  type ResearchScope,
  type WorkspaceSettings,
} from "./types";


const API_TOKEN_KEY = "research-agent.api-token";
const ACTIVE_SESSION_KEY = "research-agent.active-session";

export function App() {
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem(API_TOKEN_KEY) || "");
  const [apiKeyDraft, setApiKeyDraft] = useState(() => sessionStorage.getItem(API_TOKEN_KEY) || "");



  const [isLoading, setIsLoading] = useState(true);
  const [notice, setNotice] = useState<Notice>(null);
  const [openWorkspacePanel, setOpenWorkspacePanel] = useState<WorkspacePanel | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState<WorkspacePanel | null>(null);
  const [workbenchTabs, setWorkbenchTabs] = useState<WorkbenchTab[]>([
    { id: "chat", kind: "chat", title: "对话" },
  ]);
  const [activeWorkbenchTabId, setActiveWorkbenchTabId] = useState("chat");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [workspaceSettings, setWorkspaceSettings] = useState<WorkspaceSettings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState({
    model: "",
    rag_enabled: true,
    pdf_max_pages: 15,
    daily_search_enabled: false,
  });

  const {
    canSteerCurrentRun,
    sessions,
    activeSessionId,
    messages,
    sessionUsage,
    draft,
    mode,
    researchScope,
    currentRun,
    runEvents,
    isSending,
    isCancelling,
    attachment,
    isUploading,
    setDraft,
    setMode,
    setResearchScope,
    setMessages,
    setCurrentRun,
    setRunEvents,
    setIsSending,
    setAttachment,
    setActiveSessionId,
    setSessions,
    setSessionUsage,
    messageListRef,
    uploadInputRef,
    abortRef,
    loadSession,
    selectSession,
    startSession,
    removeActiveSession,
    sendMessage,
    stopCurrentRun,
    retryCurrentChat,
    selectAttachment,
  } = useConversation({
    apiKey,
    setNotice,
    setActiveWorkbenchTabId,
  });

  const {
    dailyDigest,
    dailyRuns,
    dailyLoading,
    dailyAction,
    dailySearchDraft,
    keywords,
    keywordDraft,
    setKeywordDraft,
    setDailySearchDraft,
    selectDailyRun,
    addDailyKeyword,
    removeDailyKeyword,
    loadKeywords,
    replaceDailyRuns,
    loadDailyCenter,
    openDailyCenter,
    updateDailyPaper,
    startDailyAction,
  } = useDailyWorkspace({
    apiKey,
    setNotice,
    setWorkbenchTabs,
    setActiveWorkbenchTabId,
    setCurrentRun,
    isSending,
    setIsSending,
    setRunEvents,
    abortRef,
  });

  const {
    runCenterRuns,
    badcases,
    runCenterLoading,
    selectedRunId,
    badcaseSubmitting,
    badcaseCategories,
    manualBadcaseRunId,
    manualBadcaseCategory,
    manualBadcaseNote,
    setSelectedRunId,
    setBadcaseCategories,
    setManualBadcaseRunId,
    setManualBadcaseCategory,
    setManualBadcaseNote,
    loadRunCenter,
    selectRunForTimeline,
    openRunCenter,
    markRunAsBadcase,
    markManualBadcase,
  } = useRunCenter({
    apiKey,
    activeSessionId,
    setNotice,
    setSessionUsage,
    replaceDailyRuns,
    setWorkbenchTabs,
    setActiveWorkbenchTabId,
  });

  const {
    documents,
    documentRequest,
    profile,
    setDocumentRequest,
    setDocuments,
    setProfile,
    selectResearchDocument,
    submitResearchDocumentRequest,
    downloadDocument,
    removeResearchDocument,
    openProfile,
  } = useDocuments({
    apiKey,
    isSending,
    setNotice,
    setWorkbenchTabs,
    setActiveWorkbenchTabId,
    closeWorkbenchTab,
    setMode,
    sendMessage,
  });

  const {
    experiments,
    refreshExperiments,
    experimentStarting,
    experimentFiles,
    repositoryUrlDraft,
    repositoryRelationship,
    setRepositoryUrlDraft,
    setRepositoryRelationship,
    selectExperimentProject,
    openExperimentFile,
    startPaperReproduction,
    retryExperimentProject,
    searchExperimentRepositories,
    connectExperimentCodeRepository,
    prepareExperimentRepository,
    startExperimentMethodReconstruction,
    prepareExperimentConfirmation,
    removeExperimentProject,
  } = useExperiments({
    apiKey,
    activeSessionId,
    setNotice,
    setDraft,
    setWorkbenchTabs,
    setActiveWorkbenchTabId,
    closeWorkbenchTab,
  });



  const activeWorkbenchTab = workbenchTabs.find((tab) => tab.id === activeWorkbenchTabId) || workbenchTabs[0];
  const selectedDocumentId = activeWorkbenchTab?.kind === "document"
    ? activeWorkbenchTab.document.document_id
    : null;
  const selectedExperimentId = activeWorkbenchTab?.kind === "experiment"
    ? activeWorkbenchTab.project.project_id
    : null;
  const selectedExperimentFile = selectedExperimentId ? experimentFiles[selectedExperimentId] : null;
  useEffect(() => {
    const list = messageListRef.current;
    if (list) {
      list.scrollTop = list.scrollHeight;
    }
  }, [messages]);

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
        await refreshExperiments();
      } else if (panel === "keywords") {
        await loadKeywords();
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

  function closeWorkbenchTab(tabId: string) {
    if (tabId === "chat") {
      return;
    }
    const closingIndex = workbenchTabs.findIndex((tab) => tab.id === tabId);
    const fallbackTabId = workbenchTabs[closingIndex - 1]?.id || "chat";
    setWorkbenchTabs((current) => current.filter((tab) => tab.id !== tabId));
    setActiveWorkbenchTabId((current) => current === tabId ? fallbackTabId : current);
  }

  const selectedRun = runCenterRuns.find((run) => run.run_id === selectedRunId) || null;

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

  const documentActive = activeWorkbenchTab?.kind === "document";
  const runInProgress =
    isSending ||
    ["running", "queued", "cancelling"].includes(currentRun?.status ?? "");
  const docFocusMode = documentActive && !runInProgress;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Research Agent 首页">
          <span className="brand-mark" aria-hidden="true">R</span>
          <span>Research Agent</span>
        </a>
        <p>论文证据、研究推理与可追溯运行</p>
      </header>

      <div className={`workspace ${docFocusMode ? "workspace-doc-focus" : ""}`}>
        <SessionSidebar
          sessions={sessions}
          activeSessionId={activeSessionId}
          isSending={isSending}
          openWorkspacePanel={openWorkspacePanel}
          workspaceLoading={workspaceLoading}
          documents={documents}
          selectedDocumentId={selectedDocumentId}
          profile={profile}
          papers={papers}
          experiments={experiments}
          selectedExperimentId={selectedExperimentId}
          experimentStarting={experimentStarting}
          dailyRuns={dailyRuns}
          keywords={keywords}
          keywordDraft={keywordDraft}
          workspaceSettings={workspaceSettings}
          settingsDraft={settingsDraft}
          apiKeyDraft={apiKeyDraft}
          onSelectSession={(sessionId) => void selectSession(sessionId)}
          onStartSession={() => void startSession()}
          onRemoveActiveSession={() => void removeActiveSession()}
          onToggleWorkspacePanel={(panel) => void toggleWorkspacePanel(panel as WorkspacePanel)}
          onSelectResearchDocument={(documentId) => void selectResearchDocument(documentId)}
          onRemoveResearchDocument={(document) => void removeResearchDocument(document)}
          onOpenProfile={() => void openProfile()}
          onPreparePaperRelationReview={(paper) => preparePaperRelationReview(paper)}
          onStartPaperReproduction={(paper) => void startPaperReproduction(paper)}
          onSelectExperimentProject={(projectId) => void selectExperimentProject(projectId)}
          onSelectDailyRun={(runId) => void selectDailyRun(runId)}
          onOpenDailyCenter={() => void openDailyCenter()}
          onAddDailyKeyword={(event) => void addDailyKeyword(event)}
          onRemoveDailyKeyword={(keyword) => void removeDailyKeyword(keyword)}
          onKeywordDraftChange={setKeywordDraft}
          onSaveWorkspaceSettings={(event) => void saveWorkspaceSettings(event)}
          onSettingsDraftChange={setSettingsDraft}
          onApiKeyDraftChange={setApiKeyDraft}
          onApplyApiKey={applyApiKey}
        />

        <section className={`conversation ${documentActive ? "conversation-doc" : ""}`} aria-label="研究工作台">
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
            <ProfilePanel profile={activeWorkbenchTab.profile} />
          ) : activeWorkbenchTab?.kind === "document" ? (
            <DocumentPanel
              document={activeWorkbenchTab.document}
              activeSessionId={activeSessionId}
              isSending={isSending}
              documentRequest={documentRequest}
              onDocumentRequestChange={setDocumentRequest}
              onSubmitRequest={(event, doc) => void submitResearchDocumentRequest(event, doc)}
              onDownload={(doc) => void downloadDocument(doc)}
              onRemove={(doc) => void removeResearchDocument(doc)}
            />
          ) : activeWorkbenchTab?.kind === "experiment" ? (
            <ExperimentPanel
              project={activeWorkbenchTab.project}
              source={activeExperimentSource}
              preparation={activeRepositoryPreparation}
              reconstruction={activeMethodReconstruction}
              starting={experimentStarting}
              selectedFile={selectedExperimentFile}
              repositoryUrlDraft={repositoryUrlDraft}
              repositoryRelationship={repositoryRelationship}
              onRepositoryUrlDraftChange={setRepositoryUrlDraft}
              onRepositoryRelationshipChange={setRepositoryRelationship}
              onRefresh={(projectId) => void selectExperimentProject(projectId)}
              onRetry={(project) => void retryExperimentProject(project)}
              onRemove={(project) => void removeExperimentProject(project)}
              onSearchRepositories={(project) => void searchExperimentRepositories(project)}
              onConnectRepository={(project, url, relationship) => void connectExperimentCodeRepository(project, url, relationship)}
              onPrepareRepository={(project) => void prepareExperimentRepository(project)}
              onStartMethodReconstruction={(project) => void startExperimentMethodReconstruction(project)}
              onPrepareConfirmation={(project) => prepareExperimentConfirmation(project)}
              onOpenFile={(projectId, path) => void openExperimentFile(projectId, path)}
            />
          ) : activeWorkbenchTab?.kind === "dailyRun" ? (
            <DailyRunPanel title={activeWorkbenchTab.title} detail={activeWorkbenchTab.detail} />
          ) : activeWorkbenchTab?.kind === "runs" ? (
            <RunsPanel
              apiKey={apiKey}
              runs={runCenterRuns}
              loading={runCenterLoading}
              selectedRun={selectedRun}
              selectedRunId={selectedRunId}
              sessionUsage={sessionUsage}
              badcases={badcases}
              badcaseCategories={badcaseCategories}
              badcaseSubmitting={badcaseSubmitting}
              manualBadcaseRunId={manualBadcaseRunId}
              manualBadcaseCategory={manualBadcaseCategory}
              manualBadcaseNote={manualBadcaseNote}
              onSelectRunForTimeline={selectRunForTimeline}
              onSelectedRunIdChange={setSelectedRunId}
              onMarkRunAsBadcase={(run) => void markRunAsBadcase(run)}
              onMarkManualBadcase={() => void markManualBadcase()}
              onBadcaseCategoriesChange={setBadcaseCategories}
              onManualBadcaseRunIdChange={setManualBadcaseRunId}
              onManualBadcaseCategoryChange={setManualBadcaseCategory}
              onManualBadcaseNoteChange={setManualBadcaseNote}
              onLoadRunCenter={() => void loadRunCenter()}
            />

          ) : activeWorkbenchTab?.kind === "daily" ? (
            <DailyPanel
              digest={dailyDigest}
              loading={dailyLoading}
              action={dailyAction}
              searchDraft={dailySearchDraft}
              runs={dailyRuns}
              isSending={isSending}
              onSearchDraftChange={setDailySearchDraft}
              onStartDailyAction={(kind) => void startDailyAction(kind)}
              onLoadDailyCenter={() => void loadDailyCenter()}
              onUpdatePaper={(paper, status) => void updateDailyPaper(paper, status)}
              onSelectRun={(runId) => void selectDailyRun(runId)}
            />

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

        {!docFocusMode && (
          <RunInspector
            run={currentRun}
            events={runEvents}
            onCancel={() => void stopCurrentRun()}
            onRetry={() => void retryCurrentChat()}
            onOpenRunCenter={() => void openRunCenter()}
            cancelling={isCancelling}
          />
        )}
      </div>
    </main>
  );
}

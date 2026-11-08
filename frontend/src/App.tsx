import { useEffect, useState, type FormEvent } from "react";

import {
  updateWorkspaceSettings,
} from "./api";
import {
  experimentCodeSource,
  methodReconstruction,
  repositoryPreparation,
  statusLabel,
} from "./lib/labels";
import { ChatPanel } from "./components/workspace/ChatPanel";
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
import { useWorkspaceChrome } from "./hooks/useWorkspaceChrome";
import { useExperiments } from "./hooks/useExperiments";
import { useWorkspaceLayout } from "./hooks/useWorkspaceLayout";
import { SidebarResizeHandle } from "./components/SidebarResizeHandle";
import { SessionSidebar } from "./components/workspace/SessionSidebar";
import {
  type Paper,
  type WorkspaceSettings,
} from "./types";


const API_TOKEN_KEY = "research-agent.api-token";

function SidebarIcon({ side }: { side: "left" | "right" }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
      <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
      <path d={side === "left" ? "M7.5 4v12" : "M12.5 4v12"} />
    </svg>
  );
}

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
    sessionRunStatuses,
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
    setAttachment,
    setSessionUsage,
    messageListRef,
    uploadInputRef,
    loadWorkspace,
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
    setIsLoading,
  });

  const {
    dailyCurrentRun,
    dailyRunEvents,
    dailySending,
    dailyCancelling,
    stopDailyRun,
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

  const {
    toggleWorkspacePanel,
    preparePaperRelationReview,
  } = useWorkspaceChrome({
    apiKey,
    setNotice,
    setDocuments,
    setProfile,
    setPapers,
    setWorkspaceSettings,
    setSettingsDraft,
    openWorkspacePanel,
    setOpenWorkspacePanel,
    setWorkspaceLoading,
    setDraft,
    setWorkbenchTabs,
    setActiveWorkbenchTabId,
    refreshExperiments,
    loadKeywords,
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

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

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

  const inspectingDaily = activeWorkbenchTab?.kind === "daily" || activeWorkbenchTab?.kind === "dailyRun";
  const inspectedRun = inspectingDaily ? dailyCurrentRun : currentRun;
  const inspectedEvents = inspectingDaily ? dailyRunEvents : runEvents;
  const documentActive = activeWorkbenchTab?.kind === "document";
  const runInProgress =
    isSending || dailySending ||
    ["running", "queued", "cancelling"].includes(currentRun?.status ?? "");
  const docFocusMode = documentActive && !runInProgress;
  const layout = useWorkspaceLayout({ docFocusMode });
  const activeSessionTitle = sessions.find((session) => session.thread_id === activeSessionId)?.title || "对话";
  const runNeedsAttention = inspectedRun && ["failed", "partial_failed"].includes(inspectedRun.status);

  return (
    <main className="app-shell">
      <div
        ref={layout.workspaceRef}
        style={layout.workspaceStyle}
        className={["workspace", !layout.leftVisible && "workspace-left-hidden", !layout.rightVisible && "workspace-right-hidden"].filter(Boolean).join(" ")}
      >
        <SessionSidebar
          hidden={!layout.leftVisible}
          sessions={sessions}
          activeSessionId={activeSessionId}
          sessionRunStatuses={sessionRunStatuses}
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
        >
          {layout.leftResizeProps && <SidebarResizeHandle {...layout.leftResizeProps} />}
        </SessionSidebar>

        <section className={`conversation ${documentActive ? "conversation-doc" : ""}`} aria-label="研究工作台">
          <div className="workbench-header">
            <button
              type="button"
              className="icon-button"
              onClick={layout.toggleLeft}
              aria-controls="session-sidebar"
              aria-expanded={layout.leftVisible}
              aria-label={layout.leftVisible ? "隐藏会话侧栏" : "显示会话侧栏"}
              title={layout.leftVisible ? "隐藏会话侧栏" : "显示会话侧栏"}
            ><SidebarIcon side="left" /></button>
          <div className="workbench-tabs" role="tablist" aria-label="工作台页面">
            {workbenchTabs.map((tab) => (
              <div className={`workbench-tab ${tab.id === activeWorkbenchTab?.id ? "active" : ""}`} key={tab.id}>
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab.id === activeWorkbenchTab?.id}
                  onClick={() => setActiveWorkbenchTabId(tab.id)}
                  title={tab.kind === "chat" ? activeSessionTitle : tab.title}
                >
                  {tab.kind === "document" && <span aria-hidden="true">▣</span>}
                  {tab.kind === "profile" && <span aria-hidden="true">◉</span>}
                  {tab.kind === "daily" && <span aria-hidden="true">◌</span>}
                  {tab.kind === "dailyRun" && <span aria-hidden="true">▧</span>}
                  {tab.kind === "experiment" && <span aria-hidden="true">⌘</span>}
                  {tab.kind === "runs" && <span aria-hidden="true">◫</span>}
                  <span>{tab.kind === "chat" ? activeSessionTitle : tab.title}</span>
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
            {inspectedRun && (
              <button
                type="button"
                className={`run-status-button status status-${inspectedRun.status}`}
                onClick={layout.showRight}
                aria-label={`查看运行详情：${statusLabel(inspectedRun.status)}`}
                title="查看运行详情"
              >{statusLabel(inspectedRun.status)}</button>
            )}
            <button
              type="button"
              className="icon-button"
              onClick={layout.toggleRight}
              aria-controls="run-inspector"
              aria-expanded={layout.rightVisible}
              aria-label={layout.rightVisible ? "隐藏运行检查器" : "显示运行检查器"}
              title={layout.rightVisible ? "隐藏运行检查器" : "显示运行检查器"}
            ><SidebarIcon side="right" /></button>
            <details className="layout-menu">
              <summary aria-label="布局选项" title="布局选项">⋯</summary>
              <div className="layout-menu-content">
                <button type="button" onClick={(event) => {
                  layout.resetLayout();
                  event.currentTarget.closest("details")?.removeAttribute("open");
                }}>恢复默认布局</button>
              </div>
            </details>
          </div>

          {notice && <p className={`notice notice-${notice.kind}`}>{notice.text}</p>}
          {!layout.rightVisible && runNeedsAttention && (
            <p className="run-alert" role="status">
              <span>任务{statusLabel(inspectedRun.status)}{inspectedRun.error_type ? `：${inspectedRun.error_type}` : ""}</span>
              <button className="text-button" type="button" onClick={layout.showRight}>查看运行详情</button>
            </p>
          )}
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
              isSending={dailySending}
              onSearchDraftChange={setDailySearchDraft}
              onStartDailyAction={(kind) => void startDailyAction(kind)}
              onLoadDailyCenter={() => void loadDailyCenter()}
              onUpdatePaper={(paper, status) => void updateDailyPaper(paper, status)}
              onSelectRun={(runId) => void selectDailyRun(runId)}
            />

          ) : (
            <ChatPanel
              messages={messages}
              draft={draft}
              mode={mode}
              researchScope={researchScope}
              currentRun={currentRun}
              isLoading={isLoading}
              isSending={isSending}
              isCancelling={isCancelling}
              isUploading={isUploading}
              canSteerCurrentRun={canSteerCurrentRun}
              activeSessionId={activeSessionId}
              attachment={attachment}
              messageListRef={messageListRef}
              uploadInputRef={uploadInputRef}
              setDraft={setDraft}
              setMode={setMode}
              setResearchScope={setResearchScope}
              setAttachment={setAttachment}
              sendMessage={sendMessage}
              stopCurrentRun={stopCurrentRun}
              selectAttachment={selectAttachment}
            />
          )}
        </section>

          <RunInspector
            hidden={!layout.rightVisible}
            run={inspectedRun}
            events={inspectedEvents}
            onCancel={() => void (inspectingDaily ? stopDailyRun() : stopCurrentRun())}
            onRetry={() => void retryCurrentChat()}
            onOpenRunCenter={() => void openRunCenter()}
            cancelling={inspectingDaily ? dailyCancelling : isCancelling}
          >
            {layout.rightResizeProps && <SidebarResizeHandle {...layout.rightResizeProps} />}
          </RunInspector>
      </div>
    </main>
  );
}

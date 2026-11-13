import { lazy, Suspense, useEffect, useState, type FormEvent } from "react";
import { ReaderErrorBoundary } from "./components/ReaderErrorBoundary";

import {
  updateWorkspaceSettings,
} from "./api";
import {
  statusLabel,
} from "./lib/labels";
import { ChatPanel } from "./components/workspace/ChatPanel";
import { RunInspector } from "./components/RunInspector";
import { DailyPanel } from "./components/workspace/DailyPanel";
import { DailyRunPanel, ProfilePanel } from "./components/workspace/SimplePanels";
import { RunsPanel } from "./components/workspace/RunsPanel";
import { userFacingError } from "./lib/errors";
import type { Notice, WorkbenchTab, WorkspacePanel } from "./lib/workspace";
import { useConversation } from "./hooks/useConversation";
import { useDailyWorkspace } from "./hooks/useDailyWorkspace";
import { useProfile } from "./hooks/useProfile";
import { useRunCenter } from "./hooks/useRunCenter";
import { useWorkspaceChrome } from "./hooks/useWorkspaceChrome";
import { useWorkspaceLayout } from "./hooks/useWorkspaceLayout";
import { SidebarResizeHandle } from "./components/SidebarResizeHandle";
import { SessionSidebar } from "./components/workspace/SessionSidebar";
import {
  type Paper,
  type WorkspaceSettings,
} from "./types";


import { LibraryPanel } from "./components/workspace/LibraryPanel";
import { SettingsPanel } from "./components/workspace/SettingsPanel";
import { useLibrary } from "./hooks/useLibrary";
import { type LibraryDocument, fileSize } from "./lib/library";
const PaperReader = lazy(() => import("./components/workspace/PaperReader").then(module => ({default: module.PaperReader})));

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


  const library = useLibrary(apiKey);
  const [selectedDocument, setSelectedDocument] = useState<LibraryDocument | null>(null);
  const [settingsSection, setSettingsSection] = useState<"general"|"daily"|"profile">("general");
  const [readerTools, setReaderTools] = useState<HTMLDivElement | null>(null);
  const [showRunTools, setShowRunTools] = useState(false);
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
    syncResearchProject,
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

  const { profile, setProfile, openProfile } = useProfile({
    apiKey,
    setNotice,
    setWorkbenchTabs,
    setActiveWorkbenchTabId,
  });

  const {
    toggleWorkspacePanel,
  } = useWorkspaceChrome({
    apiKey,
    setNotice,
    setProfile,
    setPapers,
    setWorkspaceSettings,
    setSettingsDraft,
    openWorkspacePanel,
    setOpenWorkspacePanel,
    setWorkspaceLoading,
    loadKeywords,
  });


  const activeWorkbenchTab = workbenchTabs.find((tab) => tab.id === activeWorkbenchTabId) || workbenchTabs[0];
  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  function openUtility(kind: "library"|"settings") {
    const tab: WorkbenchTab = kind === "library" ? {id:"library",kind:"library",title:"论文库"} : {id:"settings",kind:"settings",title:"设置"};
    setWorkbenchTabs(tabs => tabs.some(t => t.id === kind) ? tabs : [...tabs, tab]);
    setActiveWorkbenchTabId(kind);
    setShowRunTools(false);
    if (kind === "library") { void library.load(); void toggleWorkspacePanel("papers"); }
  }
  function openPaper(document: LibraryDocument) {
    const id = `paper:${document.id}`;
    setWorkbenchTabs(tabs => tabs.some(t => t.id === id) ? tabs : [...tabs, {id, kind:"paper", title:document.name, documentId:document.id}]);
    setActiveWorkbenchTabId(id);
    setShowRunTools(false);
  }
  function readWithAssistant(document: LibraryDocument) {
    const prompt = `请阅读论文“${document.name}”，调用 read_pdf，url_or_path 使用 ${JSON.stringify(`library/${document.id}.pdf`)}，max_pages 使用 ${Math.min(document.pages, 100)}，并建立可检索的论文索引。`;
    setDraft(draft.trim() ? `${draft}\n\n${prompt}` : prompt);
    setMode("chat");
    setActiveWorkbenchTabId("chat");
  }
  useEffect(() => {
    setSelectedDocument(current => current ? library.data.documents.find(d => d.id === current.id) || null : null);
  }, [library.data.documents]);
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

  const inspectingDaily = activeWorkbenchTab?.kind === "daily" || activeWorkbenchTab?.kind === "dailyRun" || (activeWorkbenchTab?.kind === "settings" && settingsSection === "daily");
  const inspectedRun = inspectingDaily ? dailyCurrentRun : currentRun;
  const inspectedEvents = inspectingDaily ? dailyRunEvents : runEvents;
  const layout = useWorkspaceLayout();
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
          onOpenLibrary={() => openUtility("library")}
          onOpenSettings={() => openUtility("settings")}
          sessions={sessions}
          activeSessionId={activeSessionId}
          sessionRunStatuses={sessionRunStatuses}
          isSending={isSending}
          openWorkspacePanel={openWorkspacePanel}
          workspaceLoading={workspaceLoading}
          profile={profile}
          papers={papers}
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
          onOpenProfile={() => void openProfile()}
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

        <section className={`conversation ${activeWorkbenchTab?.kind === "chat" ? "conversation-chat" : ""}`} aria-label="研究工作台">
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
                  title={tab.kind === "chat" ? activeSessionTitle : tab.kind === "paper" ? library.data.documents.find(d => d.id === tab.documentId)?.name || tab.title : tab.title}
                >
                  {tab.kind === "profile" && <span aria-hidden="true">◉</span>}
                  {tab.kind === "daily" && <span aria-hidden="true">◌</span>}
                  {tab.kind === "dailyRun" && <span aria-hidden="true">▧</span>}
                  {tab.kind === "runs" && <span aria-hidden="true">◫</span>}
                  <span>{tab.kind === "chat" ? activeSessionTitle : tab.kind === "paper" ? library.data.documents.find(d => d.id === tab.documentId)?.name || tab.title : tab.title}</span>
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
          </div>

          {notice && <p className={`notice notice-${notice.kind}`}>{notice.text}</p>}
          {!layout.rightVisible && runNeedsAttention && (
            <p className="run-alert" role="status">
              <span>任务{statusLabel(inspectedRun.status)}{inspectedRun.error_type ? `：${inspectedRun.error_type}` : ""}</span>
              <button className="text-button" type="button" onClick={layout.showRight}>查看运行详情</button>
            </p>
          )}
          {workbenchTabs.some(t => t.kind === "library") && <div className="workbench-preserved" hidden={activeWorkbenchTab?.kind !== "library"}><LibraryPanel apiKey={apiKey} {...library} onOpen={openPaper} onSelect={setSelectedDocument}/></div>}
          {workbenchTabs.some(t => t.kind === "settings") && <div className="workbench-preserved" hidden={activeWorkbenchTab?.kind !== "settings"}><SettingsPanel apiKey={apiKey} apiKeyDraft={apiKeyDraft} onApiKeyDraftChange={setApiKeyDraft} onApplyApiKey={applyApiKey} keywords={keywords} keywordDraft={keywordDraft} onKeywordDraftChange={setKeywordDraft} onAddKeyword={e => void addDailyKeyword(e)} onRemoveKeyword={word => void removeDailyKeyword(word)} section={settingsSection} onSectionChange={setSettingsSection} onLoadDaily={() => {void loadKeywords();void loadDailyCenter();}}>
            <DailyPanel digest={dailyDigest} loading={dailyLoading} action={dailyAction} searchDraft={dailySearchDraft} runs={dailyRuns} isSending={dailySending} onSearchDraftChange={setDailySearchDraft} onStartDailyAction={kind => void startDailyAction(kind)} onLoadDailyCenter={() => void loadDailyCenter()} onUpdatePaper={(paper,status) => void updateDailyPaper(paper,status)} onSelectRun={id => void selectDailyRun(id)}/>
          </SettingsPanel></div>}
          <Suspense fallback={<p>正在加载阅读器…</p>}>{workbenchTabs.filter(t => t.kind === "paper").map(tab => {
            if (tab.kind !== "paper") return null;
            const document = library.data.documents.find(d => d.id === tab.documentId);
            return document && <div key={tab.id} className="reader-boundary-host" hidden={activeWorkbenchTabId !== tab.id}><ReaderErrorBoundary><PaperReader apiKey={apiKey} document={document} active={activeWorkbenchTabId === tab.id} toolsHost={readerTools} onProgress={(id,progress) => {library.rememberProgress(id,progress);if(selectedDocument?.id===id)setSelectedDocument(current => current && {...current,progress});}}/></ReaderErrorBoundary></div>;
          })}</Suspense>
          {["library","settings","paper"].includes(activeWorkbenchTab?.kind || "") ? null : activeWorkbenchTab?.kind === "profile" ? (
            <ProfilePanel profile={activeWorkbenchTab.profile} />
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
              apiKey={apiKey}
              onResearchChange={syncResearchProject}
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

          {(activeWorkbenchTab?.kind === "library" || activeWorkbenchTab?.kind === "paper") && !showRunTools ? <aside id="run-inspector" className="run-inspector" hidden={!layout.rightVisible}>
            {layout.rightResizeProps && <SidebarResizeHandle {...layout.rightResizeProps}/>}
            <div className="context-toolbar"><strong>{activeWorkbenchTab.kind === "paper" ? "阅读辅助" : "文件详情"}</strong><button className="text-button" onClick={() => setShowRunTools(true)}>运行</button></div>
            <div className="run-inspector-content">{activeWorkbenchTab.kind === "paper" ? <div ref={setReaderTools}/> : selectedDocument ? <div className="file-details"><h2>{selectedDocument.name}</h2><dl><dt>文件大小</dt><dd>{fileSize(selectedDocument.size_bytes)}</dd><dt>页数</dt><dd>{selectedDocument.pages} 页</dd><dt>文件夹</dt><dd>{library.data.folders.find(f => f.id === selectedDocument.folder_id)?.name || "未分类"}</dd><dt>阅读进度</dt><dd>{selectedDocument.progress.page ? `第 ${selectedDocument.progress.page} 页` : "尚未阅读"}</dd></dl><button className="button button-secondary" disabled={selectedDocument.trashed} onClick={() => openPaper(selectedDocument)}>继续阅读 →</button><button className="button button-secondary" disabled={selectedDocument.trashed} onClick={() => readWithAssistant(selectedDocument)}>交给助手阅读</button></div> : <p className="panel-hint">勾选论文查看详情；点击名称打开独立阅读页。</p>}</div>
          </aside> : <RunInspector
            hidden={!layout.rightVisible}
            run={inspectedRun}
            events={inspectedEvents}
            onCancel={() => void (inspectingDaily ? stopDailyRun() : stopCurrentRun())}
            onRetry={() => void retryCurrentChat()}
            onOpenRunCenter={() => void openRunCenter()}
            cancelling={inspectingDaily ? dailyCancelling : isCancelling}
          >
            {layout.rightResizeProps && <SidebarResizeHandle {...layout.rightResizeProps} />}
            {showRunTools && <button className="button button-secondary" onClick={() => setShowRunTools(false)}>返回阅读 / 文件详情</button>}
          </RunInspector>}
      </div>
    </main>
  );
}

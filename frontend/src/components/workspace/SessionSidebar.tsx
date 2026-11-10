import type { Dispatch, FormEvent, ReactNode, SetStateAction } from "react";

import { statusLabel } from "../../lib/labels";
import { WorkspacePanel } from "../WorkspacePanel";
import type {
  DailyKeyword,
  Paper,
  ResearchDocument,
  Run,
  Session,
  UserProfile,
  WorkspaceSettings,
} from "../../types";

/**
 * The left rail: the conversation list, the collapsible workspace panels and
 * the API-token field.
 *
 * Extracted from `App.tsx` without behaviour change.  The panels here are
 * *navigation* into the workbench tabs rather than the panels themselves, so
 * this component only lists and selects.
 */
export function SessionSidebar({
  hidden = false,
  children,
  sessions,
  activeSessionId,
  sessionRunStatuses,
  isSending,
  openWorkspacePanel,
  workspaceLoading,
  documents,
  selectedDocumentId,
  profile,
  papers,
  dailyRuns,
  keywords,
  keywordDraft,
  workspaceSettings,
  settingsDraft,
  apiKeyDraft,
  onSelectSession,
  onStartSession,
  onRemoveActiveSession,
  onToggleWorkspacePanel,
  onSelectResearchDocument,
  onRemoveResearchDocument,
  onOpenProfile,
  onPreparePaperRelationReview,
  onSelectDailyRun,
  onOpenDailyCenter,
  onAddDailyKeyword,
  onRemoveDailyKeyword,
  onKeywordDraftChange,
  onSaveWorkspaceSettings,
  onSettingsDraftChange,
  onApiKeyDraftChange,
  onApplyApiKey,
}: {
  hidden?: boolean;
  children?: ReactNode;
  sessions: Session[];
  activeSessionId: string | null;
  sessionRunStatuses: Record<string, string>;
  isSending: boolean;
  openWorkspacePanel: string | null;
  workspaceLoading: string | null;
  documents: ResearchDocument[];
  selectedDocumentId: string | null;
  profile: UserProfile | null;
  papers: Paper[];
  dailyRuns: Run[];
  keywords: DailyKeyword[];
  keywordDraft: string;
  workspaceSettings: WorkspaceSettings | null;
  settingsDraft: { model: string; rag_enabled: boolean; pdf_max_pages: number; daily_search_enabled: boolean };
  apiKeyDraft: string;
  onSelectSession: (sessionId: string) => void;
  onStartSession: () => void;
  onRemoveActiveSession: () => void;
  onToggleWorkspacePanel: (panel: string) => void;
  onSelectResearchDocument: (documentId: string) => void;
  onRemoveResearchDocument: (document: ResearchDocument) => void;
  onOpenProfile: () => void;
  onPreparePaperRelationReview: (paper: Paper) => void;
  onSelectDailyRun: (runId: string) => void;
  onOpenDailyCenter: () => void;
  onAddDailyKeyword: (event: FormEvent<HTMLFormElement>) => void;
  onRemoveDailyKeyword: (keyword: string) => void;
  onKeywordDraftChange: (value: string) => void;
  onSaveWorkspaceSettings: (event: FormEvent<HTMLFormElement>) => void;
  onSettingsDraftChange: Dispatch<SetStateAction<{ model: string; rag_enabled: boolean; pdf_max_pages: number; daily_search_enabled: boolean }>>;
  onApiKeyDraftChange: (value: string) => void;
  onApplyApiKey: () => void;
}) {
  return (
    // Keep one real root: App assigns this entire rail to one grid area.
    <aside id="session-sidebar" className="session-sidebar" aria-label="会话列表" hidden={hidden}>
      <div className="sidebar-heading">
        <div><p className="eyebrow">工作区</p><h2>会话</h2></div>
        <button className="icon-button" onClick={() => void onStartSession()} aria-label="新建会话">＋</button>
      </div>
      <nav className="session-list">
        {sessions.map((session) => (
          <button
            key={session.thread_id}
            className={`session-item ${session.thread_id === activeSessionId ? "active" : ""}`}
            onClick={() => void onSelectSession(session.thread_id)}
          >
            <strong title={session.title}>{session.title}</strong>
            {sessionRunStatuses[session.thread_id] && (
              <span className={`status status-${sessionRunStatuses[session.thread_id]}`}>
                {statusLabel(sessionRunStatuses[session.thread_id])}
              </span>
            )}
            <span title={session.preview || "尚未开始"}>{session.preview || "尚未开始"}</span>
          </button>
        ))}
      </nav>
      {activeSessionId && (
        <button className="text-button danger" onClick={() => void onRemoveActiveSession()} disabled={isSending}>
          删除当前会话
        </button>
      )}
      <div className="sidebar-utilities" aria-label="工作区工具">
        <WorkspacePanel
          title="科研档案"
          open={openWorkspacePanel === "documents"}
          onToggle={() => void onToggleWorkspacePanel("documents")}
        >
          {workspaceLoading === "documents" ? (
            <p className="panel-hint">正在读取档案…</p>
          ) : documents.length ? (
            <div className="workspace-list">
              {documents.map((document) => (
                <div
                  key={document.document_id}
                  className={`workspace-list-item document-item ${selectedDocumentId === document.document_id ? "selected" : ""}`}
                >
                  <button
                    className="workspace-list-button"
                    type="button"
                    onClick={() => void onSelectResearchDocument(document.document_id)}
                    title={document.title}
                  >
                    <strong>{document.title}</strong>
                    <span>{document.summary || "未填写摘要"}</span>
                  </button>
                  <button
                    className="text-button danger document-delete"
                    type="button"
                    onClick={() => void onRemoveResearchDocument(document)}
                    title="删除档案"
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="panel-hint">暂无档案。可在对话中要求 Agent 保存研究方案。</p>
          )}
        </WorkspacePanel>

        <WorkspacePanel
          title="论文库"
          open={openWorkspacePanel === "papers"}
          onToggle={() => void onToggleWorkspacePanel("papers")}
        >
          {workspaceLoading === "papers" ? (
            <p className="panel-hint">正在读取论文库…</p>
          ) : papers.length ? (
            <div className="workspace-list">
              {papers.map((paper) => (
                <div className="workspace-list-item paper-item" key={paper.paper_id}>
                  <div>
                    <strong title={paper.title}>{paper.title}</strong>
                    <span>{paper.chunks} 个检索片段 · {paper.relation_count ? `${paper.relation_count} 条已确认关系` : "暂无已确认关系"}</span>
                  </div>
                  <div className="paper-actions">
                    <button
                      className="text-button paper-action-button"
                      type="button"
                      onClick={() => onPreparePaperRelationReview(paper)}
                    >
                      关系审查
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
          onToggle={() => void onToggleWorkspacePanel("profile")}
        >
          {workspaceLoading === "profile" ? (
            <p className="panel-hint">正在读取用户画像…</p>
          ) : (
            <>
              <p className="panel-hint">{profile?.content ? "用于跨会话保留稳定研究偏好；可在工作台查看。" : "尚未形成用户画像。"}</p>
              <button className="text-button" type="button" onClick={() => void onOpenProfile()}>
                在工作台打开
              </button>
            </>
          )}
        </WorkspacePanel>


        <WorkspacePanel
          title="每日检索"
          open={openWorkspacePanel === "keywords"}
          onToggle={() => void onToggleWorkspacePanel("keywords")}
        >
          {workspaceLoading === "keywords" ? (
            <p className="panel-hint">正在读取关键词…</p>
          ) : (
            <>
              <form className="keyword-form" onSubmit={(event) => void onAddDailyKeyword(event)}>
                <input
                  value={keywordDraft}
                  onChange={(event) => onKeywordDraftChange(event.target.value)}
                  placeholder="例如：TSN scheduling"
                  aria-label="新关键词"
                />
                <button className="button button-secondary" type="submit" disabled={!keywordDraft.trim()}>添加</button>
              </form>
              {keywords.length ? (
                <div className="keyword-list">
                  {keywords.map((item) => (
                    <div className="keyword-item" key={item.keyword}>
                      <span title={item.keyword}>{item.keyword}</span>
                      <button className="text-button danger" type="button" onClick={() => void onRemoveDailyKeyword(item.keyword)}>删除</button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="panel-hint">尚未设置每日检索关键词。</p>
              )}
              <button className="text-button" type="button" onClick={() => void onOpenDailyCenter()}>
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
                      onClick={() => void onSelectDailyRun(run.run_id)}
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
          onToggle={() => void onToggleWorkspacePanel("settings")}
        >
          {workspaceLoading === "settings" ? (
            <p className="panel-hint">正在读取设置…</p>
          ) : (
            <form className="settings-form" onSubmit={(event) => void onSaveWorkspaceSettings(event)}>
              <label>模型
                <input value={settingsDraft.model} onChange={(event) => onSettingsDraftChange((current) => ({ ...current, model: event.target.value }))} />
              </label>
              <label>PDF 最大页数
                <input type="number" min="5" max="100" value={settingsDraft.pdf_max_pages} onChange={(event) => onSettingsDraftChange((current) => ({ ...current, pdf_max_pages: Number(event.target.value) || 5 }))} />
              </label>
              <label className="checkbox-label">
                <input type="checkbox" checked={settingsDraft.rag_enabled} onChange={(event) => onSettingsDraftChange((current) => ({ ...current, rag_enabled: event.target.checked }))} />
                启用 RAG 论文库
              </label>
              <label className="checkbox-label">
                <input type="checkbox" checked={settingsDraft.daily_search_enabled} onChange={(event) => onSettingsDraftChange((current) => ({ ...current, daily_search_enabled: event.target.checked }))} />
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
                onChange={(event) => onApiKeyDraftChange(event.target.value)}
                placeholder="API_AUTH_TOKEN"
              />
              <button className="button button-secondary" type="button" onClick={onApplyApiKey}>应用</button>
            </div>
            <p>仅保存在当前浏览器标签页中。</p>
          </div>
        </WorkspacePanel>
      </div>
      {children}
    </aside>
  );
}

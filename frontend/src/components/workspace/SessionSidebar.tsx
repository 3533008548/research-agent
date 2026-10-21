import type { Dispatch, FormEvent, SetStateAction } from "react";

import { statusLabel } from "../../lib/labels";
import { WorkspacePanel } from "../WorkspacePanel";
import type {
  DailyKeyword,
  ExperimentProject,
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
  sessions,
  activeSessionId,
  isSending,
  openWorkspacePanel,
  workspaceLoading,
  documents,
  selectedDocumentId,
  profile,
  papers,
  experiments,
  selectedExperimentId,
  experimentStarting,
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
  onStartPaperReproduction,
  onSelectExperimentProject,
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
  sessions: Session[];
  activeSessionId: string | null;
  isSending: boolean;
  openWorkspacePanel: string | null;
  workspaceLoading: string | null;
  documents: ResearchDocument[];
  selectedDocumentId: string | null;
  profile: UserProfile | null;
  papers: Paper[];
  experiments: ExperimentProject[];
  selectedExperimentId: string | null;
  experimentStarting: string | null;
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
  onStartPaperReproduction: (paper: Paper) => void;
  onSelectExperimentProject: (projectId: string) => void;
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
    <>
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
            <strong>{session.title}</strong>
            <span>{session.preview || "尚未开始"}</span>
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
                    <strong>{paper.title}</strong>
                    <span>{paper.chunks} 个检索片段 · {paper.relation_count ? `${paper.relation_count} 条已确认关系` : "暂无已确认关系"}</span>
                  </div>
                  <div className="paper-actions">
                    <button
                      className="text-button paper-reproduce-button"
                      type="button"
                      onClick={() => onPreparePaperRelationReview(paper)}
                    >
                      关系审查
                    </button>
                    <button
                      className="text-button paper-reproduce-button"
                      type="button"
                      onClick={() => void onStartPaperReproduction(paper)}
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
          title="复现实验"
          open={openWorkspacePanel === "experiments"}
          onToggle={() => void onToggleWorkspacePanel("experiments")}
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
                  onClick={() => void onSelectExperimentProject(project.project_id)}
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
                      <span>{item.keyword}</span>
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
    </>
  );
}

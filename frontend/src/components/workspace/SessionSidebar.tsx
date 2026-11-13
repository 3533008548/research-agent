import type { Dispatch, FormEvent, ReactNode, SetStateAction } from "react";

import { statusLabel } from "../../lib/labels";
import type {
  DailyKeyword,
  Paper,
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
  onSelectSession,
  onStartSession,
  onRemoveActiveSession,
  onOpenLibrary,
  onOpenSettings,
}: {
  onOpenLibrary: () => void;
  onOpenSettings: () => void;
  hidden?: boolean;
  children?: ReactNode;
  sessions: Session[];
  activeSessionId: string | null;
  sessionRunStatuses: Record<string, string>;
  isSending: boolean;
  openWorkspacePanel: string | null;
  workspaceLoading: string | null;
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
  onOpenProfile: () => void;
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
        <button className="button button-secondary" onClick={() => void onStartSession()} aria-label="新建会话">＋ 新建对话</button>
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
      <nav className="sidebar-main-nav" aria-label="工作区导航">
        <button className="sidebar-nav-button" aria-label="论文库" onClick={onOpenLibrary}>▤ 论文库</button>
      </nav>
      <button className="sidebar-nav-button sidebar-settings" aria-label="设置" onClick={onOpenSettings}>⚙ 设置</button>
      {children}
    </aside>
  );
}

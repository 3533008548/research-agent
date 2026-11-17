import type { ReactNode } from "react";

import { statusLabel } from "../../lib/labels";
import type { Session } from "../../types";

/** The workbench's conversation list; application pages live in AppNavigation. */
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
}: {
  hidden?: boolean;
  children?: ReactNode;
  sessions: Session[];
  activeSessionId: string | null;
  sessionRunStatuses: Record<string, string>;
  isSending: boolean;
  onSelectSession: (sessionId: string) => void;
  onStartSession: () => void;
  onRemoveActiveSession: () => void;
}) {
  return (
    // Keep one real root: App assigns this entire rail to one grid area.
    <aside id="session-sidebar" className="session-sidebar" aria-label="会话列表" hidden={hidden}>
      <div className="sidebar-heading">
        <h2>对话</h2>
        <button type="button" className="icon-button" onClick={() => void onStartSession()} aria-label="新建会话" title="新建对话">
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M9 3H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5M11 13l-4 1 1-4 8-8 3 3Z" /></svg>
        </button>
      </div>
      <nav className="session-list" aria-label="对话列表">
        {sessions.map((session) => (
          <button
            key={session.thread_id}
            className={`session-item ${session.thread_id === activeSessionId ? "active" : ""}`}
            aria-current={session.thread_id === activeSessionId ? "true" : undefined}
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
      {children}
    </aside>
  );
}

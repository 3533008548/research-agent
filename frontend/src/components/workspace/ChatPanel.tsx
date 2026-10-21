import type { Dispatch, FormEvent, SetStateAction } from "react";

import { ChatMessage } from "../ChatMessage";
import { modeLabel, type DisplayMessage, type ResearchMode } from "../../lib/labels";
import type { ResearchScope, Run, WorkspaceUpload } from "../../types";

/**
 * The chat tab: the transcript, the composer and the mode switch.
 *
 * Extracted from `App.tsx` without behaviour change.  While a run is in
 * flight the composer becomes a steering box instead of a new-message box —
 * the same button sends a supplement rather than starting a second run, and
 * an attachment cannot be added at that point.
 */
export function ChatPanel({
  messages,
  draft,
  mode,
  researchScope,
  currentRun,
  isLoading,
  isSending,
  isCancelling,
  isUploading,
  canSteerCurrentRun,
  activeSessionId,
  attachment,
  messageListRef,
  uploadInputRef,
  setDraft,
  setMode,
  setResearchScope,
  setAttachment,
  sendMessage,
  stopCurrentRun,
  selectAttachment,
}: {
  messages: DisplayMessage[];
  draft: string;
  mode: ResearchMode;
  researchScope: ResearchScope;
  currentRun: Run | null;
  isLoading: boolean;
  isSending: boolean;
  isCancelling: boolean;
  isUploading: boolean;
  canSteerCurrentRun: boolean;
  activeSessionId: string | null;
  attachment: WorkspaceUpload | null;
  messageListRef: React.RefObject<HTMLDivElement | null>;
  uploadInputRef: React.RefObject<HTMLInputElement | null>;
  setDraft: Dispatch<SetStateAction<string>>;
  setMode: Dispatch<SetStateAction<ResearchMode>>;
  setResearchScope: Dispatch<SetStateAction<ResearchScope>>;
  setAttachment: Dispatch<SetStateAction<WorkspaceUpload | null>>;
  sendMessage: (
    event: FormEvent<HTMLFormElement> | undefined,
    resumeResearch?: boolean,
    retryContent?: string,
    requestedMode?: ResearchMode,
  ) => Promise<void>;
  stopCurrentRun: () => void;
  selectAttachment: (event: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
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
  );
}

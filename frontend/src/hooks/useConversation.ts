import { useCallback, useRef, useState, type ChangeEvent, type FormEvent } from "react";

import {
  cancelRun,
  createRun,
  createRunSteer,
  createSession,
  deleteSession,
  getRun,
  getSessionMessages,
  getSessionUsage,
  listSessions,
  streamRun,
  uploadWorkspaceFile,
} from "../api";
import { userFacingError } from "../lib/errors";
import { appendRunEvent, displayMessages, type DisplayMessage, type ResearchMode } from "../lib/labels";
import type { Notice } from "../lib/workspace";
import type {
  ResearchScope,
  Run,
  RunEvent,
  Session,
  SessionUsage,
  WorkspaceUpload,
} from "../types";

const ACTIVE_SESSION_KEY = "research-agent.active-session";

/**
 * The conversation: sessions, the transcript, the composer and the run that
 * is currently streaming.
 *
 * Extracted from `App.tsx` without behaviour change.  Everything here shares
 * one lifecycle — a message starts a run, the run streams tokens into the
 * transcript, and the transcript belongs to a session — so keeping it in one
 * hook avoids threading a dozen pieces of state between separate ones.
 *
 * The two workbench-tab setters come in from outside because switching to
 * the chat tab is how the user comes back to the conversation.
 */
export function useConversation({
  apiKey,
  setNotice,
  setActiveWorkbenchTabId,
}: {
  apiKey: string;
  setNotice: (notice: Notice) => void;
  setActiveWorkbenchTabId: (tabId: string) => void;
}) {
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
  const [isSending, setIsSending] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [attachment, setAttachment] = useState<WorkspaceUpload | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const requestGenerationRef = useRef(0);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);

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

  // A run can be steered only while it is still queued or running and it
  // belongs to this session: a finished run has nothing left to steer.
  const canSteerCurrentRun = Boolean(
    isSending
    && currentRun
    && currentRun.session_id === activeSessionId
    && ["chat", "research"].includes(currentRun.kind)
    && ["queued", "running"].includes(currentRun.status),
  );

  return {
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
    requestGenerationRef,
    loadSession,
    selectSession,
    startSession,
    removeActiveSession,
    appendToken,
    completeAnswer,
    sendMessage,
    abandonActiveRunForNavigation,
    stopCurrentRun,
    retryCurrentChat,
    uploadAttachment,
    selectAttachment,
  };
}

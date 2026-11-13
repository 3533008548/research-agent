import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";

import {
  cancelRun,
  createRun,
  createRunSteer,
  createSession,
  deleteSession,
  getRun,
  getSessionMessages,
  getSessionUsage,
  listSessionRuns,
  listSessions,
  streamRun,
  uploadWorkspaceFile,
} from "../api";
import { userFacingError } from "../lib/errors";
import { appendRunEvent, displayMessages, type ResearchMode } from "../lib/labels";
import { stopTiming, timingFromRun } from "../lib/runTiming";
import { useSessionConversationState } from "./useSessionConversationState";
import type { Notice } from "../lib/workspace";
import type {
  Run,
  ResearchProject,
  Session,
} from "../types";

const ACTIVE_SESSION_KEY = "research-agent.active-session";
const isPendingRun = (run: Run | null | undefined) => Boolean(
  run && !run.research && ["queued", "running", "cancelling"].includes(run.status),
);

function restoredMessages(history: Parameters<typeof displayMessages>[0], run?: Run) {
  const messages = displayMessages(history);
  const last = messages.at(-1);
  // History has no run IDs. Only annotate a reply that actually matches this run.
  if (run && last?.role === "assistant" && run.answer && last.content === run.answer) {
    last.timing = timingFromRun(run);
  }
  return messages;
}

/**
 * Session navigation only changes the visible conversation. Each run keeps
 * its own stream and session-bound setters until it finishes or is stopped.
 */
export function useConversation({
  apiKey,
  setNotice,
  setActiveWorkbenchTabId,
  setIsLoading,
}: {
  apiKey: string;
  setNotice: (notice: Notice) => void;
  setActiveWorkbenchTabId: (tabId: string) => void;
  setIsLoading: (value: boolean) => void;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    () => localStorage.getItem(ACTIVE_SESSION_KEY),
  );
  const { state, getSession, bindSession, sessionRunStatuses } = useSessionConversationState(activeSessionId);
  const { messages, sessionUsage, draft, mode, researchScope, currentRun, runEvents,
    isCancelling, attachment, isUploading } = state;
  const isSending = state.isSending || state.loading || isPendingRun(currentRun);
  const { setMessages, setSessionUsage, setDraft, setMode, setResearchScope, setCurrentRun,
    setRunEvents, setIsSending, setIsCancelling, setAttachment, setIsUploading } = bindSession(activeSessionId);
  const activeSessionRef = useRef(activeSessionId);
  const streams = useRef(new Map<string, AbortController>());
  const navigation = useRef(0);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);

  const syncResearchProject = useCallback((project: ResearchProject) => {
    const status = ({ active: "running", paused: "paused", stopped: "cancelled", waiting: "waiting", needs_input: "needs_input" } as Record<string, string>)[project.execution_state]
      || (project.end_reason === "fulfilled" ? "completed" : "partial_failed");
    const target = getSession(project.origin_session_id).currentRun;
    if (target?.run_id !== project.project_id || target.updated_at >= project.updated_at) return;
    bindSession(project.origin_session_id).setCurrentRun(current => current?.run_id === project.project_id
      ? { ...current, status, answer: project.report, research: { ...current.research, ...project }, updated_at: project.updated_at } : current);
  }, [getSession, bindSession]);

  useEffect(() => () => {
    // Unmounting disconnects observers; only the stop button cancels work.
    for (const controller of streams.current.values()) controller.abort();
    streams.current.clear();
  }, []);

  const observeRun = useCallback(async (sessionId: string, run: Run, streamUrl: string, restoring = false) => {
    if (streams.current.has(run.run_id)) return;
    const controller = new AbortController();
    streams.current.set(run.run_id, controller);
    const { setMessages, setCurrentRun, setRunEvents, setIsSending, setSessionUsage } = bindSession(sessionId);
    const complete = (answer?: string, detail?: Run, finished = true) => setMessages((current) => {
      const last = current.at(-1);
      return last?.role === "assistant" && (last.streaming || last.timing)
        ? [...current.slice(0, -1), { ...last, content: answer ?? last.content, streaming: false,
          timing: (detail && timingFromRun(detail)) || (finished ? stopTiming(last.timing) : last.timing) }]
        : current;
    });
    setCurrentRun(run);
    setIsSending(true);
    try {
      await streamRun(apiKey, streamUrl, (event) => {
        setRunEvents((current) => appendRunEvent(current, event));
        if (event.type === "token") {
          setMessages((current) => {
            const last = current.at(-1);
            return last?.role === "assistant" && last.streaming
              ? [...current.slice(0, -1), { ...last, content: last.content + (event.text || "") }]
              : current;
          });
        }
        if (event.type === "done" || (event.type === "status" && (!event.stage || event.stage === "run"))) {
          const completed = event.type === "done" && event.status === "completed";
          setCurrentRun((current) => current && {
            ...current, status: event.status || current.status,
            error_type: completed ? "" : event.error_type || current.error_type,
            error_details: completed ? undefined : event.error_details || current.error_details,
          });
        }
        if (event.type === "done") complete(event.answer);
        if (event.type === "error") {
          complete(`运行未能完成：${event.error_type || "RunError"}。可点击「查看运行详情」了解当前状态。`);
          setCurrentRun((current) => current && {
            ...current, status: "failed", error_type: event.error_type || "RunError",
            error_details: event.error_details || current.error_details,
          });
        }
      }, controller.signal);
      if (controller.signal.aborted) return;
      const detail = await getRun(apiKey, run.run_id);
      setCurrentRun(detail);
      setRunEvents(detail.events);
      complete(detail.answer || undefined, detail);
      if (isPendingRun(detail)) bindSession(sessionId).setLoaded(false);
      // History and run status are separate snapshots. On reconnect, reload
      // the final transcript so a completion during loading is not duplicated.
      if (restoring && !isPendingRun(detail)) {
        setMessages(restoredMessages(await getSessionMessages(apiKey, sessionId), detail));
      }
      const [refreshedSessions, usage] = await Promise.all([
        listSessions(apiKey), getSessionUsage(apiKey, sessionId),
      ]);
      // A background refresh must not drop a session created (or resurrect
      // one deleted) while the list request was in flight.
      const refreshedById = new Map(refreshedSessions.map((session) => [session.thread_id, session]));
      setSessions((current) => current.map((session) => refreshedById.get(session.thread_id) || session));
      setSessionUsage(usage);
    } catch (error) {
      if (controller.signal.aborted) return;
      // A broken event connection does not prove the server task failed.
      // Reloading this session reconnects to its persisted run.
      bindSession(sessionId).setLoaded(false);
      complete(undefined, undefined, false);
      if (activeSessionRef.current === sessionId) {
        setNotice({ kind: "error", text: userFacingError(error) });
      }
    } finally {
      streams.current.delete(run.run_id);
      setIsSending(false);
    }
  }, [apiKey, bindSession, setNotice]);

  const loadSession = useCallback(async (sessionId: string) => {
    activeSessionRef.current = sessionId;
    setActiveSessionId(sessionId);
    localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    const cached = getSession(sessionId);
    if (cached.loaded || cached.loading || cached.isSending) return;
    const target = bindSession(sessionId);
    target.setLoading(true);
    try {
      const [history, usage] = await Promise.all([
        getSessionMessages(apiKey, sessionId), getSessionUsage(apiKey, sessionId),
      ]);
      const runs = await listSessionRuns(apiKey, sessionId);
      const latest = runs.find((item) => ["chat", "research"].includes(item.kind));
      const run = latest ? await getRun(apiKey, latest.run_id) : undefined;
      const pending = run && !run.research && isPendingRun(run);
      const restored = restoredMessages(history, run);
      if (pending) {
        restored.push({ id: `assistant-${run.run_id}`, role: "assistant", content: "", streaming: true, timing: timingFromRun(run) });
      } else if (run?.answer && restored.at(-1)?.role === "user") {
        restored.push({ id: `assistant-${run.run_id}`, role: "assistant", content: run.answer, timing: timingFromRun(run) });
      }
      target.setMessages(restored);
      target.setSessionUsage(usage);
      target.setCurrentRun(run || null);
      // Active streams replay from their start; use persisted history for finished runs.
      target.setRunEvents(pending ? [] : run?.events || []);
      target.setLoaded(true);
      if (pending) void observeRun(sessionId, run, `/api/v1/runs/${encodeURIComponent(run.run_id)}/events`, true);
    } finally {
      target.setLoading(false);
    }
  }, [apiKey, bindSession, getSession, observeRun]);

  const researchRunId = currentRun?.research ? currentRun.run_id : "";
  useEffect(() => {
    if (!researchRunId) return;
    let disposed = false;
    let timer: number;
    const target = bindSession(activeSessionId);
    const refresh = async () => {
      try {
        const detail = await getRun(apiKey, researchRunId);
        if (!disposed) {
          target.setCurrentRun(current => current?.run_id === researchRunId && current.updated_at <= detail.updated_at ? detail : current);
          target.setRunEvents(detail.events);
        }
      } catch { /* The workbench reports connection errors; keep the last known activity. */ }
      if (!disposed) timer = window.setTimeout(() => void refresh(), 2000);
    };
    void refresh();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [apiKey, activeSessionId, researchRunId, bindSession]);

  async function uploadAttachment(file: File) {
    if (isUploading || isSending) {
      return;
    }
    setIsUploading(true);
    try {
      const upload = await uploadWorkspaceFile(apiKey, file);
      setAttachment(upload);
      if (activeSessionRef.current !== activeSessionId) return;
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
    if (sessionId === activeSessionRef.current && getSession(sessionId).loaded) {
      setActiveWorkbenchTabId("chat");
      return;
    }
    try {
      const selection = ++navigation.current;
      await loadSession(sessionId);
      if (selection != navigation.current) return;
      setActiveWorkbenchTabId("chat");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startSession() {
    try {
      const selection = ++navigation.current;
      const session = await createSession(apiKey);
      setSessions((current) => [session, ...current]);
      if (selection != navigation.current) return;
      await loadSession(session.thread_id);
      if (selection != navigation.current) return;
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
        activeSessionRef.current = null;
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

    if (isSending || getSession(activeSessionId).isSending || getSession(activeSessionId).loading) {
      return;
    }

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
    setCurrentRun(null);
    setNotice(null);
    setRunEvents([]);
    const submittedAt = Date.now();
    setMessages((current) => [
      ...current,
      { id: `user-${Date.now()}`, role: "user", content: shownContent },
      { id: `assistant-${submittedAt}`, role: "assistant", content: "", streaming: true,
        timing: { elapsedMs: 0, sampledAt: submittedAt, running: true } },
    ]);

    try {
      const started = await createRun(
        apiKey, requestMode, activeSessionId, content, pendingAttachment?.upload_id,
        researchScope, resumeResearch,
      );
      if (!resumeResearch) {
        setAttachment(null);
      }
      const run: Run = {
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
      };
      if (started.kind === "research" && started.project_id) {
        const detail = await getRun(apiKey, started.run_id);
        setCurrentRun(detail);
        setMessages(current => {
          const last = current.at(-1);
          return last?.streaming ? [...current.slice(0, -1), { ...last, streaming: false,
            timing: undefined,
            content: "多 Agent 研究已启动。可在对话区查看判断、证据和阶段报告，或暂停、调整目标。选择输入框中的“对话”可继续聊天。" }] : current;
        });
      } else {
        await observeRun(activeSessionId, run, started.stream_url);
      }
    } catch (error) {
      const message = userFacingError(error);
      setMessages((current) => {
        const last = current.at(-1);
        return last?.role === "assistant" && last.streaming
          ? [...current.slice(0, -1), { ...last, content: `⚠️ ${message}`, streaming: false, timing: stopTiming(last.timing) }]
          : current;
      });
      if (activeSessionRef.current === activeSessionId) setNotice({ kind: "error", text: message });
    } finally {
      setIsSending(false);
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
        activeSessionRef.current = null;
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

  return {
    syncResearchProject,
    sessionRunStatuses,
    canSteerCurrentRun,
    loadWorkspace,
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
    loadSession,
    selectSession,
    startSession,
    removeActiveSession,
    sendMessage,
    stopCurrentRun,
    retryCurrentChat,
    uploadAttachment,
    selectAttachment,
  };
}

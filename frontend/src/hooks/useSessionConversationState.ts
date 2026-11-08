import { useCallback, useRef, useState, type SetStateAction } from "react";

import type { DisplayMessage, ResearchMode } from "../lib/labels";
import type { ResearchScope, Run, RunEvent, SessionUsage, WorkspaceUpload } from "../types";

function emptyConversation() {
  return {
    messages: [] as DisplayMessage[],
    sessionUsage: null as SessionUsage | null,
    draft: "",
    mode: "chat" as ResearchMode,
    researchScope: "both" as ResearchScope,
    currentRun: null as Run | null,
    runEvents: [] as RunEvent[],
    isSending: false,
    isCancelling: false,
    attachment: null as WorkspaceUpload | null,
    isUploading: false,
    loading: false,
    loaded: false,
  };
}

type ConversationState = ReturnType<typeof emptyConversation>;

/** Every async callback keeps setters bound to the session that started it. */
export function useSessionConversationState(activeSessionId: string | null) {
  const states = useRef(new Map<string, ConversationState>());
  const [, render] = useState(0);
  const empty = useRef(emptyConversation());

  const getSession = useCallback((sessionId: string | null) => {
    if (!sessionId) return empty.current;
    let state = states.current.get(sessionId);
    if (!state) {
      state = emptyConversation();
      states.current.set(sessionId, state);
    }
    return state;
  }, []);

  const bindSession = useCallback((sessionId: string | null) => {
    function field<K extends keyof ConversationState>(key: K) {
      return (value: SetStateAction<ConversationState[K]>) => {
        if (!sessionId) return;
        const state = getSession(sessionId);
        const next = typeof value === "function"
          ? (value as (previous: ConversationState[K]) => ConversationState[K])(state[key])
          : value;
        states.current.set(sessionId, { ...state, [key]: next });
        render((version) => version + 1);
      };
    }
    return {
      setMessages: field("messages"),
      setSessionUsage: field("sessionUsage"),
      setDraft: field("draft"),
      setMode: field("mode"),
      setResearchScope: field("researchScope"),
      setCurrentRun: field("currentRun"),
      setRunEvents: field("runEvents"),
      setIsSending: field("isSending"),
      setIsCancelling: field("isCancelling"),
      setAttachment: field("attachment"),
      setIsUploading: field("isUploading"),
      setLoading: field("loading"),
      setLoaded: field("loaded"),
    };
  }, [getSession]);

  const sessionRunStatuses = Object.fromEntries([...states.current].flatMap(([id, state]) => {
    const status = state.currentRun?.status || (state.isSending ? "queued" : "");
    return status ? [[id, status]] : [];
  }));
  return { state: getSession(activeSessionId), getSession, bindSession, sessionRunStatuses };
}

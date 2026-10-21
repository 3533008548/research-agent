import { useState, type Dispatch, type FormEvent, type RefObject, type SetStateAction } from "react";

import {
  createDailyKeyword,
  createDailyRun,
  deleteDailyKeyword,
  getDailyDigest,
  getDailyRunDetail,
  getRun,
  listDailyKeywords,
  listDailyRuns,
  streamRun,
  updateDailyPaperStatus,
} from "../api";
import { userFacingError } from "../lib/errors";
import { appendRunEvent, dailyRunTitle } from "../lib/labels";
import type { Notice, WorkbenchTab } from "../lib/workspace";
import type {
  DailyDigest,
  DailyKeyword,
  DailyPaper,
  DailyRunKind,
  Run,
  RunEvent,
} from "../types";

/**
 * The daily-discovery workspace: today's digest, the keyword list and the
 * actions that start or repair a daily run.
 *
 * Extracted from `App.tsx` without behaviour change.  The hook owns the daily
 * state so the component does not have to carry seven pieces of state and
 * seven handlers; the workbench tab list is passed in because a daily run
 * opens as a tab.
 */
export function useDailyWorkspace({
  apiKey,
  setNotice,
  setWorkbenchTabs,
  setActiveWorkbenchTabId,
  setCurrentRun,
  isSending,
  setIsSending,
  setRunEvents,
  abortRef,
}: {
  apiKey: string;
  setNotice: (notice: Notice) => void;
  setWorkbenchTabs: Dispatch<SetStateAction<WorkbenchTab[]>>;
  setActiveWorkbenchTabId: (tabId: string) => void;
  setCurrentRun: Dispatch<SetStateAction<Run | null>>;
  isSending: boolean;
  setIsSending: (value: boolean) => void;
  setRunEvents: Dispatch<SetStateAction<RunEvent[]>>;
  abortRef: RefObject<AbortController | null>;
}) {
  const [dailyDigest, setDailyDigest] = useState<DailyDigest>({ progress: "", papers: [] });
  const [dailyRuns, setDailyRuns] = useState<Run[]>([]);
  const [dailyLoading, setDailyLoading] = useState(false);
  const [dailyAction, setDailyAction] = useState<DailyRunKind | null>(null);
  const [dailySearchDraft, setDailySearchDraft] = useState("");
  const [keywords, setKeywords] = useState<DailyKeyword[]>([]);
  const [keywordDraft, setKeywordDraft] = useState("");

  async function selectDailyRun(runId: string) {
    try {
      const detail = await getDailyRunDetail(apiKey, runId);
      const tabId = `daily-run:${detail.run_id}`;
      const title = dailyRunTitle(detail);
      setWorkbenchTabs((current) => {
        const existing = current.find((tab) => tab.id === tabId);
        if (existing?.kind === "dailyRun") {
          return current.map((tab) => tab.id === tabId ? { ...tab, title, detail } : tab);
        }
        return [...current, { id: tabId, kind: "dailyRun", title, detail }];
      });
      setActiveWorkbenchTabId(tabId);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function addDailyKeyword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const keyword = keywordDraft.trim();
    if (!keyword) {
      return;
    }
    try {
      await createDailyKeyword(apiKey, keyword);
      setKeywords(await listDailyKeywords(apiKey));
      setKeywordDraft("");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function removeDailyKeyword(keyword: string) {
    if (!window.confirm(`删除每日检索关键词“${keyword}”？`)) {
      return;
    }
    try {
      await deleteDailyKeyword(apiKey, keyword);
      setKeywords((current) => current.filter((item) => item.keyword !== keyword));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function loadDailyCenter() {
    setDailyLoading(true);
    try {
      const [digest, runs] = await Promise.all([
        getDailyDigest(apiKey),
        listDailyRuns(apiKey),
      ]);
      setDailyDigest(digest);
      setDailyRuns(runs);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setDailyLoading(false);
    }
  }

  async function openDailyCenter() {
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "daily")
      ? current
      : [...current, { id: "daily", kind: "daily", title: "每日检索" }]);
    setActiveWorkbenchTabId("daily");
    await loadDailyCenter();
  }

  async function updateDailyPaper(paper: DailyPaper, status: Exclude<DailyPaper["status"], "new">) {
    try {
      await updateDailyPaperStatus(apiKey, paper.keyword, paper.title, status);
      setDailyDigest((current) => ({
        ...current,
        papers: current.papers.map((item) => item.keyword === paper.keyword && item.title === paper.title
          ? { ...item, status }
          : item),
      }));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startDailyAction(kind: DailyRunKind) {
    if (isSending) {
      return;
    }
    const keyword = dailySearchDraft.trim();
    if (kind === "search" && !keyword) {
      setNotice({ kind: "info", text: "请输入要临时检索的关键词。" });
      return;
    }

    setDailyAction(kind);
    setIsSending(true);
    setNotice(null);
    setRunEvents([]);
    try {
      const started = await createDailyRun(apiKey, kind, keyword || undefined);
      setCurrentRun({
        run_id: started.run_id,
        kind: started.kind,
        session_id: null,
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
        setRunEvents((current) => appendRunEvent(current, runEvent));
        if (runEvent.type === "status" || runEvent.type === "done") {
          setCurrentRun((current) => current && { ...current, status: runEvent.status || current.status });
        }
        if (runEvent.type === "error") {
          setCurrentRun((current) => current && {
            ...current,
            status: "failed",
            error_type: runEvent.error_type || "RunError",
          });
        }
      }, controller.signal);
      setCurrentRun(await getRun(apiKey, started.run_id));
      if (kind === "search") {
        setDailySearchDraft("");
      }
      await loadDailyCenter();
    } catch (error) {
      const message = userFacingError(error);
      setNotice({ kind: "error", text: message });
      setCurrentRun((current) => current && { ...current, status: "failed" });
    } finally {
      abortRef.current = null;
      setIsSending(false);
      setDailyAction(null);
    }
  }

  /** Reload the keyword list and the daily runs that go with it. */
  async function loadKeywords() {
    const [nextKeywords, nextDailyRuns] = await Promise.all([
      listDailyKeywords(apiKey),
      listDailyRuns(apiKey),
    ]);
    setKeywords(nextKeywords);
    setDailyRuns(nextDailyRuns);
  }

  /** Accept a fresh daily-run list from elsewhere (the run centre loads both). */
  function replaceDailyRuns(runs: Run[]) {
    setDailyRuns(runs);
  }

  return {
    dailyDigest,
    loadKeywords,
    replaceDailyRuns,
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
    loadDailyCenter,
    openDailyCenter,
    updateDailyPaper,
    startDailyAction,
  };
}

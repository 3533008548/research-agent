import { useState, type Dispatch, type SetStateAction } from "react";

import {
  createBadcase,
  getSessionUsage,
  listBadcases,
  listDailyRuns,
  listSessionRuns,
} from "../api";
import { userFacingError } from "../lib/errors";
import { badcaseCategoryLabel } from "../lib/labels";
import type { Notice, WorkbenchTab } from "../lib/workspace";
import type { BadcaseCandidate, BadcaseCategory, Run, SessionUsage } from "../types";

/**
 * The run centre: recent runs, the selected run, and the badcase triage.
 *
 * Extracted from `App.tsx` without behaviour change.  A badcase record only
 * ever holds a category, a run id and an optional redacted note — never the
 * conversation, the papers or the model output — and the run list mixes the
 * session's own runs with the daily ones so they read as one timeline.
 */
export function useRunCenter({
  apiKey,
  activeSessionId,
  setNotice,
  setSessionUsage,
  replaceDailyRuns,
  setWorkbenchTabs,
  setActiveWorkbenchTabId,
}: {
  apiKey: string;
  activeSessionId: string | null;
  setNotice: (notice: Notice) => void;
  setSessionUsage: (usage: SessionUsage | null) => void;
  replaceDailyRuns: (runs: Run[]) => void;
  setWorkbenchTabs: Dispatch<SetStateAction<WorkbenchTab[]>>;
  setActiveWorkbenchTabId: (tabId: string) => void;
}) {
  const [runCenterRuns, setRunCenterRuns] = useState<Run[]>([]);
  const [badcases, setBadcases] = useState<BadcaseCandidate[]>([]);
  const [runCenterLoading, setRunCenterLoading] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [badcaseSubmitting, setBadcaseSubmitting] = useState<string | null>(null);
  const [badcaseCategories, setBadcaseCategories] = useState<Record<string, BadcaseCategory>>({});
  const [manualBadcaseRunId, setManualBadcaseRunId] = useState("");
  const [manualBadcaseCategory, setManualBadcaseCategory] = useState<BadcaseCategory>("answer_quality");
  const [manualBadcaseNote, setManualBadcaseNote] = useState("");

  async function loadRunCenter() {
    setRunCenterLoading(true);
    try {
      const [sessionRuns, nextDailyRuns, nextBadcases, usage] = await Promise.all([
        activeSessionId ? listSessionRuns(apiKey, activeSessionId) : Promise.resolve([] as Run[]),
        listDailyRuns(apiKey),
        listBadcases(apiKey),
        activeSessionId ? getSessionUsage(apiKey, activeSessionId) : Promise.resolve(null),
      ]);
      setRunCenterRuns([...sessionRuns, ...nextDailyRuns].sort((left, right) => (
        `${right.updated_at}${right.created_at}`.localeCompare(`${left.updated_at}${left.created_at}`)
      )));
      replaceDailyRuns(nextDailyRuns);
      setBadcases(nextBadcases);
      setSessionUsage(usage);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setRunCenterLoading(false);
    }
  }

  function selectRunForTimeline(run: Run) {
    // Clicking the selected row again closes the panel, so the list doubles as
    // the toggle without needing a separate control.
    setSelectedRunId((current) => (current === run.run_id ? null : run.run_id));
  }

  async function openRunCenter() {
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "runs")
      ? current
      : [...current, { id: "runs", kind: "runs", title: "运行中心" }]);
    setActiveWorkbenchTabId("runs");
    await loadRunCenter();
  }

  async function markRunAsBadcase(run: Run) {
    const category = badcaseCategories[run.run_id] || "answer_quality";
    setBadcaseSubmitting(run.run_id);
    try {
      const candidate = await createBadcase(apiKey, run.run_id, category);
      setBadcases((current) => [candidate, ...current.filter((item) => item.candidate_id !== candidate.candidate_id)]);
      setNotice({ kind: "info", text: `已将该运行标记为 Badcase：${badcaseCategoryLabel(category)}。` });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setBadcaseSubmitting(null);
    }
  }

  async function markManualBadcase() {
    const runId = manualBadcaseRunId.trim();
    if (!runId) {
      setNotice({ kind: "info", text: "请填写要标记的运行 ID。" });
      return;
    }
    const requestId = `manual:${runId}`;
    setBadcaseSubmitting(requestId);
    try {
      const candidate = await createBadcase(apiKey, runId, manualBadcaseCategory, manualBadcaseNote);
      setBadcases((current) => [candidate, ...current.filter((item) => item.candidate_id !== candidate.candidate_id)]);
      setManualBadcaseNote("");
      setNotice({ kind: "info", text: `已将 ${runId} 标记为 Badcase。` });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setBadcaseSubmitting(null);
    }
  }

  return {
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
  };
}

import type { BadcaseCategory, Run } from "../../types";

/**
 * Recent-runs list.
 *
 * Extracted verbatim from App.tsx: the row markup, the status vocabulary and
 * the Badcase gating rules are unchanged, so this move cannot alter behaviour.
 * The one addition is a selection callback driving the stage timeline.
 */
export function RunCenterList({
  runs,
  loading,
  selectedRunId,
  badcaseCategories,
  badcaseSubmitting,
  onSelect,
  onBadcaseCategoryChange,
  onMarkBadcase,
}: {
  runs: Run[];
  loading: boolean;
  selectedRunId: string | null;
  badcaseCategories: Record<string, BadcaseCategory>;
  badcaseSubmitting: string | null;
  onSelect: (run: Run) => void;
  onBadcaseCategoryChange: (runId: string, category: BadcaseCategory) => void;
  onMarkBadcase: (run: Run) => void;
}) {
  if (loading) {
    return <p className="empty-panel">正在读取运行记录…</p>;
  }
  if (!runs.length) {
    return <p className="empty-panel">当前范围内尚无运行记录。</p>;
  }
  return (
    <div className="run-center-list">
      {runs.map((run) => (
        <article
          className={`run-center-row${run.run_id === selectedRunId ? " run-center-row-active" : ""}`}
          key={run.run_id}
        >
          <button
            className="run-center-select"
            type="button"
            onClick={() => onSelect(run)}
            aria-label={`查看 ${run.run_id} 的阶段时间轴`}
          >
            <div className="run-center-main">
              <div className="run-center-meta">
                <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>
                <span>{runKindLabel(run.kind)}</span>
                <span>{durationLabel(run.duration_ms) || "未完成"}</span>
              </div>
              <strong title={run.run_id}>{run.run_id}</strong>
              {run.error_type && <p className="run-center-error">{run.error_type}</p>}
            </div>
          </button>
          <div className="badcase-editor">
            <select
              aria-label={`${run.run_id} 的 Badcase 分类`}
              value={badcaseCategories[run.run_id] || "answer_quality"}
              onChange={(event) => onBadcaseCategoryChange(run.run_id, event.target.value as BadcaseCategory)}
            >
              {(["retrieval_miss", "citation_quality", "answer_quality", "tool_failure", "performance", "safety", "other"] as BadcaseCategory[]).map((category) => (
                <option key={category} value={category}>{badcaseCategoryLabel(category)}</option>
              ))}
            </select>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => onMarkBadcase(run)}
              disabled={badcaseSubmitting === run.run_id || !["completed", "failed", "cancelled", "partial_failed"].includes(run.status)}
            >
              {badcaseSubmitting === run.run_id ? "记录中…" : "标记问题"}
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}

// These three helpers are duplicated from App.tsx on purpose: App still uses
// them for the chat header, the daily board and the run list, and sharing them
// across the two files would mean relocating them to a module both can import.
// That is a pure-move refactor and is deliberately left out of this commit so
// the timeline work stays reviewable on its own.
function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    cancelling: "正在取消",
    completed: "已完成",
    cancelled: "已取消",
    failed: "执行失败",
    partial_failed: "部分完成",
  };
  return labels[status || ""] || status || "等待开始";
}

function runKindLabel(kind: Run["kind"]): string {
  return kind === "daily" ? "每日检索" : kind === "research" ? "深度研究" : kind === "experiment" ? "论文复现" : "对话";
}

function badcaseCategoryLabel(category: BadcaseCategory): string {
  return ({
    retrieval_miss: "召回遗漏",
    citation_quality: "引用质量",
    answer_quality: "回答质量",
    tool_failure: "工具失败",
    performance: "性能",
    safety: "安全",
    other: "其他",
  })[category];
}

export function durationLabel(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "";
  }
  return value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${Math.round(value)} 毫秒`;
}
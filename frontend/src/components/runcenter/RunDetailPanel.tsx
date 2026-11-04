import { useEffect, useState } from "react";

import { getDailyRunDetail, getRun } from "../../api";
import type { Run, RunEvent, RunErrorDetails as ErrorDetails } from "../../types";
import { buildTimeline, type Timeline } from "../../lib/timeline";
import { StageTimeline } from "./StageTimeline";
import { RunErrorDetails } from "../RunErrorDetails";

/**
 * Detail panel for one run.
 *
 * Events are fetched only when a run is selected: the list endpoint returns 20
 * runs without events, so loading them all up front would multiply the request
 * count for data most rows never display.
 *
 * Daily runs read their timeline from the workspace endpoint rather than
 * `/runs/{id}`. Both expose the same event shape, but the canonical route needs
 * the Redis run manager to be configured, whereas the workspace endpoint only
 * needs the scheduler — so the timeline keeps working in a deployment without
 * the durable worker boundary.
 */
export function RunDetailPanel({
  apiKey,
  run,
  onClose,
}: {
  apiKey: string;
  run: Run | null;
  onClose: () => void;
}) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [errorDetails, setErrorDetails] = useState<ErrorDetails | undefined>();
  const [errorType, setErrorType] = useState("");

  useEffect(() => {
    if (!run) {
      setEvents([]);
      setError("");
      setErrorDetails(undefined);
      setErrorType("");
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    setErrorDetails(run.error_details);
    setErrorType(run.error_type);
    const request = run.kind === "daily"
      ? getDailyRunDetail(apiKey, run.run_id).then((detail) => ({ events: detail.events ?? [], error_details: run.error_details, error_type: run.error_type }))
      : getRun(apiKey, run.run_id).then((detail) => ({ events: detail.events ?? [], error_details: detail.error_details, error_type: detail.error_type }));
    request
      .then((loaded) => {
        if (!cancelled) {
          setEvents(loaded.events);
          setErrorDetails(loaded.error_details);
          setErrorType(loaded.error_type);
        }
      })
      .catch((cause) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "无法读取该运行的阶段事件");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apiKey, run]);

  if (!run) {
    return (
      <section className="run-detail" aria-label="运行详情">
        <p className="empty-panel">从左侧选择一个运行，查看它的阶段时间轴与工具调用。</p>
      </section>
    );
  }

  const timeline: Timeline = buildTimeline(events, run.kind);
  const measuredTotal = timeline.stages.reduce(
    (total, stage) => total + (stage.durationMs ?? 0),
    0,
  );

  return (
    <section className="run-detail" aria-label={`运行 ${run.run_id} 的详情`}>
      <header className="run-detail-heading">
        <div>
          <p className="eyebrow">运行详情</p>
          <h2 title={run.run_id}>{run.run_id}</h2>
          <p className="run-detail-meta">
            总耗时 {typeof run.duration_ms === "number" ? format(run.duration_ms) : "—"}
            {measuredTotal > 0 && ` · 阶段合计 ${format(measuredTotal)}`}
          </p>
        </div>
        <button className="button button-secondary" type="button" onClick={onClose}>
          关闭
        </button>
      </header>

      {error && <p className="run-detail-error">{error}</p>}
      {loading && !events.length && <p className="empty-panel">正在读取阶段事件…</p>}

      <RunErrorDetails runId={run.run_id} errorType={errorType} details={errorDetails} />

      <StageTimeline timeline={timeline} kind={run.kind} />
    </section>
  );
}

function format(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

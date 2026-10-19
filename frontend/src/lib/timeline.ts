/**
 * Stage-timeline reducer.
 *
 * This is deliberately a pure function with no React and no network access, so
 * the rule that matters most can be unit tested on its own:
 *
 *   duration comes from the backend `duration_ms` or from nowhere.
 *
 * Deriving it from the gap between `at_ms` values looks plausible and is wrong:
 * it charges the previous stage's teardown to the next stage, and it silently
 * invents a number whenever a stage never reported its own timing. If the UI
 * estimates and the backend measures, a mismatch becomes impossible to debug —
 * there are two truths and no way to tell which one to believe. So a missing
 * duration stays `null` and renders as "—".
 */

export interface TimelineInputEvent {
  type?: string;
  stage?: string;
  status?: string;
  tool?: string;
  /** Present on token events; ignored here, declared so token shapes type-check. */
  text?: string;
  at_ms?: number;
  duration_ms?: number;
}

export interface TimelineStage {
  /** Backend stage name, e.g. `planner`. Kept verbatim for traceability. */
  stage: string;
  /** The last status this stage reported. */
  status: string;
  /** Offset from the start of the run, in milliseconds. */
  startMs: number;
  /** Measured duration, or null when the backend did not report one. */
  durationMs: number | null;
  /** True while the stage is still open. */
  running: boolean;
  /** Number of events that carried this stage. */
  eventCount: number;
}

export interface TimelineToolCall {
  tool: string;
  status: string;
  durationMs: number | null;
  atMs: number | null;
}

export interface Timeline {
  stages: TimelineStage[];
  tools: TimelineToolCall[];
  /** Longest measured or observed extent, used to scale the bars. */
  extentMs: number;
}

const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "partial",
  "partial_failed",
  "skipped",
]);

/**
 * Stages that are part of a pipeline but did not run.
 *
 * `critic` only executes when the quality gate says it can add value, so its
 * absence is meaningful information rather than a gap to paper over. Rendering
 * it as a grey zero-width row keeps the pipeline shape readable.
 */
const OPTIONAL_STAGES: Record<string, string[]> = {
  research: ["planner", "researchers", "evidence_analysis", "synthesis", "critic", "revision"],
  daily: ["planner", "scouts", "normalizer", "quality_gate", "curator", "critic", "delivery"],
};

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function buildTimeline(
  events: TimelineInputEvent[],
  kind: string = "research",
): Timeline {
  const byStage = new Map<string, TimelineStage>();
  const order: string[] = [];
  const tools: TimelineToolCall[] = [];
  // Tracked separately from `startMs`: 0 is a legitimate measured offset, so it
  // cannot double as the "not measured yet" sentinel.
  const startsSeen = new Set<string>();

  for (const event of events) {
    if (event.type === "tool") {
      if (event.tool) {
        tools.push({
          tool: event.tool,
          status: event.status || "completed",
          durationMs: finite(event.duration_ms),
          atMs: finite(event.at_ms),
        });
      }
      continue;
    }
    const stage = event.stage;
    if (!stage) continue;

    const atMs = finite(event.at_ms);
    const durationMs = finite(event.duration_ms);
    const existing = byStage.get(stage);
    if (!existing) {
      order.push(stage);
      byStage.set(stage, {
        stage,
        status: event.status || "running",
        // No measured offset means the backend sent none; start at zero rather
        // than inventing a position from the array index.
        startMs: atMs ?? 0,
        durationMs,
        running: !TERMINAL_STATUSES.has(event.status || "running"),
        eventCount: 1,
      });
      if (atMs !== null) startsSeen.add(stage);
      continue;
    }

    existing.eventCount += 1;
    if (event.status) existing.status = event.status;
    existing.running = !TERMINAL_STATUSES.has(event.status || existing.status);
    // Only the first measured offset counts. Repeated status refreshes must not
    // drag the bar forwards, or the stage would appear to start when it ended.
    if (atMs !== null && !startsSeen.has(stage)) {
      existing.startMs = atMs;
      startsSeen.add(stage);
    }
    if (durationMs !== null) existing.durationMs = durationMs;
  }

  const stages = order.map((stage) => byStage.get(stage)!).filter(Boolean);

  // Pad the known pipeline so a run that is still mid-flight reads as "stages
  // remaining" rather than "finished with fewer stages".
  for (const stage of OPTIONAL_STAGES[kind] || []) {
    if (byStage.has(stage)) continue;
    stages.push({
      stage,
      status: "pending",
      startMs: 0,
      durationMs: null,
      running: false,
      eventCount: 0,
    });
  }

  let extentMs = 0;
  for (const stage of stages) {
    const end = stage.startMs + (stage.durationMs ?? 0);
    if (Number.isFinite(end) && end > extentMs) extentMs = end;
  }
  for (const tool of tools) {
    const end = (tool.atMs ?? 0) + (tool.durationMs ?? 0);
    if (Number.isFinite(end) && end > extentMs) extentMs = end;
  }

  return { stages, tools, extentMs };
}

/**
 * Total time actually attributed to stages.
 *
 * Sums only measured durations, so an unmeasured stage cannot inflate the total
 * and make the parts look larger than the whole.
 */
export function measuredTotalMs(timeline: Timeline): number {
  return timeline.stages.reduce((total, stage) => total + (stage.durationMs ?? 0), 0);
}

export function hasMeasuredTiming(timeline: Timeline): boolean {
  return timeline.stages.some((stage) => stage.durationMs !== null);
}
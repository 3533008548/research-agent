import {
  buildTimeline,
  hasMeasuredTiming,
  measuredTotalMs,
  type Timeline,
  type TimelineStage,
} from "../../lib/timeline";

/**
 * Stage timeline for one run.
 *
 * Bar geometry comes from the backend's `at_ms`/`duration_ms` only. A stage the
 * backend never timed shows a dashed outline and "—" rather than a bar, so a
 * missing measurement is visible instead of looking like an instant stage.
 */
export function StageTimeline({
  timeline,
  kind,
}: {
  timeline: Timeline;
  kind: string;
}) {
  const measured = hasMeasuredTiming(timeline);
  const total = measuredTotalMs(timeline);
  const scale = Math.max(timeline.extentMs, 1);

  return (
    <div className="stage-timeline">
      <div className="stage-timeline-header">
        <h3>阶段时间轴</h3>
        <span className="stage-timeline-note">
          {measured
            ? `已测量阶段合计 ${formatMs(total)}`
            : "后端未提供阶段耗时，此处不做估算"}
        </span>
      </div>

      <ol className="stage-list">
        {timeline.stages.map((stage) => (
          <StageRow key={stage.stage} stage={stage} scale={scale} kind={kind} />
        ))}
      </ol>

      {timeline.tools.length > 0 && (
        <>
          <div className="stage-timeline-header">
            <h3>工具调用</h3>
            <span className="stage-timeline-note">{timeline.tools.length} 次</span>
          </div>
          <ul className="tool-trace">
            {timeline.tools.map((tool, index) => (
              <li className="tool-trace-row" key={`${tool.tool}-${index}`}>
                <span className="tool-trace-name">{tool.tool}</span>
                <span className={`tool-trace-status tool-trace-${tool.status}`}>
                  {tool.status === "completed" ? "完成" : tool.status}
                </span>
                <span className="tool-trace-duration">
                  {tool.durationMs === null ? "—" : formatMs(tool.durationMs)}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function StageRow({
  stage,
  scale,
  kind,
}: {
  stage: TimelineStage;
  scale: number;
  kind: string;
}) {
  const offset = Math.max(0, Math.min(100, (stage.startMs / scale) * 100));
  const width =
    stage.durationMs === null
      ? 0
      : Math.max(0.6, Math.min(100 - offset, (stage.durationMs / scale) * 100));
  const timed = stage.durationMs !== null;

  return (
    <li className={`stage-row stage-${stage.status}`}>
      <div className="stage-row-head">
        <span className="stage-name" title={`${kind} · ${stage.stage}`}>
          {stage.stage}
        </span>
        <span className="stage-status">{statusText(stage)}</span>
        <span className="stage-duration">{timed ? formatMs(stage.durationMs) : "—"}</span>
      </div>
      <div className="stage-track">
        <span
          className={`stage-bar${timed ? "" : " stage-bar-unmeasured"}`}
          style={timed ? { left: `${offset}%`, width: `${width}%` } : undefined}
        />
      </div>
    </li>
  );
}

function statusText(stage: TimelineStage): string {
  if (stage.status === "pending") return "未执行";
  if (stage.status === "skipped") return "已跳过";
  if (stage.status === "partial" || stage.status === "partial_failed") return "部分完成";
  if (stage.status === "completed") return "已完成";
  if (stage.status === "failed") return "失败";
  if (stage.status === "cancelled") return "已取消";
  if (stage.running) return "进行中";
  return stage.status;
}

export function formatMs(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

export { buildTimeline };
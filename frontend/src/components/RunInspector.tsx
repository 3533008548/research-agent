import { durationLabel, runKindLabel, stageLabel, statusLabel, toolLabel } from "../lib/labels";
import type { Run, RunEvent } from "../types";

/**
 * The run inspector: what the current run is doing, and the events it
 * produced.
 *
 * Extracted from `App.tsx` without behaviour change.  The event list is
 * deliberately free of the token stream — the transcript shows the answer,
 * this shows the machinery that produced it.
 */
export function RunInspector({
  run,
  events,
  onCancel,
  onRetry,
  onOpenRunCenter,
  cancelling,
}: {
  run: Run | null;
  events: RunEvent[];
  onCancel: () => void;
  onRetry: () => void;
  onOpenRunCenter: () => void;
  cancelling: boolean;
}) {
  return (
    <aside className="run-inspector" aria-label="运行检查器">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">运行检查器</p>
          <h2>本轮任务</h2>
        </div>
        <div className="run-inspector-actions">
          <button className="text-button" type="button" onClick={onOpenRunCenter}>历史运行</button>
          {run && <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>}
        </div>
      </div>

      {!run ? (
        <p className="empty-panel">开始一项对话或深度研究后，这里会显示脱敏的运行状态和工具活动。</p>
      ) : (
        <>
          <dl className="run-summary">
            <div><dt>类型</dt><dd>{runKindLabel(run.kind)}</dd></div>
            <div><dt>耗时</dt><dd>{durationLabel(run.duration_ms) || "进行中"}</dd></div>
            {run.model && <div><dt>模型</dt><dd>{run.model}</dd></div>}
          </dl>
          {!["completed", "cancelled", "failed"].includes(run.status) && (
            <button
              className="button button-secondary button-full"
              type="button"
              onClick={onCancel}
              disabled={cancelling}
              title="协作式停止当前任务；不是可恢复的进程暂停"
            >
              {cancelling ? "正在暂停…" : "暂停任务"}
            </button>
          )}
          {run.kind === "chat" && ["failed", "cancelled"].includes(run.status) && (
            <button
              className="button button-secondary button-full"
              type="button"
              onClick={onRetry}
            >
              重新发送本轮问题
            </button>
          )}
          <ol className="run-events">
            {events.map((event, index) => (
              <li key={`${event.type}-${index}`} className="run-event">
                <span className={`event-dot event-${event.type}`} aria-hidden="true" />
                <div>
                  <strong>
                    {event.type === "tool" ? toolLabel(event.tool) : statusLabel(event.status)}
                  </strong>
                  <p>
                    {event.type === "tool"
                      ? `工具${event.status === "completed" ? "已完成" : "正在执行"}${durationLabel(event.duration_ms) ? ` · ${durationLabel(event.duration_ms)}` : ""}`
                      : event.type === "error"
                        ? event.error_type || "运行出错"
                        : stageLabel(event.stage, event.type === "done" ? "已生成最终结果" : "任务状态更新")}
                  </p>
                </div>
              </li>
            ))}
          </ol>
          {run.error_type && <p className="run-error">错误类型：{run.error_type}</p>}
        </>
      )}
    </aside>
  );
}
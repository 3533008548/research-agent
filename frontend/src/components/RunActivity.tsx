import { durationLabel, stageLabel, statusLabel, toolLabel } from "../lib/labels";
import { agentLabel, collectToolCalls } from "../lib/toolActivity";
import { verificationNotice } from "../lib/runErrors";
import type { Run, RunEvent } from "../types";

const toolErrors: Record<string, string> = {
  ToolResultError: "工具返回了失败结果",
  ToolAccessDenied: "当前角色无权使用此工具",
  SourceScopeDenied: "来源超出指定研究范围",
  UnknownTool: "工具不存在",
  ToolDeadlineExceeded: "工具执行超时",
  RequestCancelledError: "调用已取消",
};

export function RunActivity({ run, events }: { run: Run; events: RunEvent[] }) {
  const activity = run.research?.activity || events;
  const calls = collectToolCalls(activity, run.status, run.research?.jobs);
  const running = calls.filter(call => call.status === "running");
  const completed = calls.filter(call => call.status === "completed").length;
  const failed = calls.filter(call => call.status === "failed").length;
  const latest = [...activity].reverse().find(event => event.type === "status" && event.stage);
  const active = ["running", "queued", "cancelling"].includes(run.status);
  const jobs = run.research?.jobs?.filter(job => job.version === run.research?.goal_version && ["running", "queued"].includes(job.state)) || [];
  return <>
    <section className="current-activity" aria-label="当前活动">
      <h3>当前活动</h3>
      {!active ? <p>{statusLabel(run.status)}</p> : running.length
        ? running.map(call => <p key={call.key}>{agentLabel(call.agent)} · 正在{toolLabel(call.tool)}</p>)
        : jobs.length ? jobs.map(job => <p key={job.job_id}>{agentLabel(job.role)} · {job.state === "queued" ? "排队中" : "正在分析与生成"}</p>)
        : <p>{latest ? `${stageLabel(latest.stage, "处理任务")} · ${latest.status === "waiting" ? "等待服务" : statusLabel(latest.status)}` : "等待下一条活动记录"}</p>}
      {verificationNotice(activity) && <p className="verification-warning">{verificationNotice(activity)}</p>}
    </section>
    <section className="tool-activity" aria-label="工具调用记录">
      <h3>工具调用 <span>{calls.length} 次</span></h3>
      {!!calls.length && <p className="tool-activity-counts">执行中 {running.length} · 成功 {completed} · 失败 {failed}</p>}
      {!calls.length && <p className="empty-panel">{active ? "本轮尚无工具调用记录。" : "本轮没有保存工具调用记录。"}</p>}
      {(calls.length > 100 || (run.research?.activity?.length || 0) >= 500) && <p className="tool-activity-counts">仅展示最近的调用记录</p>}
      <ol className="tool-activity-list">
        {calls.slice(-100).reverse().map(call => <li key={call.key} className={`tool-call tool-call-${call.status}`}>
          <div className="tool-call-heading"><strong>{toolLabel(call.tool)}</strong><span className={`status status-${call.status}`}>{statusLabel(call.status)}</span></div>
          {call.tool && <code className="tool-call-name">{call.tool}</code>}
          <p>{agentLabel(call.agent)}{call.version ? ` · 目标版本 ${call.version}` : ""}{durationLabel(call.duration_ms) ? ` · ${durationLabel(call.duration_ms)}` : ""}</p>
          {call.error_type && <p className="tool-call-error">{toolErrors[call.error_type] || `调用异常：${call.error_type}`}</p>}
        </li>)}
      </ol>
    </section>
  </>;
}

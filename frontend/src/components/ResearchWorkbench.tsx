import { lazy, Suspense, useEffect, useState } from "react";
import { controlResearchProject, getResearchProject, getResearchReport, listResearchProjects } from "../api";
import type { ResearchProject } from "../types";
import { ResearchProjectPicker } from "./ResearchProjectPicker";

const MarkdownContent = lazy(() => import("../MarkdownContent"));
const states: Record<string, string> = {
  active: "持续推进中", waiting: "等待材料", needs_input: "需要你的决定", paused: "已暂停",
  stopped: "已停止", ended: "本阶段已结束", budget_exhausted: "预算已用尽",
  supported: "有证据支持", conflicting: "证据冲突", conditional: "有条件成立",
  insufficient: "证据不足", unverified: "待核验",
};
const reasons: Record<string, string> = {
  fulfilled: "完成标准已满足", budget: "预算已用尽", user_stop: "用户停止",
  no_progress: "当前路径未获得新证据", no_feasible_path: "当前没有可行的后续路径",
  insufficient_evidence: "结论仍有证据缺口", unverified: "核验未完成",
};
const failures: Record<string, string> = {
  LLMRequestTimeoutError: "模型请求超时", empty_response: "模型未返回正文", output_truncated: "模型输出被截断",
  invalid_response: "模型响应格式无效", invalid_decision: "模型决策格式无效", budget: "剩余预算不足",
  attempt_limit: "任务恢复次数已用尽", cancelled: "任务已取消", retrieval_failed: "检索失败",
  LLMRequestFailedError: "模型请求失败", LLMQueueFullError: "模型请求队列已满",
};

export function ResearchWorkbench({ apiKey, currentId, onChange }: { apiKey: string; currentId?: string; onChange: (project: ResearchProject) => void }) {
  const [projects, setProjects] = useState<Array<Pick<ResearchProject, "project_id" | "question">>>([]);
  const [selected, setSelected] = useState(currentId || "");
  const [project, setProject] = useState<ResearchProject | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [historicalReport, setHistoricalReport] = useState<{ version: number; snapshot: { report: string } } | null>(null);
  useEffect(() => setHistoricalReport(null), [selected]);
  useEffect(() => { if (currentId) setSelected(currentId); }, [currentId]);
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const list = await listResearchProjects(apiKey);
        const id = selected || list[0]?.project_id;
        const detail = id ? await getResearchProject(apiKey, id) : null;
        if (!cancelled) {
          setProjects(list);
          setProject(current => current && detail && current.project_id === detail.project_id && current.updated_at > detail.updated_at ? current : detail);
          if (detail) onChange(detail);
          setError("");
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "研究状态读取失败");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [apiKey, selected, currentId, onChange]);

  async function control(action: string) {
    if (!project) return;
    setBusy(true);
    try {
      const updated = await controlResearchProject(apiKey, project.project_id, action, message);
      setProject(current => current?.project_id === updated.project_id ? { ...current, ...updated } : updated);
      onChange(updated); setError("");
      if (action === "steer" || action === "material") setMessage("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "操作失败"); }
    finally { setBusy(false); }
  }

  return <section className="research-workbench" aria-label="持续研究工作台">
    <h3>多 Agent 研究</h3>
    <p className="composer-hint">1 位负责人 · 2 位研究员 · 1 位质检员</p>
    {error && <p role="alert">{error}</p>}
    {!projects.length ? <p className="empty-panel">在下方输入研究问题并开始。研究会保留进度，也可随时暂停。</p> : <>
      <ResearchProjectPicker projects={projects} selected={selected || project?.project_id || ""} onSelect={setSelected} />
      {project && <>
        <p><strong>{states[project.execution_state] || project.execution_state}</strong> · 目标版本 {project.goal_version}</p>
        {project.end_reason && <p>{reasons[project.end_reason] || project.end_reason}</p>}
        {project.last_failure?.version === project.goal_version && project.execution_state !== "active" && project.end_reason !== "fulfilled" &&
          <p role="status">最近失败：{failures[project.last_failure.error] || "任务执行失败"}。已保留进度；恢复会继续使用剩余预算。</p>}
        <p>模型调用 {project.budget.calls} / {project.budget.max_calls} · Token {project.budget.tokens.toLocaleString()} / {project.budget.max_tokens.toLocaleString()}</p>
        <progress aria-label="研究调用预算" value={project.budget.calls} max={project.budget.max_calls} />
        <div className="research-controls">
          {["active", "waiting", "needs_input"].includes(project.execution_state)
            ? <button disabled={busy} onClick={() => void control("pause")}>暂停研究</button>
            : <button disabled={busy || project.budget.calls >= project.budget.max_calls || project.budget.tokens >= project.budget.max_tokens} onClick={() => void control("resume")}>恢复研究</button>}
          {!["ended", "stopped", "budget_exhausted"].includes(project.execution_state) && <button disabled={busy} onClick={() => void control("stop")}>停止研究</button>}
        </div>
        {!!project.waiting_for.length && <div><h4>等待事项</h4><ul>{project.waiting_for.map(item => <li key={item}>{item}</li>)}</ul></div>}
        <label>补充约束或材料标识<textarea value={message} onChange={event => setMessage(event.target.value)} rows={2} placeholder="说明目标调整，或填写等待的材料标识" /></label>
        <div className="research-controls">
          <button disabled={busy || !message.trim()} onClick={() => void control("steer")}>更新研究目标</button>
          <button disabled={busy || !message.trim()} onClick={() => void control("material")}>材料已就绪</button>
        </div>
        <p className="composer-hint">暂停后更新目标不会自动恢复。恢复沿用剩余预算。删除聊天会保留研究。</p>
        {!!project.criteria.length && <details><summary>完成标准</summary><ul>{project.criteria.map((item, i) => <li key={i}>{item}</li>)}</ul></details>}
        {!!project.claims.length && <details open><summary>当前判断 · 目标版本 {project.claims_version}</summary>
          {project.claims_version !== project.goal_version && <p>目标已更新，这些判断尚待重新评估。</p>}<ul>{project.claims.map((claim, i) => <li key={claim.id || i}>
          <strong>{states[claim.status] || claim.status}</strong>：{claim.text}
          {!!claim.support?.length && <p>支持：{claim.support.join("、")}</p>}
          {!!claim.opposition?.length && <p>反证：{claim.opposition.join("、")}</p>}
          {claim.limitations && <p>{typeof claim.limitations === "string" ? claim.limitations : JSON.stringify(claim.limitations)}</p>}
        </li>)}</ul></details>}
        {!!project.gaps.length && <details open><summary>未解决问题</summary><ul>{project.gaps.map((gap, i) => <li key={i}>{typeof gap === "string" ? gap : gap.text || gap.question || JSON.stringify(gap)}</li>)}</ul></details>}
        {!!project.jobs?.length && <details><summary>研究任务</summary><ul>{project.jobs.filter(job => job.version === project.goal_version).map(job => <li key={job.job_id}>
          {{ lead: "负责人", researcher1: "研究员一", researcher2: "研究员二", critic: "质检员", report: "负责人撰写报告" }[job.role] || job.role} · {job.state === "queued" && job.result?.error ? "等待恢复（最多一次）" :
            job.state === "done" && job.result?.error ? "执行失败" :
            ({ queued: "待执行", running: "执行中", done: "已完成", failed: "执行失败", stale: "已失效" }[job.state] || job.state)}
          {job.result?.error && <p>{failures[job.result.error] || "任务执行失败"}</p>}
          {job.payload.question && <p>{job.payload.question}</p>}
        </li>)}</ul></details>}
        <details><summary>证据库（{project.evidence.length}）</summary>{project.evidence.map(item => <div key={item.stable_id}><h4>[{item.id}] {item.source}</h4><p>{item.excerpt}</p></div>)}</details>
        {project.partial_report && <details><summary>未完成的报告草稿 · 目标版本 {project.partial_report.version}</summary>
          <p>生成中断，未经过完整质检。</p><Suspense fallback={<p>读取草稿…</p>}><MarkdownContent content={project.partial_report.text} /></Suspense>
        </details>}
        {project.report && <details open><summary>{project.delivery_status === "incomplete" || project.report_version === 0 ? "未完成交付" : "阶段报告"}{project.report_version > 0 && ` · 目标版本 ${project.report_version}`}</summary>
          {project.report_version > 0 && project.report_version !== project.goal_version && <p>这是先前目标下的报告。</p>}
          <Suspense fallback={<p>{project.report}</p>}><MarkdownContent content={project.report} /></Suspense></details>}
        {!!project.reports?.length && <details><summary>历史报告（{project.reports.length}）</summary>
          {project.reports.map(report => <button key={report.report_id} className="text-button" onClick={() => {
            void getResearchReport(apiKey, project.project_id, report.report_id).then(setHistoricalReport)
              .catch(cause => setError(cause instanceof Error ? cause.message : "历史报告读取失败"));
          }}>版本 {report.version} · {new Date(report.created_at).toLocaleString()}</button>)}
          {historicalReport && <Suspense fallback={<p>读取报告…</p>}><MarkdownContent content={historicalReport.snapshot.report} /></Suspense>}
        </details>}
      </>}
    </>}
  </section>;
}

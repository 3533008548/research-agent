import type { ResearchProject, RunEvent } from "../types.ts";

export type ToolCall = RunEvent & { key: string };

/** Pair lifecycle records by invocation, not by tool name (parallel calls may share a name). */
export function collectToolCalls(events: RunEvent[], runStatus: string, jobs?: ResearchProject["jobs"]): ToolCall[] {
  const calls: ToolCall[] = [];
  for (const event of events) {
    if (event.type !== "tool") continue;
    const previous = event.call_id
      ? calls.find(call => call.call_id === event.call_id)
      : [...calls].reverse().find(call => !call.call_id && call.tool === event.tool && call.agent === event.agent && call.status === "running");
    if (previous) {
      // Reconnects may replay a start after we have received its terminal record.
      if (event.status === "running" && previous.status !== "running") continue;
      Object.assign(previous, event);
    } else calls.push({ ...event, key: event.call_id || `legacy-${calls.length}` });
  }
  return calls.map(call => {
    if (call.status !== "running") return call;
    const job = jobs?.find(item => item.job_id === call.job_id);
    const closed = !["running", "queued", "cancelling"].includes(runStatus)
      || (job && (job.state !== "running" || (call.attempt && call.attempt !== job.attempts)));
    return closed ? { ...call, status: "interrupted" } : call;
  });
}

export function agentLabel(agent?: string): string {
  return ({ single_agent: "对话 Agent", researcher1: "研究员一", researcher2: "研究员二",
    lead: "负责人", report: "报告撰写", critic: "质检员", verifier: "核验 Agent" } as Record<string, string>)[agent || ""] || agent || "对话 Agent";
}

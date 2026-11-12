import type { RunErrorDetails, RunEvent } from "../types";

const PHASES: Record<string, string> = {
  queue: "等待模型请求名额",
  connect: "建立连接／等待响应头",
  response_headers: "接收响应头",
  stream_read: "读取流式响应",
  retry: "重试模型请求",
  unknown: "未记录",
};

const ERROR_CODES: Record<string, string> = {
  LLM_HTTP_ERROR: "模型 HTTP 请求失败",
  LLM_DEADLINE_EXCEEDED: "模型请求总时间预算耗尽",
  LLM_STREAM_TIMEOUT: "模型流式响应超时",
  LLM_RESPONSE_TIMEOUT: "等待模型响应超时",
  LLM_CONNECT_TIMEOUT: "模型连接超时",
  LLM_CONNECTION_ERROR: "模型连接失败",
  LLM_QUEUE_TIMEOUT: "等待模型请求名额超时",
  LLM_QUEUE_FULL: "模型请求队列已满",
  LLM_CIRCUIT_OPEN: "模型服务暂不可用",
  LLM_STREAM_INTERRUPTED: "模型流式响应中断",
  LLM_REQUEST_FAILED: "模型请求失败",
};

function nonnegative(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function exceptionName(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z_][A-Za-z0-9_.]{0,79}$/.test(value);
}

function milliseconds(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(2)} 秒` : `${Math.round(value)} 毫秒`;
}

/** Present known fields only; old runs must not acquire an invented HTTP code. */
export function errorDetailRows(details: RunErrorDetails | null | undefined): Array<{ label: string; value: string }> {
  if (!details || typeof details !== "object") return [];
  const rows: Array<{ label: string; value: string }> = [];
  if (typeof details.code === "string" && Object.hasOwn(ERROR_CODES, details.code)) rows.push({ label: "错误代码", value: `${details.code}（${ERROR_CODES[details.code]}）` });
  if (typeof details.phase === "string" && Object.hasOwn(PHASES, details.phase)) rows.push({ label: "失败阶段", value: PHASES[details.phase] });
  if (exceptionName(details.exception_type)) rows.push({ label: "异常类型", value: details.exception_type });
  if (exceptionName(details.cause_type)) rows.push({ label: "底层原因", value: details.cause_type });
  if (Number.isInteger(details.http_status) && details.http_status! >= 100 && details.http_status! <= 599) {
    rows.push({ label: "最后 HTTP 状态", value: `HTTP ${details.http_status}` });
  }
  if (nonnegative(details.elapsed_ms)) rows.push({ label: "实际耗时", value: milliseconds(details.elapsed_ms) });
  if (nonnegative(details.deadline_ms)) rows.push({ label: "总时间预算", value: milliseconds(details.deadline_ms) });
  if (nonnegative(details.queue_wait_ms)) rows.push({ label: "排队耗时", value: milliseconds(details.queue_wait_ms) });
  if (nonnegative(details.attempts) && Number.isInteger(details.attempts)) rows.push({ label: "实际请求次数", value: String(details.attempts) });
  if (nonnegative(details.retries_used) && Number.isInteger(details.retries_used)) rows.push({ label: "实际重试次数", value: String(details.retries_used) });
  if (typeof details.request_id === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$/.test(details.request_id)
      && !/(bearer|authorization|api_key|api-key|sk-)/i.test(details.request_id)) {
    rows.push({ label: "上游请求 ID", value: details.request_id });
  }
  return rows;
}

export function failedRunEvent(event: RunEvent): boolean {
  return event.type === "error" || event.status === "failed" || event.status === "partial_failed";
}

export function runEventStatusLabel(event: RunEvent): string {
  if (event.status === "waiting" && ["model", "verify"].includes(event.stage || "")) return "等待模型服务";
  if (event.stage === "verify" && failedRunEvent(event)) {
    return event.error_type === "LLMRequestTimeoutError" || /TIMEOUT|DEADLINE_EXCEEDED/.test(event.error_details?.code || "")
      ? "核验超时" : "核验未完成";
  }
  return "";
}

export function verificationIncomplete(events: RunEvent[]): boolean {
  const latest = [...events].reverse().find(event => event.stage === "verify");
  return !!latest && ["failed", "skipped", "cancelled", "unresolved", "inconclusive"].includes(latest.status || "");
}

export function verificationNotice(events: RunEvent[]): string {
  const latest = [...events].reverse().find(event => event.stage === "verify");
  if (latest?.status === "unresolved") return "仍有待确认的事实问题；已达到一次自动修正上限。";
  if (latest?.status === "inconclusive") return "核验未返回可用结论，本轮回复仍需核对。";
  if (latest?.status === "advisory") return "核验有非阻断提示，已保留回答。";
  return verificationIncomplete(events) ? "事实核验未完成，本轮回复未经完整核验。" : "";
}

/** A terminal transport event can describe failure; its type is not success. */
export function runEventDescription(event: RunEvent, fallback = event.stage || "任务状态更新"): string {
  if (event.stage === "verify" && failedRunEvent(event)) {
    return `${runEventStatusLabel(event)}，已保留回复；这不表示发现了事实错误。`;
  }
  if (event.summary) return event.summary;
  if (event.stage === "verify") {
    const descriptions: Record<string, string> = {
      passed: "本次核验未发现问题", advisory: "有非阻断提示，保留回答，不触发自动重写",
      revision_requested: "发现影响核心结论的事实问题，开始一次定向修正",
      unresolved: "仍有待确认问题，已达到一次自动修正上限",
      inconclusive: "核验未返回可用结论，保留回复，不自动重写",
      completed: "核验模型已返回结果",
    };
    if (descriptions[event.status || ""]) return descriptions[event.status!];
  }
  if (failedRunEvent(event)) return event.error_type ? `运行未完成：${event.error_type}` : "运行未完成，可查看错误诊断";
  if (event.status === "cancelled") return "任务已取消";
  if (event.type === "done" && event.status === "completed") return "已生成最终结果";
  return fallback;
}

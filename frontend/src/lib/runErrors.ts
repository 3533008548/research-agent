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

/** A terminal transport event can describe failure; its type is not success. */
export function runEventDescription(event: RunEvent, fallback = event.stage || "任务状态更新"): string {
  if (event.summary) return event.summary;
  if (failedRunEvent(event)) return event.error_type ? `运行未完成：${event.error_type}` : "运行未完成，可查看错误诊断";
  if (event.status === "cancelled") return "任务已取消";
  if (event.type === "done" && event.status === "completed") return "已生成最终结果";
  return fallback;
}

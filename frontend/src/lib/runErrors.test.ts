import assert from "node:assert/strict";
import test from "node:test";
import { errorDetailRows, failedRunEvent, runEventDescription } from "./runErrors.ts";

test("shows recorded HTTP and measured timing without guessing missing fields", () => {
  const rows = errorDetailRows({
    code: "LLM_HTTP_ERROR", phase: "response_headers", http_status: 401,
    exception_type: "LLMRequestFailedError", cause_type: "HTTPError",
    elapsed_ms: 512.4, deadline_ms: 45000, queue_wait_ms: 0,
    attempts: 1, retries_used: 0, request_id: "req-fixture-401",
  });
  assert.equal(rows.find((row) => row.label === "最后 HTTP 状态")?.value, "HTTP 401");
  assert.equal(rows.find((row) => row.label === "排队耗时")?.value, "0 毫秒");
  assert.equal(rows.find((row) => row.label === "实际重试次数")?.value, "0");
  assert.equal(rows.find((row) => row.label === "总时间预算")?.value, "45.00 秒");
  assert.deepEqual(errorDetailRows(undefined), []);
  assert.equal(errorDetailRows({ code: "LLM_STREAM_TIMEOUT" }).some((row) => row.label === "最后 HTTP 状态"), false);
});

test("does not render unknown payload keys or invalid diagnostic values", () => {
  const rows = errorDetailRows({
    code: "sk-private-key", exception_type: "HTTPError https://secret", cause_type: "Bearer token",
    http_status: 999, elapsed_ms: Number.NaN, deadline_ms: -1, attempts: 1.5,
    retries_used: Number.POSITIVE_INFINITY, request_id: "Bearer private-token",
    response_body: "secret response", url: "https://private", prompt: "private question",
  } as Parameters<typeof errorDetailRows>[0]);
  assert.deepEqual(rows, []);
  assert.deepEqual(errorDetailRows({ code: "constructor", phase: "toString", request_id: "sk-private-token" }), []);
});

test("failed terminal events retain failure presentation and prefer backend summaries", () => {
  assert.equal(failedRunEvent({ type: "done", status: "failed" }), true);
  assert.equal(failedRunEvent({ type: "done", status: "partial_failed" }), true);
  assert.equal(failedRunEvent({ type: "done", status: "completed" }), false);
  assert.match(runEventDescription({ type: "done", status: "failed", error_type: "llm_error" }), /未完成/);
  assert.equal(runEventDescription({ type: "done", status: "cancelled" }), "任务已取消");
  assert.equal(runEventDescription({ type: "done", status: "failed", summary: "模型流式响应超时" }), "模型流式响应超时");
  assert.equal(runEventDescription({ type: "done", status: "completed" }), "已生成最终结果");
});

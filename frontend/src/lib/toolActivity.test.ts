import test from "node:test";
import assert from "node:assert/strict";
import { collectToolCalls } from "./toolActivity.ts";
import { buildTimeline } from "./timeline.ts";
import type { RunEvent } from "../types.ts";

test("parallel calls of the same tool remain distinct and replay cannot reopen a finished call", () => {
  const events: RunEvent[] = [
    { type: "tool", tool: "read_pdf", call_id: "a", status: "running" },
    { type: "tool", tool: "read_pdf", call_id: "b", status: "running" },
    { type: "tool", tool: "read_pdf", call_id: "b", status: "failed", duration_ms: 30 },
    { type: "tool", tool: "read_pdf", call_id: "a", status: "completed", duration_ms: 40 },
    { type: "tool", tool: "read_pdf", call_id: "a", status: "running" },
  ];
  const calls = collectToolCalls(events, "completed");
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(call => [call.status, call.duration_ms]), [["completed", 40], ["failed", 30]]);
  assert.equal(events[0].status, "running");
  assert.deepEqual(buildTimeline(events, "chat").tools.map(call => [call.status, call.durationMs]), [["completed", 40], ["failed", 30]]);
});

test("missing terminal records are interrupted when the run or research attempt ended", () => {
  const event: RunEvent = { type: "tool", tool: "query_papers", call_id: "a", status: "running", job_id: "job", attempt: 1 };
  assert.equal(collectToolCalls([event], "running")[0].status, "running");
  assert.equal(collectToolCalls([event], "cancelled")[0].status, "interrupted");
  assert.equal(collectToolCalls([event], "running", [{ job_id: "job", role: "researcher1", state: "running", version: 1, attempts: 2, payload: {} }])[0].status, "interrupted");
  assert.equal(collectToolCalls([event], "completed")[0].duration_ms, undefined);
});

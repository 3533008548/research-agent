/**
 * Timeline reducer tests.
 *
 * Run with `npm run test` (Node's built-in runner + native type stripping).
 * The assertions concentrate on the rule that is easy to break and expensive to
 * get wrong: a stage duration is either measured by the backend or absent.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  buildTimeline,
  hasMeasuredTiming,
  measuredTotalMs,
} from "./timeline.ts";

test("takes duration from the backend instead of differencing at_ms", () => {
  const timeline = buildTimeline([
    { stage: "planner", status: "running", at_ms: 0 },
    { stage: "planner", status: "completed", at_ms: 900, duration_ms: 1200 },
  ]);
  const planner = timeline.stages.find((stage) => stage.stage === "planner");
  // 1200ms measured, even though the gap between the two at_ms values is 900ms.
  // A reducer that differenced would silently report 0 here, because the
  // closing event repeats the stage rather than opening a new one.
  assert.equal(planner?.durationMs, 1200);
  assert.equal(planner?.startMs, 0);
});

test("leaves duration null when the backend reported none", () => {
  const timeline = buildTimeline([
    { stage: "planner", status: "completed", at_ms: 500 },
  ]);
  const planner = timeline.stages.find((stage) => stage.stage === "planner");
  assert.equal(planner?.durationMs, null);
  assert.equal(hasMeasuredTiming(timeline), false);
});

test("never invents a duration from neighbouring stages", () => {
  const timeline = buildTimeline([
    { stage: "planner", status: "completed", at_ms: 0, duration_ms: 100 },
    { stage: "synthesis", status: "completed", at_ms: 100 },
  ]);
  const synthesis = timeline.stages.find((stage) => stage.stage === "synthesis");
  assert.equal(synthesis?.durationMs, null);
});

test("sums only measured durations so parts never exceed the whole", () => {
  const timeline = buildTimeline([
    { stage: "planner", status: "completed", at_ms: 0, duration_ms: 100 },
    { stage: "synthesis", status: "completed", at_ms: 100, duration_ms: 250 },
    { stage: "critic", status: "completed", at_ms: 350 },
  ]);
  assert.equal(measuredTotalMs(timeline), 350);
});

test("repeated running refreshes do not move the bar", () => {
  const timeline = buildTimeline([
    { stage: "scouts", status: "running", at_ms: 100 },
    { stage: "scouts", status: "running", at_ms: 200 },
    { stage: "scouts", status: "running", at_ms: 300 },
  ]);
  const scouts = timeline.stages.find((stage) => stage.stage === "scouts");
  assert.equal(scouts?.startMs, 100);
  assert.equal(scouts?.eventCount, 3);
  assert.equal(scouts?.running, true);
});

test("treats partial as terminal because it consumed real time", () => {
  const timeline = buildTimeline([
    { stage: "scouts", status: "running", at_ms: 0 },
    { stage: "scouts", status: "partial", at_ms: 800, duration_ms: 800 },
  ]);
  const scouts = timeline.stages.find((stage) => stage.stage === "scouts");
  assert.equal(scouts?.status, "partial");
  assert.equal(scouts?.running, false);
  assert.equal(scouts?.durationMs, 800);
});

test("pads the known pipeline with pending stages", () => {
  const timeline = buildTimeline(
    [{ stage: "planner", status: "completed", at_ms: 0, duration_ms: 50 }],
    "research",
  );
  const pending = timeline.stages.filter((stage) => stage.status === "pending");
  assert.deepEqual(pending.map((stage) => stage.stage), [
    "researchers",
    "evidence_analysis",
    "synthesis",
    "critic",
    "revision",
  ]);
});

test("does not pad a stage that actually ran", () => {
  const timeline = buildTimeline(
    [
      { stage: "planner", status: "completed", at_ms: 0, duration_ms: 50 },
      { stage: "critic", status: "skipped", at_ms: 60, duration_ms: 0 },
    ],
    "research",
  );
  const critic = timeline.stages.find((stage) => stage.stage === "critic");
  assert.equal(critic?.status, "skipped");
  assert.equal(critic?.eventCount, 1);
});

test("collects tool calls separately from stages", () => {
  const timeline = buildTimeline([
    { type: "tool", tool: "query_papers", status: "completed", duration_ms: 612 },
    { type: "tool", tool: "search_papers", status: "completed", duration_ms: 88 },
    { stage: "researchers", status: "completed", at_ms: 0, duration_ms: 700 },
  ]);
  assert.equal(timeline.tools.length, 2);
  assert.equal(timeline.tools[0].tool, "query_papers");
  assert.equal(timeline.tools[1].durationMs, 88);
  // Tool time must not be attributed to the enclosing stage.
  assert.equal(measuredTotalMs(timeline), 700);
});

test("extent scales bars by the true end of the run", () => {
  const timeline = buildTimeline([
    { stage: "planner", status: "completed", at_ms: 0, duration_ms: 100 },
    { stage: "synthesis", status: "completed", at_ms: 400, duration_ms: 600 },
  ]);
  assert.equal(timeline.extentMs, 1000);
});

test("ignores events without a stage or tool", () => {
  const timeline = buildTimeline([
    { type: "status", status: "running" },
    { type: "token", text: "hello" },
  ], "chat");
  assert.equal(timeline.stages.length, 0);
  assert.equal(timeline.tools.length, 0);
  assert.equal(timeline.extentMs, 0);
});

test("rejects non-finite numbers rather than rendering NaN", () => {
  const timeline = buildTimeline([
    { stage: "planner", status: "completed", at_ms: Number.NaN, duration_ms: Number.NaN },
  ]);
  const planner = timeline.stages.find((stage) => stage.stage === "planner");
  assert.equal(planner?.startMs, 0);
  assert.equal(planner?.durationMs, null);
  assert.equal(Number.isFinite(timeline.extentMs), true);
});

test("empty input still yields the pipeline skeleton", () => {
  const timeline = buildTimeline([], "daily");
  assert.ok(timeline.stages.length > 0);
  assert.equal(hasMeasuredTiming(timeline), false);
  assert.equal(measuredTotalMs(timeline), 0);
});
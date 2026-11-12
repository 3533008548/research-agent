import assert from "node:assert/strict";
import { it } from "node:test";
import { elapsedLabel, elapsedMilliseconds, timingFromRun } from "./runTiming.ts";
import type { Run } from "../types.ts";

it("uses elapsed server time across time zones and stops on terminal states", () => {
  const run = { status: "running", elapsed_ms: 92000, created_at: "2026-11-11T03:00:00" } as Run;
  const timing = timingFromRun(run, 1000)!;
  assert.equal(elapsedMilliseconds(timing, 4000), 95000);
  assert.equal(elapsedMilliseconds(timing, 0), 92000);
  const done = timingFromRun({ ...run, status: "cancelled", elapsed_ms: 98000 }, 4000)!;
  assert.equal(elapsedMilliseconds(done, 100000), 98000);
  assert.equal(timingFromRun({ ...run, elapsed_ms: undefined }), undefined);
});

it("formats elapsed hours without wrapping at an hour or day", () => {
  assert.equal(elapsedLabel(-1), "00:00");
  assert.equal(elapsedLabel(59999), "00:59");
  assert.equal(elapsedLabel(3601000), "01:00:01");
  assert.equal(elapsedLabel(90061000), "25:01:01");
});

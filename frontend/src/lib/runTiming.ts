import type { Run } from "../types";

export interface RunTiming {
  elapsedMs: number;
  sampledAt: number;
  running: boolean;
}

export function elapsedMilliseconds(timing: RunTiming, now = Date.now()): number {
  return Math.max(0, timing.elapsedMs + (timing.running ? Math.max(0, now - timing.sampledAt) : 0));
}

export function timingFromRun(run: Run, now = Date.now()): RunTiming | undefined {
  const running = ["queued", "running", "cancelling"].includes(run.status);
  const elapsed = run.elapsed_ms ?? (running ? null : run.duration_ms);
  // Old responses may have timezone-less dates. Never guess the server's zone.
  if (elapsed == null || !Number.isFinite(elapsed)) return undefined;
  return { elapsedMs: Math.max(0, elapsed), sampledAt: now, running };
}

export function stopTiming(timing: RunTiming | undefined): RunTiming | undefined {
  return timing && { ...timing, elapsedMs: elapsedMilliseconds(timing), running: false };
}

export function elapsedLabel(milliseconds: number): string {
  const seconds = Math.floor(Math.max(0, milliseconds) / 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return seconds >= 3600
    ? `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}`
    : `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}`;
}

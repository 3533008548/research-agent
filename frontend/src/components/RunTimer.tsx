import { useEffect, useState } from "react";
import { elapsedLabel, elapsedMilliseconds, type RunTiming } from "../lib/runTiming";

export function RunTimer({ timing, research = false }: { timing: RunTiming; research?: boolean }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!timing.running) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [timing.running]);
  return <div className="run-timer" role="timer" aria-live="off" aria-label={research ? "研究历时" : "本轮运行时间"}
    data-running={timing.running} title={research ? "从研究创建开始，包含暂停和等待时间" : "从提交开始计时，包含排队和工具处理时间"}>
    {research ? "研究历时" : timing.running ? "本轮已运行" : "本轮用时"} <span>{elapsedLabel(elapsedMilliseconds(timing, now))}</span>
  </div>;
}

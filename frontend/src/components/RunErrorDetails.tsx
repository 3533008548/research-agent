import { errorDetailRows } from "../lib/runErrors";
import type { RunErrorDetails as Details } from "../types";

export function RunErrorDetails({
  runId,
  errorType,
  details,
}: {
  runId: string;
  errorType?: string;
  details?: Details;
}) {
  const rows = errorDetailRows(details);
  if (!errorType && !rows.length) return null;
  return (
    <section className="run-error-details" aria-label="错误诊断">
      <h3>错误诊断</h3>
      <dl>
        <div><dt>运行 ID</dt><dd>{runId}</dd></div>
        {errorType && <div><dt>错误类型</dt><dd>{errorType}</dd></div>}
        {rows.map(({ label, value }) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      </dl>
      {!rows.length && <p>此运行未记录更详细的诊断信息。</p>}
      {typeof details?.http_status === "number" && details.http_status < 400 && rows.some((row) => row.label === "最后 HTTP 状态") && (
        <p>收到响应头不代表流式内容已完成；后续读取或重试仍可能超时。</p>
      )}
    </section>
  );
}

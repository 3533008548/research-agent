import { dailyPaperStatusLabel, durationLabel, statusLabel } from "../../lib/labels";
import type { DailyDigest, DailyPaper, DailyRunKind, Run } from "../../types";

/**
 * The daily-discovery workbench tab: today's digest, the controls that start
 * a daily run, and the recommended papers with their reading decisions.
 *
 * Extracted from `App.tsx` without behaviour change.
 */
export function DailyPanel({
  digest,
  loading,
  action,
  searchDraft,
  runs,
  isSending,
  onSearchDraftChange,
  onStartDailyAction,
  onLoadDailyCenter,
  onUpdatePaper,
  onSelectRun,
}: {
  digest: DailyDigest;
  loading: boolean;
  action: DailyRunKind | null;
  searchDraft: string;
  runs: Run[];
  isSending: boolean;
  onSearchDraftChange: (value: string) => void;
  onStartDailyAction: (kind: DailyRunKind) => void;
  onLoadDailyCenter: () => void;
  onUpdatePaper: (paper: DailyPaper, status: Exclude<DailyPaper["status"], "new">) => void;
  onSelectRun: (runId: string) => void;
}) {
  return (
      <article className="utility-page" aria-label="每日检索中心">
        <header className="utility-page-heading">
          <div>
            <p className="eyebrow">文献发现</p>
            <h1>每日检索中心</h1>
            <p>{digest.progress || "管理今日文献并保留明确的阅读决策。"}</p>
          </div>
          <button className="button button-secondary" type="button" onClick={() => void onLoadDailyCenter()} disabled={loading || isSending}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </header>
        <div className="utility-page-content">
          <section className="daily-actions" aria-label="检索操作">
            <div className="daily-action-buttons">
              <button className="button button-primary" type="button" onClick={() => void onStartDailyAction("daily")} disabled={isSending}>
                {action === "daily" ? "检索中…" : "执行今日检索"}
              </button>
              <button className="button button-secondary" type="button" onClick={() => void onStartDailyAction("retry")} disabled={isSending}>
                {action === "retry" ? "重试中…" : "重试失败项"}
              </button>
              <button className="button button-secondary" type="button" onClick={() => void onStartDailyAction("resume")} disabled={isSending}>
                {action === "resume" ? "继续中…" : "继续未完成"}
              </button>
            </div>
            <form
              className="daily-search-form"
              onSubmit={(event) => {
                event.preventDefault();
                void onStartDailyAction("search");
              }}
            >
              <input
                value={searchDraft}
                onChange={(event) => onSearchDraftChange(event.target.value)}
                placeholder="临时检索关键词，例如：TSN scheduling"
                disabled={isSending}
              />
              <button className="button button-secondary" type="submit" disabled={isSending || !searchDraft.trim()}>
                {action === "search" ? "检索中…" : "临时检索"}
              </button>
            </form>
          </section>

          <section className="daily-section">
            <div className="section-heading">
              <div><h2>今日论文</h2><p>{digest.papers.length ? `共 ${digest.papers.length} 篇，可标记后续阅读。` : "尚无今日结果。"}</p></div>
            </div>
            {loading ? (
              <p className="empty-panel">正在读取今日结果…</p>
            ) : digest.papers.length ? (
              <div className="daily-paper-list">
                {digest.papers.map((paper) => (
                  <article className="daily-paper" key={`${paper.keyword}:${paper.title}`}>
                    <div className="daily-paper-main">
                      <p className="daily-paper-meta">{paper.keyword}{paper.source ? ` · ${paper.source}` : ""}</p>
                      {paper.url ? (
                        <a className="daily-paper-title" href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a>
                      ) : (
                        <strong className="daily-paper-title">{paper.title}</strong>
                      )}
                    </div>
                    <div className="daily-paper-actions">
                      <span className={`status daily-paper-status status-${paper.status}`}>{dailyPaperStatusLabel(paper.status)}</span>
                      <button type="button" onClick={() => void onUpdatePaper(paper, "want_read")} disabled={paper.status === "want_read"}>想读</button>
                      <button type="button" onClick={() => void onUpdatePaper(paper, "read")} disabled={paper.status === "read"}>已读</button>
                      <button type="button" onClick={() => void onUpdatePaper(paper, "skipped")} disabled={paper.status === "skipped"}>跳过</button>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty-panel">执行今日检索后，匹配论文会出现在这里。</p>
            )}
          </section>

          <section className="daily-section">
            <div className="section-heading">
              <div><h2>最近运行</h2><p>只保留状态和统计，不重复展示论文正文。</p></div>
            </div>
            {runs.length ? (
              <ol className="daily-run-list">
                {runs.map((run) => (
                  <li key={run.run_id}>
                    <span className={`status status-${run.status}`}>{statusLabel(run.status)}</span>
                    <button className="daily-run-open" type="button" onClick={() => void onSelectRun(run.run_id)} title={run.run_id}>
                      {run.created_at ? `查看 ${run.created_at.slice(0, 10)} 报告` : "查看检索报告"}
                    </button>
                    <span>{run.metrics.selected_count ?? 0} 篇入选 · {durationLabel(run.duration_ms) || "处理中"}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="empty-panel">尚无每日检索运行记录。</p>
            )}
          </section>
        </div>
      </article>
  );
}

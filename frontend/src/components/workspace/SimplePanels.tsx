import { Suspense, lazy } from "react";

import { statusLabel } from "../../lib/labels";
import type { DailyRunDetail } from "../../types";

const MarkdownContent = lazy(() => import("../../MarkdownContent"));

/**
 * The user-profile workbench tab.
 *
 * Extracted from `App.tsx` without behaviour change: the tab shows only the
 * stable preferences the agent has saved, never the conversation itself.
 */
export function ProfilePanel({ profile }: { profile: { content?: string } }) {
  return (
    <article className="document-page" aria-label="用户画像">
      <header className="document-page-heading">
        <div>
          <p className="eyebrow">跨会话偏好</p>
          <h1>用户画像</h1>
          <p className="document-page-meta">仅显示 Agent 已保存的稳定偏好，不包含完整对话内容。</p>
        </div>
      </header>
      <div className="document-page-content">
        {profile.content ? (
          <Suspense fallback={<p className="markdown-loading">{profile.content}</p>}>
            <MarkdownContent content={profile.content} />
          </Suspense>
        ) : <p>尚未形成用户画像。</p>}
      </div>
    </article>
  );
}

/**
 * One finished daily-discovery run: the brief, the quality warnings and the
 * recommended papers.
 *
 * Extracted from `App.tsx` without behaviour change.
 */
export function DailyRunPanel({ title, detail }: { title: string; detail: DailyRunDetail }) {
  return (
    <article className="document-page" aria-label={`每日检索报告：${title}`}>
      <header className="document-page-heading">
        <div>
          <p className="eyebrow">每日检索报告</p>
          <h1>{title}</h1>
          <p className="document-page-meta">
            <span className={`status status-${detail.status}`}>{statusLabel(detail.status)}</span>
            {" · "}{detail.run_id}
          </p>
        </div>
      </header>
      <div className="document-page-content daily-report-page">
        {detail.brief && (
          <section className="daily-report-brief">
            <h2>本次摘要</h2>
            <p>{detail.brief}</p>
          </section>
        )}
        {detail.warnings.length > 0 && (
          <section className="daily-report-warnings">
            <h2>质量提示</h2>
            <ul>{detail.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
          </section>
        )}
        <section className="daily-report-papers">
          <h2>论文推荐</h2>
          {detail.papers.length ? (
            <div className="daily-paper-list">
              {detail.papers.map((paper) => (
                <article className="daily-paper" key={`${paper.title}:${paper.url}`}>
                  <div className="daily-paper-main">
                    <p className="daily-paper-meta">
                      {paper.selected ? "已入选" : "候选"}
                      {paper.source ? ` · ${paper.source}` : ""}
                      {paper.year ? ` · ${paper.year}` : ""}
                      {paper.citation_count !== null ? ` · 引用 ${paper.citation_count}` : ""}
                    </p>
                    {paper.url ? (
                      <a className="daily-paper-title" href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a>
                    ) : (
                      <strong className="daily-paper-title">{paper.title}</strong>
                    )}
                    {paper.reason && <p className="daily-paper-reason">推荐理由：{paper.reason}</p>}
                    {paper.tags.length > 0 && <p className="daily-paper-tags">{paper.tags.map((tag) => <span key={tag}>{tag}</span>)}</p>}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <p>该任务尚未产生可展示的论文推荐。</p>
          )}
        </section>
      </div>
    </article>
  );
}

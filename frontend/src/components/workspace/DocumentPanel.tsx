import { Suspense, lazy, type FormEvent } from "react";

import { ledgerKindLabel, ledgerRelationLabel, ledgerStatusLabel } from "../../lib/labels";
import type { ResearchDocumentDetail } from "../../types";

const MarkdownContent = lazy(() => import("../../MarkdownContent"));

/**
 * The research-document workbench tab.
 *
 * Extracted from `App.tsx` without behaviour change.  The tab has two
 * distinct jobs, and both stay here: the document itself (read, version
 * history) and the rail that lets the user ask the agent to work *around*
 * the current document, plus the ledger of research judgements.
 */
export function DocumentPanel({
  document,
  activeSessionId,
  isSending,
  documentRequest,
  onDocumentRequestChange,
  onSubmitRequest,
  onDownload,
  onRemove,
}: {
  document: ResearchDocumentDetail;
  activeSessionId: string | null;
  isSending: boolean;
  documentRequest: string;
  onDocumentRequestChange: (value: string) => void;
  onSubmitRequest: (event: FormEvent<HTMLFormElement>, document: ResearchDocumentDetail) => void;
  onDownload: (document: ResearchDocumentDetail) => void;
  onRemove: (document: ResearchDocumentDetail) => void;
}) {
  return (
    <article className="document-page document-reading" aria-label={`科研档案：${document.title}`}>
      <header className="document-page-heading">
        <div>
          <p className="eyebrow">科研档案</p>
          <h1>{document.title}</h1>
          <p className="document-page-meta">
            修订 {document.revision} · 更新于 {document.updated_at || document.created_at} · 四章节模板
          </p>
        </div>
        <div className="document-page-actions">
          <button className="button button-secondary" type="button" onClick={() => onDownload(document)}>
            下载 Word
          </button>
          <button className="button button-danger" type="button" onClick={() => onRemove(document)}>
            删除档案
          </button>
        </div>
      </header>
      <nav className="document-outline-strip" aria-label="科研档案章节目录">
        {document.sections.map((section) => (
          <span className="document-outline-chip" key={section.section_id} title={section.summary || "待补充"}>
            <strong>{section.heading}</strong>
            <span>{section.content_length} 字符</span>
          </span>
        ))}
      </nav>
      <div className="document-body">
        <div className="document-main">
          <div className="document-page-content">
            <Suspense fallback={<p className="markdown-loading">{document.content}</p>}>
              <MarkdownContent content={document.content} />
            </Suspense>
          </div>
          {document.versions.length > 1 && (
            <section className="document-version-history" aria-label="版本历史">
              <h2>版本历史</h2>
              <p>当前内容及每个写入前的完整快照均可由 Agent 在你确认后恢复。</p>
              <ul>
                {document.versions.map((version) => (
                  <li key={`${version.revision}-${version.updated_at}`}>
                    第 {version.revision} 版 · {version.current ? "当前版本" : "可恢复快照"} · {version.updated_at}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
        <aside className="document-rail" aria-label="档案工具与索引">
          <section className="document-request" aria-label="围绕当前科研档案提问">
            <header className="document-request-heading">
              <h2>直接描述你想做什么</h2>
              <p>例如“补充研究方案”“和新导入论文比对”“梳理证据与假设”或“审查创新性”。</p>
            </header>
            <form onSubmit={(event) => onSubmitRequest(event, document)}>
              <textarea
                value={documentRequest}
                onChange={(event) => onDocumentRequestChange(event.target.value)}
                placeholder="例如：结合《论文标题》补充研究方案，或梳理证据与假设…"
                rows={3}
                disabled={isSending || !activeSessionId}
              />
              <div>
                <span>仅关联相关章节（不塞入全文），写入前保留完整快照。</span>
                <button className="button button-primary" type="submit" disabled={!documentRequest.trim() || isSending || !activeSessionId}>
                  发送给 Agent
                </button>
              </div>
            </form>
            {isSending && (
              <p className="document-request-status" role="status">Agent 正在处理你的请求，可在「运行检查器」或会话中查看进度。</p>
            )}
          </section>
          <section className="research-ledger" aria-label="研究判断索引">
            <header className="research-ledger-heading">
              <h2>研究判断索引</h2>
              <span>账本修订 {document.ledger.ledger_revision}</span>
            </header>
            <p className="research-ledger-summary">
              {document.ledger.summary.items} 项判断 · 已支持 {document.ledger.summary.supported} · 存在争议 {document.ledger.summary.contested} · 待复核 {document.ledger.summary.stale_links}
            </p>
            {document.ledger.items.length ? (
              <div className="research-ledger-list">
                {document.ledger.items.map((item) => (
                  <article className={`research-ledger-item ${item.section_current ? "" : "stale"}`} key={item.item_id}>
                    <div className="research-ledger-item-meta">
                      <span>{item.heading}</span>
                      <span>{ledgerKindLabel(item.kind)} · {ledgerStatusLabel(item.status)}</span>
                      {!item.section_current && <span className="ledger-stale">正文已改动，需复核</span>}
                    </div>
                    <strong>{item.statement}</strong>
                    {item.falsification && <p>可证伪条件：{item.falsification}</p>}
                    {item.evidence.length > 0 && (
                      <ul>
                        {item.evidence.map((evidence, index) => (
                          <li key={`${item.item_id}-${index}`}>
                            {ledgerRelationLabel(evidence.relation)}：{evidence.title || evidence.paper_id || "未命名论文"}
                            {evidence.page ? ` · 第 ${evidence.page} 页` : ""}
                            {evidence.chunk_index !== null ? ` · 切块 ${evidence.chunk_index}` : ""}
                            {evidence.note ? ` · ${evidence.note}` : ""}
                          </li>
                        ))}
                      </ul>
                    )}
                  </article>
                ))}
              </div>
            ) : (
              <p className="research-ledger-empty">尚未记录研究判断。可在上方直接要求梳理证据与假设；Agent 会先提出条目，只有你确认后才会写入。</p>
            )}
          </section>
        </aside>
      </div>
    </article>
  );
}

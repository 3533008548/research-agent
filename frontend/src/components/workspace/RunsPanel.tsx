import type { Dispatch, SetStateAction } from "react";

import { RunCenterList } from "../runcenter/RunCenterList";
import { RunDetailPanel } from "../runcenter/RunDetailPanel";
import { badcaseCategoryLabel } from "../../lib/labels";
import type { BadcaseCandidate, BadcaseCategory, Run, SessionUsage } from "../../types";

/**
 * The run-centre workbench tab: recent runs, the selected run's detail, and
 * the badcase triage form.
 *
 * Extracted from `App.tsx` without behaviour change.  The badcase form only
 * ever stores a category, a run id and an optional redacted note — never the
 * conversation, the papers or the model output — and that constraint is
 * stated in the form's own copy so it stays visible to whoever reads it.
 */
export function RunsPanel({
  apiKey,
  runs,
  loading,
  selectedRun,
  selectedRunId,
  sessionUsage,
  badcases,
  badcaseCategories,
  badcaseSubmitting,
  manualBadcaseRunId,
  manualBadcaseCategory,
  manualBadcaseNote,
  onSelectRunForTimeline,
  onSelectedRunIdChange,
  onMarkRunAsBadcase,
  onMarkManualBadcase,
  onBadcaseCategoriesChange,
  onManualBadcaseRunIdChange,
  onManualBadcaseCategoryChange,
  onManualBadcaseNoteChange,
  onLoadRunCenter,
}: {
  apiKey: string;
  runs: Run[];
  loading: boolean;
  selectedRun: Run | null;
  selectedRunId: string | null;
  sessionUsage: SessionUsage | null;
  badcases: BadcaseCandidate[];
  badcaseCategories: Record<string, BadcaseCategory>;
  badcaseSubmitting: string | null;
  manualBadcaseRunId: string;
  manualBadcaseCategory: BadcaseCategory;
  manualBadcaseNote: string;
  onSelectRunForTimeline: (run: Run) => void;
  onSelectedRunIdChange: (runId: string | null) => void;
  onMarkRunAsBadcase: (run: Run) => void;
  onMarkManualBadcase: () => void;
  onBadcaseCategoriesChange: Dispatch<SetStateAction<Record<string, BadcaseCategory>>>;
  onManualBadcaseRunIdChange: (value: string) => void;
  onManualBadcaseCategoryChange: (value: BadcaseCategory) => void;
  onManualBadcaseNoteChange: (value: string) => void;
  onLoadRunCenter: () => void;
}) {
  return (
      <article className="utility-page" aria-label="运行中心">
        <header className="utility-page-heading">
          <div>
            <p className="eyebrow">可追溯运行</p>
            <h1>运行中心</h1>
            <p>查看当前会话及每日检索的近期任务；问题运行可记录为不含内容的 Badcase 候选。</p>
          </div>
          <button className="button button-secondary" type="button" onClick={() => void onLoadRunCenter()} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </header>
        <div className="utility-page-content">
          <section className="token-summary" aria-label="当前会话 Token 统计">
            <div className="section-heading">
              <div><h2>当前会话 Token</h2><p>来自持久化的对话与深度研究统计；每日检索暂不计入。</p></div>
            </div>
            {sessionUsage ? (
              <div className="token-summary-grid">
                <div><span>输入</span><strong>{sessionUsage.prompt.toLocaleString()}</strong></div>
                <div><span>输出</span><strong>{sessionUsage.completion.toLocaleString()}</strong></div>
                <div><span>合计</span><strong>{sessionUsage.total.toLocaleString()}</strong></div>
                <div><span>模型调用</span><strong>{sessionUsage.calls.toLocaleString()}</strong></div>
              </div>
            ) : (
              <p className="empty-panel">选择一个会话后显示其 Token 统计。</p>
            )}
          </section>
          <section className="daily-section run-center-section">
            <div className="section-heading">
              <div><h2>近期运行</h2><p>当前会话与每日检索各保留最近 20 条；点击任意运行查看其阶段时间轴。</p></div>
            </div>
            <RunCenterList
              runs={runs}
              loading={loading}
              selectedRunId={selectedRunId}
              badcaseCategories={badcaseCategories}
              badcaseSubmitting={badcaseSubmitting}
              onSelect={onSelectRunForTimeline}
              onBadcaseCategoryChange={(runId, category) => onBadcaseCategoriesChange((current) => ({
                ...current,
                [runId]: category,
              }))}
              onMarkBadcase={(run) => void onMarkRunAsBadcase(run)}
            />
          </section>

          <RunDetailPanel
            apiKey={apiKey}
            run={selectedRun}
            onClose={() => onSelectedRunIdChange(null)}
          />

          <section className="daily-section">
            <div className="section-heading">
              <div><h2>Badcase 候选</h2><p>只保存分类、运行状态和匿名运行指标，不保存对话、论文或模型正文。</p></div>
            </div>
            {badcases.length ? (
              <ol className="badcase-list">
                {badcases.map((candidate) => (
                  <li key={candidate.candidate_id}>
                    <span className="status">{badcaseCategoryLabel(candidate.category)}</span>
                    <strong title={candidate.run_id}>{candidate.run_id}</strong>
                    <span>{candidate.status === "triage" ? "待整理" : candidate.status === "promoted" ? "已转为评测" : "已忽略"} · {candidate.occurrence_count} 次</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="empty-panel">还没有标记的问题运行。</p>
            )}
            <form
              className="manual-badcase-form"
              onSubmit={(event) => {
                event.preventDefault();
                void onMarkManualBadcase();
              }}
            >
              <div>
                <h3>手动标记</h3>
                <p>可粘贴运行卡片中的 run_id；备注只写脱敏现象，不要写入原始问题、回答、论文内容或密钥。</p>
              </div>
              <input
                value={manualBadcaseRunId}
                onChange={(event) => onManualBadcaseRunIdChange(event.target.value)}
                placeholder="run_id，例如 chat-… 或 research-…"
                aria-label="要标记的运行 ID"
              />
              <select
                value={manualBadcaseCategory}
                onChange={(event) => onManualBadcaseCategoryChange(event.target.value as BadcaseCategory)}
                aria-label="Badcase 分类"
              >
                {(["retrieval_miss", "citation_quality", "answer_quality", "tool_failure", "performance", "safety", "other"] as BadcaseCategory[]).map((category) => (
                  <option key={category} value={category}>{badcaseCategoryLabel(category)}</option>
                ))}
              </select>
              <textarea
                value={manualBadcaseNote}
                onChange={(event) => onManualBadcaseNoteChange(event.target.value)}
                maxLength={500}
                placeholder="可选脱敏备注（最多 500 字）"
                aria-label="脱敏备注"
              />
              <button
                className="button button-secondary"
                type="submit"
                disabled={!manualBadcaseRunId.trim() || badcaseSubmitting === `manual:${manualBadcaseRunId.trim()}`}
              >
                {badcaseSubmitting === `manual:${manualBadcaseRunId.trim()}` ? "记录中…" : "添加候选"}
              </button>
            </form>
          </section>
        </div>
      </article>
  );
}

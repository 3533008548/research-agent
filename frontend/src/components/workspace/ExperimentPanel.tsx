import {
  codeSourceLabel,
  type ExperimentCodeSource,
  type MethodReconstruction,
  type RepositoryPreparation,
  type RepositoryRelationship,
} from "../../lib/labels";
import type { ExperimentFileContent, ExperimentProjectDetail } from "../../types";

/**
 * The paper-reproduction workbench tab.
 *
 * Extracted from `App.tsx` without behaviour change.  The panel is the
 * read-out of one reproduction project: where its code came from, what the
 * upstream repository looked like, what the paper's method decomposes into,
 * which facts are still unknown, and the files that were produced.
 *
 * The handler props are grouped rather than flattened: the panel is a
 * read-out, and a flat list of seventeen callbacks would hide which action
 * does what.
 */
export function ExperimentPanel({
  project,
  source,
  preparation,
  reconstruction,
  starting,
  selectedFile,
  repositoryUrlDraft,
  repositoryRelationship,
  onRepositoryUrlDraftChange,
  onRepositoryRelationshipChange,
  onRefresh,
  onRetry,
  onRemove,
  onSearchRepositories,
  onConnectRepository,
  onPrepareRepository,
  onStartMethodReconstruction,
  onPrepareConfirmation,
  onOpenFile,
}: {
  project: ExperimentProjectDetail;
  source: ExperimentCodeSource | null;
  preparation: RepositoryPreparation | null;
  reconstruction: MethodReconstruction | null;
  starting: string | null;
  selectedFile: ExperimentFileContent | null;
  repositoryUrlDraft: string;
  repositoryRelationship: Exclude<RepositoryRelationship, "candidate_unverified">;
  onRepositoryUrlDraftChange: (value: string) => void;
  onRepositoryRelationshipChange: (value: Exclude<RepositoryRelationship, "candidate_unverified">) => void;
  onRefresh: (projectId: string) => void;
  onRetry: (project: ExperimentProjectDetail) => void;
  onRemove: (project: ExperimentProjectDetail) => void;
  onSearchRepositories: (project: ExperimentProjectDetail) => void;
  onConnectRepository: (
    project: ExperimentProjectDetail,
    url: string,
    relationship: Exclude<RepositoryRelationship, "candidate_unverified"> | "candidate_unverified",
  ) => void;
  onPrepareRepository: (project: ExperimentProjectDetail) => void;
  onStartMethodReconstruction: (project: ExperimentProjectDetail) => void;
  onPrepareConfirmation: (project: ExperimentProjectDetail) => void;
  onOpenFile: (projectId: string, path: string) => void;
}) {
  return (
          <article className="document-page experiment-page" aria-label={`论文复现实验：${project.paper_title}`}>
            <header className="document-page-heading">
              <div>
                <p className="eyebrow">论文代码复现</p>
                <h1>{project.paper_title}</h1>
                <p className="document-page-meta">
                  <span className={`status status-${project.status}`}>{project.status}</span>
                  {" · "}{project.reproduction_level === "exact" ? "精确复现规格" : project.reproduction_level === "approximate" ? "近似复现规格" : "待确认规格"}
                  {" · 修订 "}{project.revision}
                </p>
              </div>
              <div className="experiment-page-actions">
                <button className="button button-secondary" type="button" onClick={() => void onRefresh(project.project_id)}>
                  刷新项目
                </button>
                {project.status !== "ready" && (
                  <button
                    className="button button-primary"
                    type="button"
                    onClick={() => void onRetry(project)}
                    disabled={Boolean(starting)}
                  >
                    {starting === `retry:${project.project_id}` ? "重试中…" : "重试生成"}
                  </button>
                )}
                <button
                  className="button button-danger"
                  type="button"
                  onClick={() => void onRemove(project)}
                  disabled={["queued", "running"].includes(project.status)}
                  title={["queued", "running"].includes(project.status) ? "任务执行中，请先等待完成" : "删除项目及其文件"}
                >
                  删除项目
                </button>
              </div>
            </header>
            <div className="document-page-content experiment-page-content">
              <section className="experiment-summary">
                <h2>当前状态</h2>
                <p>{project.summary || "正在等待任务生成实验规格。"}</p>
                <p className="experiment-note">首版只生成证据驱动的规格、代码与静态校验，不会下载数据、执行训练或宣称论文指标已复现。</p>
              </section>
              <section className="experiment-source">
                <div className="experiment-source-heading">
                  <div>
                    <h2>代码来源</h2>
                    <p>{source ? codeSourceLabel(source) : "尚未检查"}</p>
                  </div>
                  <div className="experiment-source-actions">
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void onSearchRepositories(project)}
                      disabled={Boolean(starting)}
                    >
                      {starting === `source:${project.project_id}` ? "检索中…" : "查找 GitHub 候选"}
                    </button>
                    <button
                      className="button button-secondary"
                      type="button"
                      onClick={() => void onStartMethodReconstruction(project)}
                      disabled={Boolean(starting)}
                    >
                      {starting === `method:${project.project_id}` ? "还原中…" : "按论文方法还原"}
                    </button>
                  </div>
                </div>
                {source?.mode === "repository" && source.repository?.repository_url ? (
                  <div className="experiment-repository-connected">
                    <a href={source.repository.repository_url} target="_blank" rel="noreferrer">
                      {source.repository.full_name || source.repository.repository_url}
                    </a>
                    <span>{source.repository.commit_sha ? `commit ${source.repository.commit_sha.slice(0, 12)}` : "未取得 commit"}</span>
                    <button
                      className="button button-primary"
                      type="button"
                      onClick={() => void onPrepareRepository(project)}
                      disabled={Boolean(starting) || !source.repository.commit_sha}
                    >
                      {starting === `prepare:${project.project_id}` ? "准备中…" : "同步并分析上游代码"}
                    </button>
                    <p>会克隆固定 commit 并解析 README、依赖文件与训练入口；不会安装依赖、下载数据或执行代码。</p>
                  </div>
                ) : source?.mode === "method_reconstruction" ? (
                  <p className="experiment-note">当前产物是论文方法还原，不是原作者代码复现；关键缺失信息会继续显示在待确认项中。</p>
                ) : (
                  <p className="experiment-note">可按论文标题检索候选，或直接粘贴一个 GitHub 仓库地址。检索结果只代表候选，不会自动标为官方实现。</p>
                )}
                {Array.isArray(source?.candidates) && source.candidates.length > 0 && (
                  <div className="experiment-repository-candidates">
                    {source.candidates.map((candidate) => (
                      <div key={candidate.repository_url || candidate.full_name} className="experiment-repository-candidate">
                        <div>
                          <a href={candidate.repository_url} target="_blank" rel="noreferrer">{candidate.full_name || candidate.repository_url}</a>
                          <p>{candidate.description || "GitHub 未提供简介"}{typeof candidate.stars === "number" ? ` · ★ ${candidate.stars}` : ""}</p>
                        </div>
                        <button
                          className="button button-secondary"
                          type="button"
                          onClick={() => void onConnectRepository(project, candidate.repository_url || "", "candidate_unverified")}
                          disabled={Boolean(starting) || !candidate.repository_url}
                        >
                          使用此候选
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {preparation && (
                  <div className="experiment-repository-plan">
                    <div>
                      <strong>{preparation.status === "failed" ? "上游仓库准备失败" : "上游仓库已准备"}</strong>
                      <span>{preparation.local_path || "repository/source"}{preparation.commit_sha ? ` · ${preparation.commit_sha.slice(0, 12)}` : ""}</span>
                    </div>
                    <p>依赖声明：{preparation.dependency_files?.join("、") || "未找到"}</p>
                    <p>训练/评测入口候选：{preparation.entrypoint_candidates?.join("、") || "未找到"}</p>
                    {preparation.has_submodules && <p>检测到 submodule，尚未初始化。</p>}
                    {preparation.scan_truncated && <p>仓库文件较多，静态清单已按上限截断。</p>}
                    {preparation.status === "failed" && <p>最近错误：{preparation.error || "未知错误"}</p>}
                    <p className="experiment-note">下一阶段需先确认数据集位置、执行入口与资源预算，再显式安装依赖和运行。</p>
                  </div>
                )}
                {reconstruction && (
                  <div className="experiment-method-plan">
                    <div>
                      <strong>论文方法还原参考实现</strong>
                      <span>{reconstruction.source_ref_count || 0} 条页面级证据定位</span>
                    </div>
                    <p>{reconstruction.method_summary || "尚未可靠提取方法概述，请先查看待确认项。"}</p>
                    {reconstruction.components?.length ? (
                      <ul>
                        {reconstruction.components.map((component, index) => (
                          <li key={`${component.name || "component"}-${index}`}>
                            <strong>{component.name || "未命名组件"}</strong>
                            {component.responsibility ? `：${component.responsibility}` : ""}
                            {component.evidence_ids?.length ? <span>（证据：{component.evidence_ids.join("、")}）</span> : <span>（未定位到直接证据）</span>}
                          </li>
                        ))}
                      </ul>
                    ) : <p>未能可靠拆出方法组件，已保留可编辑骨架。</p>}
                    {reconstruction.assumptions?.length ? <p>工程假设：{reconstruction.assumptions.join("；")}</p> : null}
                    <p className="experiment-note">这里只完成源代码草案和静态检查。补齐数据、参数或实现事实后，可通过“在对话中确认”写回项目。</p>
                  </div>
                )}
                <form
                  className="experiment-repository-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void onConnectRepository(project, repositoryUrlDraft, repositoryRelationship);
                  }}
                >
                  <input
                    value={repositoryUrlDraft}
                    onChange={(event) => onRepositoryUrlDraftChange(event.target.value)}
                    placeholder="https://github.com/owner/repo"
                    aria-label="GitHub 仓库地址"
                    disabled={Boolean(starting)}
                  />
                  <select
                    value={repositoryRelationship}
                    onChange={(event) => onRepositoryRelationshipChange(event.target.value as Exclude<RepositoryRelationship, "candidate_unverified">)}
                    aria-label="仓库来源性质"
                    disabled={Boolean(starting)}
                  >
                    <option value="user_provided">用户提供，待核验</option>
                    <option value="official_confirmed">我已确认是官方实现</option>
                    <option value="community">社区实现</option>
                  </select>
                  <button className="button button-primary" type="submit" disabled={Boolean(starting) || !repositoryUrlDraft.trim()}>
                    连接仓库
                  </button>
                </form>
              </section>
              <section className="experiment-grid">
                <div>
                  <h2>待确认项</h2>
                  {Array.isArray(project.spec.unknowns) && project.spec.unknowns.length ? (
                    <ul>{project.spec.unknowns.map((item, index) => <li key={`${String(item)}-${index}`}>{String(item)}</li>)}</ul>
                  ) : <p>没有记录待确认项。</p>}
                </div>
                <div>
                  <h2>验证结果</h2>
                  {project.validation.valid ? <p className="validation-ok">已通过代码静态验证。</p> : (
                    <ul className="validation-errors">
                      {(project.validation.errors || ["尚未完成静态验证。"]).map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
                    </ul>
                  )}
                </div>
              </section>
              <section className="experiment-confirmation">
                <div>
                  <h2>如何确认待确认项？</h2>
                  <p>点击右侧按钮后，会在当前会话的输入框中放入一段项目专属指令。补全数据集、参数、实现约束等已知事实，再发送给 Agent；它只会写入明确确认的信息。</p>
                </div>
                <button className="button button-primary" type="button" onClick={() => onPrepareConfirmation(project)}>
                  在对话中确认
                </button>
              </section>
              <section className="experiment-spec-section">
                <h2>论文证据范围</h2>
                <p>{Array.isArray(project.spec.source_refs) ? `已保留 ${project.spec.source_refs.length} 条页面级来源定位；具体参数和数据集仅在证据充分时写入规格。` : "尚未提取来源定位。"}</p>
              </section>
              <section className="experiment-files-section">
                <h2>项目文件</h2>
                {project.files.length ? (
                  <div className="experiment-file-layout">
                    <div className="experiment-file-list">
                      {project.files.map((file) => (
                        <button
                          key={file.path}
                          type="button"
                          className={selectedFile?.path === file.path ? "selected" : ""}
                          onClick={() => void onOpenFile(project.project_id, file.path)}
                        >
                          <strong>{file.path}</strong><span>{Math.max(1, Math.ceil(file.size_bytes / 1024))} KB</span>
                        </button>
                      ))}
                    </div>
                    <pre className="experiment-file-preview"><code>{selectedFile?.content || "选择一个文件查看内容。"}</code></pre>
                  </div>
                ) : <p>项目尚未生成代码文件。</p>}
              </section>
            </div>
          </article>
  );
}

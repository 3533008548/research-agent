import type {
  BadcaseCategory,
  DailyPaper,
  DailyRunDetail,
  ExperimentProjectDetail,
  Message,
  Run,
  RunEvent,
} from "../types";

/**
 * Pure presentation helpers.
 *
 * These used to live inside `App.tsx`, which made that file a 2500-line
 * monolith and, worse, made the label wording impossible to test on its own.
 * They are pure functions of their inputs with no React involvement, so they
 * belong next to the other pure logic in `src/lib`.
 */

export type ResearchMode = "chat" | "research";
export type DisplayMessage = Message & { id: string; streaming?: boolean };

export type RepositoryRelationship =
  | "user_provided"
  | "official_confirmed"
  | "community"
  | "candidate_unverified";

export type ExperimentRepository = {
  repository_url?: string;
  full_name?: string;
  description?: string;
  default_branch?: string;
  commit_sha?: string;
  stars?: number;
  is_fork?: boolean;
  is_archived?: boolean;
};

export type ExperimentCodeSource = {
  mode?: string;
  status?: string;
  candidate_status?: string;
  relationship?: RepositoryRelationship | string;
  repository?: ExperimentRepository;
  candidates?: ExperimentRepository[];
};

export type RepositoryPreparation = {
  local_path?: string;
  commit_sha?: string;
  sync_mode?: string;
  dependency_files?: string[];
  entrypoint_candidates?: string[];
  config_candidates?: string[];
  has_submodules?: boolean;
  scan_truncated?: boolean;
  status?: string;
  error?: string;
};

export type MethodReconstruction = {
  status?: string;
  basis?: string;
  method_summary?: string;
  components?: Array<{
    name?: string;
    responsibility?: string;
    implementation_hint?: string;
    evidence_ids?: string[];
  }>;
  assumptions?: string[];
  unresolved?: string[];
  source_ref_count?: number;
};

const TOOL_LABELS: Record<string, string> = {
  search_papers: "检索论文",
  read_pdf: "读取 PDF",
  describe_image: "理解图片",
  paper_card: "生成论文卡片",
  query_papers: "检索本地论文库",
  list_research_document_sections: "查看档案章节",
  read_research_document_section: "读取档案章节",
  apply_research_document_patch: "安全更新档案",
  compare_papers_to_research_document: "论文档案比对",
  read_research_document_ledger: "查看证据与假设账本",
  apply_research_document_ledger_patch: "更新证据与假设账本",
  review_research_document_innovation: "科研档案创新性审查",
  review_new_paper_impact_on_research_document: "新论文综合审查",
};

export function toolLabel(name: string | undefined): string {
  if (!name) {
    return "工具调用";
  }
  return TOOL_LABELS[name] || name;
}

export function statusLabel(status: string | undefined): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    cancelling: "正在取消",
    completed: "已完成",
    cancelled: "已取消",
    failed: "执行失败",
    partial_failed: "部分完成",
  };
  return labels[status || ""] || status || "等待开始";
}

export function stageLabel(stage: string | undefined, fallback: string): string {
  const labels: Record<string, string> = {
    steer_queued: "已收到运行中补充，等待下一节点使用",
    steer_consumed: "运行中补充已在下一节点使用",
  };
  return labels[stage || ""] || stage || fallback;
}

export function modeLabel(mode: ResearchMode): string {
  return mode === "research" ? "深度研究" : "对话";
}

export function runKindLabel(kind: Run["kind"]): string {
  return kind === "daily" ? "每日检索" : kind === "research" ? "深度研究" : kind === "experiment" ? "论文复现" : "对话";
}

export function dailyPaperStatusLabel(status: DailyPaper["status"]): string {
  return ({ new: "未处理", want_read: "想读", read: "已读", skipped: "已跳过" })[status];
}

export function badcaseCategoryLabel(category: BadcaseCategory): string {
  return ({
    retrieval_miss: "召回遗漏",
    citation_quality: "引用质量",
    answer_quality: "回答质量",
    tool_failure: "工具失败",
    performance: "性能",
    safety: "安全",
    other: "其他",
  })[category];
}

export function dailyRunTitle(detail: DailyRunDetail): string {
  const date = detail.created_at ? detail.created_at.slice(0, 10) : "未命名";
  return `每日检索 · ${date}`;
}

export function durationLabel(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "";
  }
  return value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${Math.round(value)} 毫秒`;
}

export function ledgerKindLabel(kind: string): string {
  return ({
    research_question: "研究问题",
    hypothesis: "假设",
    innovation_candidate: "创新候选",
    decision: "研究决策",
  } as Record<string, string>)[kind] || kind;
}

export function ledgerStatusLabel(status: string): string {
  return ({
    open: "待澄清",
    hypothesis: "假设",
    supported: "有证据支持",
    contested: "存在争议",
    rejected: "已否定",
    decision: "已确认决策",
  } as Record<string, string>)[status] || status;
}

export function ledgerRelationLabel(relation: string): string {
  return ({
    supports: "支持",
    contradicts: "冲突",
    conditions: "条件/边界",
    inspiration: "启发",
  } as Record<string, string>)[relation] || relation;
}

export function experimentCodeSource(project: ExperimentProjectDetail): ExperimentCodeSource {
  const source = project.spec.code_source;
  return source && typeof source === "object" && !Array.isArray(source)
    ? source as ExperimentCodeSource
    : { mode: "not_checked", status: "not_checked", candidates: [] };
}

export function codeSourceLabel(source: ExperimentCodeSource): string {
  if (source.mode === "repository") {
    return ({
      official_confirmed: "官方实现（已确认）",
      community: "社区实现",
      user_provided: "用户提供仓库（待核验）",
      candidate_unverified: "GitHub 候选（待核验）",
    } as Record<string, string>)[String(source.relationship || "")] || "已连接仓库";
  }
  if (source.mode === "method_reconstruction") return "按论文方法还原";
  if (source.mode === "repository_search") return source.status === "candidates_found" ? "已找到候选" : "未找到候选";
  return "尚未检查";
}

export function repositoryPreparation(project: ExperimentProjectDetail): RepositoryPreparation | null {
  const value = project.spec.repository_preparation;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RepositoryPreparation
    : null;
}

export function methodReconstruction(project: ExperimentProjectDetail): MethodReconstruction | null {
  const value = project.spec.method_reconstruction;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as MethodReconstruction
    : null;
}

export function displayMessages(messages: Message[]): DisplayMessage[] {
  return messages.map((message, index) => ({
    ...message,
    id: `${index}-${message.role}`,
  }));
}

export function appendRunEvent(events: RunEvent[], event: RunEvent): RunEvent[] {
  // Token events belong to the chat transcript, not the operational timeline.
  // Redis also emits an initial state and its queued copy, so adjacent identical
  // state transitions add no information to the inspector.
  if (event.type === "token") {
    return events;
  }
  const previous = events.at(-1);
  const isRepeatedStatus = event.type === "status"
    && previous?.type === "status"
    && previous.status === event.status
    && previous.stage === event.stage;
  const isRepeatedCompletion = event.type === "done"
    && previous?.type === "done"
    && previous.status === event.status;
  return isRepeatedStatus || isRepeatedCompletion ? events : [...events, event];
}

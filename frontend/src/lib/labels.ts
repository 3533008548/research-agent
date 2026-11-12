import type {
  BadcaseCategory,
  DailyPaper,
  DailyRunDetail,
  Message,
  Run,
  RunEvent,
} from "../types";
import type { RunTiming } from "./runTiming";

/**
 * Pure presentation helpers.
 *
 * These used to live inside `App.tsx`, which made that file a 2500-line
 * monolith and, worse, made the label wording impossible to test on its own.
 * They are pure functions of their inputs with no React involvement, so they
 * belong next to the other pure logic in `src/lib`.
 */

export type ResearchMode = "chat" | "research";
export type DisplayMessage = Message & { id: string; streaming?: boolean; timing?: RunTiming };

const TOOL_LABELS: Record<string, string> = {
  search_papers: "检索论文",
  read_pdf: "读取 PDF",
  describe_image: "理解图片",
  paper_card: "生成论文卡片",
  query_papers: "检索本地论文库",
  list_papers: "查看本地论文",
  list_indexed_papers: "查看已入库论文",
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
    paused: "已暂停",
    waiting: "等待材料",
    needs_input: "等待决定",
    cancelling: "正在取消",
    completed: "已完成",
    cancelled: "已取消",
    failed: "执行失败",
    partial_failed: "部分完成",
    streaming: "正在输出",
    recovering: "正在恢复",
    skipped: "已跳过",
    interrupted: "未收到结束记录",
    passed: "核验通过",
    advisory: "核验有提示",
    revision_requested: "定向修正中",
    unresolved: "仍有待确认问题",
    inconclusive: "核验未得出结论",
  };
  return labels[status || ""] || status || "等待开始";
}

export function stageLabel(stage: string | undefined, fallback: string): string {
  const labels: Record<string, string> = {
    model: "模型生成",
    verify: "事实核验",
    run: "本轮任务",
    planner: "规划研究",
    researchers: "收集证据",
    synthesis: "综合证据",
    critic: "检查证据与局限",
    revision: "修订报告",
    steer_queued: "已收到运行中补充，等待下一节点使用",
    session_wait: "等待本会话上一轮任务完成",
    capacity_wait: "等待任务执行名额",
    steer_consumed: "运行中补充已在下一节点使用",
  };
  return labels[stage || ""] || stage || fallback;
}

export function modeLabel(mode: ResearchMode): string {
  return mode === "research" ? "多 Agent 研究" : "对话";
}

export function runKindLabel(kind: Run["kind"]): string {
  return kind === "daily" ? "每日检索" : kind === "research" ? "深度研究" : "对话";
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

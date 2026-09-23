export type MessageRole = "user" | "assistant";

export interface Message {
  role: MessageRole;
  content: string;
}

export interface Session {
  thread_id: string;
  title: string;
  preview: string;
  created_at: string;
  updated_at: string;
}

export type RunKind = "chat" | "research" | "daily" | "experiment";
export type DailyRunKind = "daily" | "retry" | "search" | "resume";
export type DailyPaperStatus = "new" | "want_read" | "read" | "skipped";
export type ResearchScope = "both" | "local" | "public";
export type BadcaseCategory =
  | "retrieval_miss"
  | "citation_quality"
  | "answer_quality"
  | "tool_failure"
  | "performance"
  | "safety"
  | "other";

export interface RunStart {
  run_id: string;
  kind: RunKind;
  session_id: string | null;
  project_id: string | null;
  status: string;
  stream_url: string;
}

export interface RunEvent {
  type: "status" | "token" | "tool" | "done" | "error";
  status?: string;
  stage?: string;
  tool?: string;
  duration_ms?: number;
  text?: string;
  answer?: string;
  error_type?: string;
}

export interface RunSteer {
  steer_id: number;
  run_id: string;
  status: "pending" | "consumed";
  message: string;
  consumed_stage: string;
  created_at: string;
  consumed_at: string | null;
}

export interface Run {
  run_id: string;
  kind: RunKind;
  session_id: string | null;
  project_id: string | null;
  status: string;
  model: string;
  answer: string;
  duration_ms: number | null;
  metrics: Record<string, number>;
  error_type: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  events: Array<Record<string, unknown>>;
  steers: RunSteer[];
}

export interface ResearchDocument {
  document_id: string;
  title: string;
  summary: string;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface ResearchDocumentSection {
  section_id: "background" | "content_innovation" | "approach_feasibility" | "outlook_plan";
  heading: string;
  content_length: number;
  content_hash: string;
  summary: string;
}

export interface ResearchDocumentVersion {
  revision: number;
  updated_at: string;
  current: boolean;
}

export interface ResearchLedgerEvidence {
  paper_id: string;
  title: string;
  page: number | null;
  chunk_index: number | null;
  relation: "supports" | "contradicts" | "conditions" | "inspiration";
  note: string;
}

export interface ResearchLedgerItem {
  item_id: string;
  section_id: ResearchDocumentSection["section_id"];
  heading: string;
  section_hash: string;
  section_current: boolean;
  kind: "research_question" | "hypothesis" | "innovation_candidate" | "decision";
  status: "open" | "hypothesis" | "supported" | "contested" | "rejected" | "decision";
  statement: string;
  falsification: string;
  evidence: ResearchLedgerEvidence[];
  updated_at: string;
}

export interface ResearchLedgerSummary {
  items: number;
  supported: number;
  contested: number;
  stale_links: number;
}

export interface ResearchDocumentLedger {
  document_id: string;
  title: string;
  document_revision: number;
  ledger_revision: number;
  updated_at: string;
  items: ResearchLedgerItem[];
  summary: ResearchLedgerSummary;
}

export interface ResearchDocumentRoute {
  document_id: string;
  intent: "new_paper_impact_review" | "safe_patch" | "paper_comparison" | "ledger" | "innovation_review" | "consult";
  label: string;
  message: string;
}

export interface ResearchDocumentDetail extends ResearchDocument {
  content: string;
  download_url: string;
  sections: ResearchDocumentSection[];
  versions: ResearchDocumentVersion[];
  ledger: ResearchDocumentLedger;
}

export interface ExperimentFile {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface ExperimentProject {
  project_id: string;
  paper_id: string;
  paper_title: string;
  status: string;
  reproduction_level: string;
  implementation_path: string;
  summary: string;
  revision: number;
  latest_run_id: string;
  created_at: string;
  updated_at: string;
}

export interface ExperimentProjectDetail extends ExperimentProject {
  spec: Record<string, unknown>;
  validation: {
    valid?: boolean;
    checks?: Array<{ name?: string; passed?: boolean }>;
    errors?: string[];
  };
  files: ExperimentFile[];
}

export interface ExperimentFileContent {
  path: string;
  content: string;
  sha256: string;
}

export interface Paper {
  paper_id: string;
  title: string;
  chunks: number;
  indexed_at: string;
  relation_count: number;
}

export interface WorkspaceUpload {
  upload_id: string;
  filename: string;
  kind: "pdf" | "image";
  size_bytes: number;
  duplicate: boolean;
}

export interface DailyKeyword {
  keyword: string;
  active: boolean;
  added_at: string;
  search_status: string;
}

export interface DailyPaper {
  keyword: string;
  title: string;
  url: string;
  source: string;
  status: DailyPaperStatus;
  searched_at: string;
}

export interface DailyDigest {
  progress: string;
  papers: DailyPaper[];
}

export interface DailyRunPaper {
  title: string;
  url: string;
  source: string;
  year: number | null;
  citation_count: number | null;
  reason: string;
  tags: string[];
  selected: boolean;
}

export interface DailyRunDetail {
  run_id: string;
  kind: string;
  status: string;
  created_at: string;
  updated_at: string;
  brief: string;
  warnings: string[];
  papers: DailyRunPaper[];
}

export interface SessionUsage {
  prompt: number;
  completion: number;
  total: number;
  calls: number;
  context_limit: number;
}

export interface UserProfile {
  content: string;
}

export interface BadcaseCandidate {
  candidate_id: string;
  run_id: string;
  category: BadcaseCategory;
  source: string;
  status: "triage" | "promoted" | "dismissed";
  fingerprint: string;
  occurrence_count: number;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceSettings {
  model: string;
  rag_enabled: boolean;
  pdf_max_pages: number;
  daily_search_enabled: boolean;
  restart_required: boolean;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

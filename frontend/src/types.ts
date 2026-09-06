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

export type RunKind = "chat" | "research" | "daily";

export interface RunStart {
  run_id: string;
  kind: RunKind;
  session_id: string | null;
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

export interface Run {
  run_id: string;
  kind: RunKind;
  session_id: string | null;
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
}

export interface ResearchDocument {
  document_id: string;
  title: string;
  summary: string;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface ResearchDocumentDetail extends ResearchDocument {
  content: string;
  download_url: string;
}

export interface Paper {
  paper_id: string;
  title: string;
  chunks: number;
  indexed_at: string;
}

export interface DailyKeyword {
  keyword: string;
  active: boolean;
  added_at: string;
  search_status: string;
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

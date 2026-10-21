import type {
  DailyRunDetail,
  ExperimentProjectDetail,
  ResearchDocumentDetail,
  Run,
  UserProfile,
} from "../types";

/**
 * The shapes that describe the workbench's own state.
 *
 * They live apart from the components so that a hook can accept them without
 * importing a component, and so the workbench's vocabulary has one home.
 */

export type ResearchMode = "chat" | "research";

export type WorkspacePanel =
  | "documents"
  | "profile"
  | "papers"
  | "experiments"
  | "keywords"
  | "settings";

export type Notice = { kind: "error" | "info"; text: string } | null;

export type WorkbenchTab =
  | { id: "chat"; kind: "chat"; title: string }
  | { id: string; kind: "document"; title: string; document: ResearchDocumentDetail }
  | { id: "profile"; kind: "profile"; title: string; profile: UserProfile }
  | { id: "daily"; kind: "daily"; title: string }
  | { id: string; kind: "dailyRun"; title: string; detail: DailyRunDetail }
  | { id: string; kind: "experiment"; title: string; project: ExperimentProjectDetail }
  | { id: "runs"; kind: "runs"; title: string };

export type RunKind = Run["kind"];

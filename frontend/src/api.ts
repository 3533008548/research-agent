import {
  ApiError,
  type BadcaseCandidate,
  type BadcaseCategory,
  type DailyKeyword,
  type DailyDigest,
  type DailyRunDetail,
  type DailyPaperStatus,
  type DailyRunKind,
  type Message,
  type Paper,
  type Run,
  type RunEvent,
  type RunKind,
  type RunStart,
  type RunSteer,
  type ResearchScope,
  type ResearchProject,
  type Session,
  type SessionUsage,
  type UserProfile,
  type WorkspaceUpload,
  type WorkspaceSettings,
} from "./types";

const API_PREFIX = "/api/v1";

export function listResearchProjects(apiKey: string): Promise<Array<Pick<ResearchProject, "project_id" | "question">>> {
  return request("/research-projects", apiKey);
}

export function getResearchProject(apiKey: string, id: string): Promise<ResearchProject> {
  return request(`/research-projects/${encodeURIComponent(id)}`, apiKey);
}

export function getResearchReport(apiKey: string, id: string, reportId: string): Promise<{ version: number; snapshot: { report: string } }> {
  return request(`/research-projects/${encodeURIComponent(id)}/reports/${encodeURIComponent(reportId)}`, apiKey);
}

export function controlResearchProject(apiKey: string, id: string, action: string, message = ""): Promise<ResearchProject> {
  return request(`/research-projects/${encodeURIComponent(id)}/controls`, apiKey, {
    method: "POST", body: JSON.stringify({ action, message, event_id: crypto.randomUUID() }),
  });
}

function headers(apiKey: string, includeJson = false): Headers {
  const result = new Headers({ Accept: "application/json" });
  if (includeJson) {
    result.set("Content-Type", "application/json");
  }
  if (apiKey.trim()) {
    result.set("X-API-Key", apiKey.trim());
  }
  return result;
}

export async function request<T>(
  path: string,
  apiKey: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers: headers(apiKey, Boolean(init.body)),
  });
  if (!response.ok) {
    let detail = "请求失败";
    try {
      const data = (await response.json()) as { detail?: string };
      detail = data.detail || detail;
    } catch {
      // Non-JSON gateway errors still have a useful status below.
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

export function listSessions(apiKey: string): Promise<Session[]> {
  return request("/sessions", apiKey);
}

export function createSession(apiKey: string, title?: string): Promise<Session> {
  return request("/sessions", apiKey, {
    method: "POST",
    body: JSON.stringify({ title: title?.trim() || undefined }),
  });
}

export function deleteSession(apiKey: string, sessionId: string): Promise<void> {
  return request(`/sessions/${encodeURIComponent(sessionId)}`, apiKey, { method: "DELETE" });
}

export function getSessionMessages(apiKey: string, sessionId: string): Promise<Message[]> {
  return request(`/sessions/${encodeURIComponent(sessionId)}/messages`, apiKey);
}

export function getSessionUsage(apiKey: string, sessionId: string): Promise<SessionUsage> {
  return request(`/sessions/${encodeURIComponent(sessionId)}/usage`, apiKey);
}

export function getWorkspaceProfile(apiKey: string): Promise<UserProfile> {
  return request("/workspace/profile", apiKey);
}

export function listSessionRuns(apiKey: string, sessionId: string): Promise<Run[]> {
  return request(`/sessions/${encodeURIComponent(sessionId)}/runs`, apiKey);
}

export async function uploadWorkspaceFile(apiKey: string, file: File): Promise<WorkspaceUpload> {
  const body = new FormData();
  body.append("file", file, file.name);
  const response = await fetch(`${API_PREFIX}/workspace/uploads`, {
    method: "POST",
    headers: headers(apiKey),
    body,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: "文件上传失败" }));
    throw new ApiError(data.detail || "文件上传失败", response.status);
  }
  return response.json() as Promise<WorkspaceUpload>;
}

export function listPapers(apiKey: string): Promise<Paper[]> {
  return request("/workspace/papers", apiKey);
}

export function listDailyKeywords(apiKey: string): Promise<DailyKeyword[]> {
  return request("/workspace/daily-keywords", apiKey);
}

export function createDailyKeyword(apiKey: string, keyword: string): Promise<DailyKeyword> {
  return request("/workspace/daily-keywords", apiKey, {
    method: "POST",
    body: JSON.stringify({ keyword }),
  });
}

export function deleteDailyKeyword(apiKey: string, keyword: string): Promise<void> {
  return request(`/workspace/daily-keywords/${encodeURIComponent(keyword)}`, apiKey, { method: "DELETE" });
}

export function getDailyDigest(apiKey: string): Promise<DailyDigest> {
  return request("/workspace/daily-digest", apiKey);
}

export function updateDailyPaperStatus(
  apiKey: string,
  keyword: string,
  title: string,
  status: Exclude<DailyPaperStatus, "new">,
): Promise<void> {
  return request("/workspace/daily-papers/status", apiKey, {
    method: "PUT",
    body: JSON.stringify({ keyword, title, status }),
  });
}

export function listDailyRuns(apiKey: string): Promise<Run[]> {
  return request("/workspace/daily-runs", apiKey);
}

export function getDailyRunDetail(apiKey: string, runId: string): Promise<DailyRunDetail> {
  return request(`/workspace/daily-runs/${encodeURIComponent(runId)}`, apiKey);
}

export function listBadcases(apiKey: string): Promise<BadcaseCandidate[]> {
  return request("/workspace/badcases", apiKey);
}

export function createBadcase(
  apiKey: string,
  runId: string,
  category: BadcaseCategory,
  note = "",
): Promise<BadcaseCandidate> {
  return request(`/runs/${encodeURIComponent(runId)}/badcases`, apiKey, {
    method: "POST",
    body: JSON.stringify({ category, note: note.trim() || undefined }),
  });
}

export function getWorkspaceSettings(apiKey: string): Promise<WorkspaceSettings> {
  return request("/workspace/settings", apiKey);
}

export function updateWorkspaceSettings(
  apiKey: string,
  settings: Omit<WorkspaceSettings, "restart_required">,
): Promise<WorkspaceSettings> {
  return request("/workspace/settings", apiKey, {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

export function getRun(apiKey: string, runId: string): Promise<Run> {
  return request(`/runs/${encodeURIComponent(runId)}`, apiKey);
}

export function createRun(
  apiKey: string,
  kind: Extract<RunKind, "chat" | "research">,
  sessionId: string,
  content: string,
  uploadId?: string,
  researchScope: ResearchScope = "both",
  resume = false,
): Promise<RunStart> {
  return request("/runs", apiKey, {
    method: "POST",
    body: JSON.stringify(
      kind === "chat"
        ? { kind, session_id: sessionId, message: content || undefined, upload_id: uploadId }
        : {
          kind, session_id: sessionId, query: content || undefined,
          scope: researchScope, resume: resume || undefined,
        },
    ),
  });
}

export function createDailyRun(
  apiKey: string,
  dailyKind: DailyRunKind,
  keyword?: string,
): Promise<RunStart> {
  return request("/runs", apiKey, {
    method: "POST",
    body: JSON.stringify({
      kind: "daily",
      daily_kind: dailyKind,
      keyword: dailyKind === "search" ? keyword?.trim() || undefined : undefined,
    }),
  });
}

export function cancelRun(apiKey: string, runId: string): Promise<{ run_id: string; status: string }> {
  return request(`/runs/${encodeURIComponent(runId)}/cancel`, apiKey, { method: "POST" });
}

export function createRunSteer(apiKey: string, runId: string, message: string): Promise<RunSteer> {
  return request(`/runs/${encodeURIComponent(runId)}/steers`, apiKey, {
    method: "POST",
    body: JSON.stringify({ message: message.trim() }),
  });
}

function parseFrame(frame: string): RunEvent | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) {
    return null;
  }
  try {
    return JSON.parse(data) as RunEvent;
  } catch {
    return null;
  }
}

export async function streamRun(
  apiKey: string,
  streamUrl: string,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(streamUrl, {
    headers: headers(apiKey),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError("无法连接运行事件流", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      pending += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
      const frames = pending.split("\n\n");
      pending = frames.pop() || "";
      for (const frame of frames) {
        const event = parseFrame(frame);
        if (event) {
          onEvent(event);
        }
      }
      if (done) {
        const event = parseFrame(pending);
        if (event) {
          onEvent(event);
        }
        return;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

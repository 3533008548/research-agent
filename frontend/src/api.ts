import {
  ApiError,
  type DailyKeyword,
  type Message,
  type Paper,
  type ResearchDocument,
  type ResearchDocumentDetail,
  type Run,
  type RunEvent,
  type RunKind,
  type RunStart,
  type Session,
  type WorkspaceSettings,
} from "./types";

const API_PREFIX = "/api/v1";

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

async function request<T>(
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

export function listResearchDocuments(apiKey: string): Promise<ResearchDocument[]> {
  return request("/workspace/research-documents", apiKey);
}

export function getResearchDocument(apiKey: string, documentId: string): Promise<ResearchDocumentDetail> {
  return request(`/workspace/research-documents/${encodeURIComponent(documentId)}`, apiKey);
}

export async function downloadResearchDocument(apiKey: string, document: ResearchDocumentDetail): Promise<void> {
  const response = await fetch(document.download_url, { headers: headers(apiKey) });
  if (!response.ok) {
    throw new ApiError("无法下载研究档案", response.status);
  }
  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = window.document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = `${document.title}.docx`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
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
): Promise<RunStart> {
  return request("/runs", apiKey, {
    method: "POST",
    body: JSON.stringify(
      kind === "chat"
        ? { kind, session_id: sessionId, message: content }
        : { kind, session_id: sessionId, query: content, scope: "both" },
    ),
  });
}

export function cancelRun(apiKey: string, runId: string): Promise<{ run_id: string; status: string }> {
  return request(`/runs/${encodeURIComponent(runId)}/cancel`, apiKey, { method: "POST" });
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

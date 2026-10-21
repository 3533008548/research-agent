import { ApiError } from "../types";

/**
 * Turn an unknown thrown value into something a user can act on.
 *
 * The three cases are distinct on purpose: a missing token needs the user to
 * do something specific, an aborted stream is not a failure at all, and
 * anything else means the service is not reachable.
 */
export function userFacingError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status === 401
      ? "需要 API 令牌。请在左下角填入 API_AUTH_TOKEN 后重试。"
      : error.message;
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "事件流已停止。";
  }
  return "连接服务失败，请确认 FastAPI 服务正在运行。";
}

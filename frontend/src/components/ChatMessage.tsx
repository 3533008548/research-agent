import { lazy, Suspense } from "react";

import type { DisplayMessage } from "../lib/labels";
import { RunTimer } from "./RunTimer";

const MarkdownContent = lazy(() => import("../MarkdownContent"));

/**
 * One message in the transcript.
 *
 * Extracted from `App.tsx` without behaviour change.  The assistant's
 * markdown is loaded lazily so the transcript renders before the (large)
 * markdown bundle arrives.
 */
export function ChatMessage({ message }: { message: DisplayMessage }) {
  return (
    <article className={`message message-${message.role}`} aria-label={message.role === "user" ? "用户消息" : "助手回复"}>
      <div className="message-body">
        {message.role === "assistant" && message.timing && <RunTimer timing={message.timing} />}
        {message.content ? (
          <Suspense fallback={<p className="markdown-loading">{message.content}</p>}>
            <MarkdownContent content={message.content} />
          </Suspense>
        ) : (
          <p className="streaming-placeholder">正在生成回答<span aria-hidden="true">…</span></p>
        )}
      </div>
    </article>
  );
}

import { lazy, Suspense } from "react";

import type { DisplayMessage } from "../lib/labels";

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
    <article className={`message message-${message.role}`}>
      <div className="message-avatar" aria-hidden="true">
        {message.role === "user" ? "你" : "研"}
      </div>
      <div className="message-body">
        <p className="message-role">{message.role === "user" ? "你" : "Research Agent"}</p>
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
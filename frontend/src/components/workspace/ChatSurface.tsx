import { useLayoutEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Move one mounted conversation between panes without resetting its local state. */
export function ChatSurface({ target, scrollTop, children }: { target: HTMLDivElement | null; scrollTop: number; children: ReactNode }) {
  const [surface] = useState(() => {
    const element = document.createElement("div");
    element.className = "chat-surface";
    return element;
  });
  useLayoutEffect(() => {
    if (!target) return;
    const list = surface.querySelector(".message-list");
    const focused = surface.contains(document.activeElement) ? document.activeElement as HTMLElement : null;
    target.appendChild(surface);
    if (list) list.scrollTop = scrollTop;
    focused?.focus({ preventScroll: true });
  }, [target, surface, scrollTop]);
  useLayoutEffect(() => () => { surface.remove(); }, [surface]);
  return createPortal(children, surface);
}

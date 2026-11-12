import { useLayoutEffect, useRef, type RefObject } from "react";

/** Follow new content only while the reader stays at the bottom. */
export function useChatScroll(
  listRef: RefObject<HTMLDivElement | null>,
  threadRef: RefObject<HTMLDivElement | null>,
  sessionId: string | null,
  newestUserId: string | undefined,
  mode: string,
) {
  const following = useRef(true);
  const lastTop = useRef(0);

  useLayoutEffect(() => {
    const list = listRef.current;
    const thread = threadRef.current;
    if (!list || !thread) return;
    const scroll = () => {
      const top = list.scrollTop;
      if (top < lastTop.current - 1) following.current = false;
      else if (list.scrollHeight - list.clientHeight - top <= 24) following.current = true;
      lastTop.current = top;
    };
    const wheel = (event: WheelEvent) => {
      if (event.deltaY < 0) following.current = false;
    };
    const key = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest("input, textarea, select, [contenteditable=true]")) return;
      if (["ArrowUp", "PageUp", "Home"].includes(event.key)) following.current = false;
    };
    const follow = () => {
      if (following.current) {
        list.scrollTop = list.scrollHeight;
        lastTop.current = list.scrollTop;
      }
    };
    // Covers streamed Markdown, lazy rendering, images and changes in pane width.
    const observer = new ResizeObserver(follow);
    observer.observe(thread);
    observer.observe(list);
    list.addEventListener("scroll", scroll, { passive: true });
    list.addEventListener("wheel", wheel, { passive: true });
    list.addEventListener("keydown", key);
    return () => {
      observer.disconnect();
      list.removeEventListener("scroll", scroll);
      list.removeEventListener("wheel", wheel);
      list.removeEventListener("keydown", key);
    };
  }, [listRef, threadRef]);

  useLayoutEffect(() => {
    following.current = mode === "chat";
    const list = listRef.current;
    if (list && following.current) {
      list.scrollTop = list.scrollHeight;
      lastTop.current = list.scrollTop;
    }
  }, [listRef, sessionId, newestUserId, mode]);
}

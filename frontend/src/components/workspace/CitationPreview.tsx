import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { libraryRequest, parsePaperLocation } from "../../lib/library";

type Excerpt = { name: string; text: string; image: string };

export function CitationPreview({ list, apiKey, sessionId }: {
  list: RefObject<HTMLDivElement | null>; apiKey: string; sessionId: string | null;
}) {
  const [anchor, setAnchor] = useState<HTMLAnchorElement | null>(null);
  const [excerpt, setExcerpt] = useState<Excerpt | null>(null);
  const [error, setError] = useState(false);
  const popup = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = list.current;
    if (!container) return;
    let timer = 0;
    const close = () => { window.clearTimeout(timer); setAnchor(null); };
    function enter(event: Event) {
      const link = (event.target as Element).closest<HTMLAnchorElement>('.message-assistant a[href^="#paper-ref/"]');
      if (!link || !parsePaperLocation(link.hash)) return;
      window.clearTimeout(timer);
      setAnchor(link);
    }
    function leave(event: MouseEvent | FocusEvent) {
      const next = event.relatedTarget;
      if (next instanceof Node && popup.current?.contains(next)) return;
      if (event.target instanceof Element && event.target.closest('.message-assistant a[href^="#paper-ref/"]')) {
        timer = window.setTimeout(close, 160);
      }
    }
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    container.addEventListener("mouseover", enter);
    container.addEventListener("mouseout", leave);
    container.addEventListener("focusin", enter);
    container.addEventListener("focusout", leave);
    container.addEventListener("click", close);
    container.addEventListener("scroll", close);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", escape);
    return () => {
      close();
      container.removeEventListener("mouseover", enter);
      container.removeEventListener("mouseout", leave);
      container.removeEventListener("focusin", enter);
      container.removeEventListener("focusout", leave);
      container.removeEventListener("click", close);
      container.removeEventListener("scroll", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", escape);
    };
  }, [list, sessionId]);

  useEffect(() => {
    if (!anchor) return;
    const location = parsePaperLocation(anchor.hash);
    if (!location) return;
    const abort = new AbortController();
    setExcerpt(null);
    setError(false);
    anchor.setAttribute("aria-describedby", "paper-citation-preview");
    const timer = window.setTimeout(async () => {
      try {
        const data = await libraryRequest<Excerpt>(apiKey, "/citation-preview", {
          method: "POST", signal: abort.signal, body: JSON.stringify({ ...location, kind: "area" }),
        });
        if (!abort.signal.aborted) setExcerpt(data);
      } catch {
        if (!abort.signal.aborted) setError(true);
      }
    }, 120);
    return () => {
      abort.abort();
      window.clearTimeout(timer);
      anchor.removeAttribute("aria-describedby");
    };
  }, [anchor, apiKey]);
  if (!anchor || !anchor.isConnected) return null;
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(420, window.innerWidth - 20);
  const above = rect.top > window.innerHeight - rect.bottom;
  const room = above ? rect.top : window.innerHeight - rect.bottom;
  return createPortal(<div ref={popup} role="tooltip" id="paper-citation-preview" className="citation-preview"
    onMouseLeave={() => setAnchor(null)}
    style={{ width, left: Math.max(10, Math.min(rect.left, window.innerWidth - width - 10)),
      maxHeight: Math.min(350, room - 10),
      ...(above ? { bottom: window.innerHeight - rect.top } : { top: rect.bottom }) }}>
    {excerpt ? <>
      <strong>{excerpt.name} · 第 {parsePaperLocation(anchor.hash)?.page} 页</strong>
      <img src={excerpt.image} alt={excerpt.text || "论文原页选区"} />
    </> : error ? "无法读取这处原文，请检查论文是否仍在库中。" : "正在读取原文…"}
  </div>, document.body);
}

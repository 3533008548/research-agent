import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import {
  getDocument,
  GlobalWorkerOptions,
  TextLayer,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "pdfjs-dist/web/pdf_viewer.css";
import {
  libraryRequest,
  patchDocument,
  type LibraryDocument,
  type NoteAnchor,
  type PaperNote,
} from "../../lib/library";
import { userFacingError } from "../../lib/errors";
GlobalWorkerOptions.workerSrc = workerUrl;

type Draft = {
  id: string;
  content: string;
  anchor: NoteAnchor | null;
  revision: number;
};
type Props = {
  apiKey: string;
  document: LibraryDocument;
  active: boolean;
  toolsHost: HTMLElement | null;
  onShowTools: () => void;
  onProgress: (id: string, progress: LibraryDocument["progress"]) => void;
};
export function PaperReader({
  apiKey,
  document: doc,
  active,
  toolsHost,
  onShowTools,
  onProgress,
}: Props) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null),
    [page, setPage] = useState(doc.progress.page || 1),
    [scale, setScale] = useState(doc.progress.scale || 1),
    [fit, setFit] = useState(doc.progress.fit || "width");
  const [size, setSize] = useState({ width: 600, height: 800 }),
    [available, setAvailable] = useState({ width: 600, height: 600 }),
    [error, setError] = useState(""),
    [rendering, setRendering] = useState(false),
    [scanned, setScanned] = useState(false);
  const [pan, setPan] = useState(false),
    [space, setSpace] = useState(false),
    [anchor, setAnchor] = useState<NoteAnchor | null>(null),
    [side, setSide] = useState<"notes" | "translation">("notes");
  const [translation, setTranslation] = useState(""),
    [translationOk, setTranslationOk] = useState(false),
    [translating, setTranslating] = useState(false),
    [translatedAnchor, setTranslatedAnchor] = useState<NoteAnchor | null>(null);
  const [notes, setNotes] = useState<PaperNote[]>([]),
    [draft, setDraft] = useState<Draft | null>(null),
    [saveStatus, setSaveStatus] = useState(""),
    [find, setFind] = useState(""),
    [finding, setFinding] = useState(false);
  const viewport = useRef<HTMLDivElement>(null),
    sheet = useRef<HTMLDivElement>(null),
    canvas = useRef<HTMLCanvasElement>(null),
    textLayer = useRef<HTMLDivElement>(null);
  const draftRef = useRef<Draft | null>(null),
    dirty = useRef(false),
    saving = useRef<Promise<boolean> | null>(null),
    translateAbort = useRef<AbortController | null>(null),
    drag = useRef<{ x: number; y: number; left: number; top: number } | null>(
      null,
    );
  const draftKey = `research-agent.note-draft:${doc.id}`;
  const renderedSize = useRef(size),
    zoomAnchor = useRef<{
      x: number;
      y: number;
      clientX: number;
      clientY: number;
    } | null>(null);
  const restoreScroll = useRef<number | null>(doc.progress.scroll_top || 0);
  const [scrollTop, setScrollTop] = useState(doc.progress.scroll_top || 0);
  const effectiveScale =
    fit === "width"
      ? Math.max(0.25, Math.min(4, (available.width - 40) / size.width))
      : fit === "page"
        ? Math.max(
            0.25,
            Math.min(
              4,
              Math.min(
                (available.width - 40) / size.width,
                (available.height - 40) / size.height,
              ),
            ),
          )
        : scale;

  function setEditor(value: Draft | null, isDirty = false) {
    draftRef.current = value;
    dirty.current = isDirty;
    setDraft(value);
    if (isDirty && value) {
      try {
        localStorage.setItem(draftKey, JSON.stringify(value));
      } catch {
        setSaveStatus("浏览器草稿空间不足，请立即保存");
      }
    }
  }
  async function flush(): Promise<boolean> {
    if (saving.current) return saving.current;
    const job = (async () => {
      while (dirty.current && draftRef.current) {
        const snapshot = { ...draftRef.current };
        setSaveStatus("保存中…");
        try {
          const saved = await libraryRequest<PaperNote>(
            apiKey,
            `/documents/${doc.id}/notes/${snapshot.id}`,
            { method: "PUT", body: JSON.stringify(snapshot) },
          );
          setNotes((current) => [
            saved,
            ...current.filter((n) => n.id !== saved.id),
          ]);
          const latest = draftRef.current;
          if (latest?.id === snapshot.id) {
            const unchanged =
              latest.content === snapshot.content &&
              JSON.stringify(latest.anchor) === JSON.stringify(snapshot.anchor);
            setEditor({ ...latest, revision: saved.revision }, !unchanged);
            if (unchanged) {
              localStorage.removeItem(draftKey);
              setSaveStatus("已保存");
            }
          }
        } catch (e) {
          setSaveStatus(
            `保存失败：${userFacingError(e)}。草稿已保留，可点击重试。`,
          );
          return false;
        }
      }
      return true;
    })();
    saving.current = job;
    try {
      return await job;
    } finally {
      saving.current = null;
    }
  }
  async function newNote(value: NoteAnchor | null = null, content = "") {
    onShowTools();
    if (!(await flush())) return;
    setEditor(
      { id: crypto.randomUUID(), content, anchor: value, revision: 0 },
      Boolean(value || content),
    );
    setSide("notes");
    setSaveStatus(value || content ? "待保存" : "新笔记");
  }

  useEffect(() => {
    let alive = true;
    const task = getDocument({
      url: `/api/v1/library/documents/${doc.id}/file`,
      httpHeaders: apiKey ? { "X-API-Key": apiKey } : {},
      cMapUrl: "/pdfjs/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "/pdfjs/standard_fonts/",
      wasmUrl: "/pdfjs/wasm/",
    });
    task.promise
      .then((value) => {
        if (alive) setPdf(value);
      })
      .catch((e) => {
        if (alive) setError(userFacingError(e));
      });
    return () => {
      alive = false;
      void task.destroy();
    };
  }, [apiKey, doc.id]);
  useEffect(() => {
    let alive = true;
    void libraryRequest<PaperNote[]>(apiKey, `/documents/${doc.id}/notes`)
      .then((value) => {
        if (!alive) return;
        setNotes(value);
        try {
          const local = localStorage.getItem(draftKey);
          if (local) {
            const restored = JSON.parse(local) as Draft;
            if (
              typeof restored.content === "string" &&
              typeof restored.id === "string"
            ) {
              setEditor(restored, true);
              setSaveStatus("已恢复未保存草稿");
            }
          }
        } catch {
          setSaveStatus("无法恢复草稿，请检查本地存储");
        }
      })
      .catch((e) => setError(userFacingError(e)));
    return () => {
      alive = false;
      void flush();
      translateAbort.current?.abort();
    };
  }, [apiKey, doc.id]);
  useEffect(() => {
    if (!dirty.current) return;
    const timer = setTimeout(() => void flush(), 700);
    return () => clearTimeout(timer);
  }, [draft]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (dirty.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, []);
  useEffect(() => {
    if (!active) {
      translateAbort.current?.abort();
      void flush();
      return;
    }
    const el = viewport.current;
    if (!el) return;
    const observer = new ResizeObserver(() =>
      setAvailable({ width: el.clientWidth, height: el.clientHeight }),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [active]);
  useEffect(() => {
    if (!pdf || !active) return;
    let cancelled = false;
    let task: RenderTask | undefined;
    let layer: TextLayer | undefined;
    setRendering(true);
    setError("");
    void (async () => {
      try {
        const p = await pdf.getPage(page);
        if (cancelled) return;
        const base = p.getViewport({ scale: 1 });
        if (
          Math.abs(base.width - size.width) > 1 ||
          Math.abs(base.height - size.height) > 1
        ) {
          setSize({ width: base.width, height: base.height });
          return;
        }
        const view = p.getViewport({ scale: effectiveScale }),
          pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
        const c = window.document.createElement("canvas");
        c.width = Math.floor(view.width * pixelRatio);
        c.height = Math.floor(view.height * pixelRatio);
        task = p.render({
          canvas: c,
          viewport: view,
          transform:
            pixelRatio === 1 ? undefined : [pixelRatio, 0, 0, pixelRatio, 0, 0],
        });
        await task.promise;
        if (cancelled) return;
        const target = canvas.current;
        if (!target || !textLayer.current) return;
        target.width = c.width;
        target.height = c.height;
        target.getContext("2d")!.drawImage(c, 0, 0);
        textLayer.current.replaceChildren();
        const content = await p.getTextContent();
        if (cancelled) return;
        setScanned(!content.items.length);
        layer = new TextLayer({
          textContentSource: content,
          container: textLayer.current,
          viewport: view,
        });
        await layer.render();
        if (cancelled) return;
        renderedSize.current = { width: view.width, height: view.height };
        const za = zoomAnchor.current;
        if (za && viewport.current) {
          const el = viewport.current;
          const bounds = el.getBoundingClientRect();
          const paperBounds = sheet.current!.getBoundingClientRect();
          el.scrollLeft += paperBounds.left - bounds.left + za.x * view.width - za.clientX;
          el.scrollTop += paperBounds.top - bounds.top + za.y * view.height - za.clientY;
          zoomAnchor.current = null;
        } else if (restoreScroll.current !== null && viewport.current) {
          viewport.current.scrollTop = restoreScroll.current;
          restoreScroll.current = null;
        }
        setRendering(false);
      } catch (e) {
        if (!cancelled) {
          setError(userFacingError(e));
          setRendering(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
      layer?.cancel();
    };
  }, [pdf, page, effectiveScale, size.width, size.height, active]);
  useEffect(() => {
    if (!active || !pdf) return;
    const timer = setTimeout(() => {
      const progress = {
        page,
        scale: effectiveScale,
        fit,
        scroll_top: viewport.current?.scrollTop || 0,
      };
      void patchDocument(apiKey, doc.id, { progress })
        .then(() => onProgress(doc.id, progress))
        .catch((e) => setError("阅读位置未保存：" + userFacingError(e)));
    }, 500);
    return () => clearTimeout(timer);
  }, [page, effectiveScale, fit, active, pdf, scrollTop]);
  function zoom(value: number, cx?: number, cy?: number) {
    const el = viewport.current;
    if (el) {
      const bounds = el.getBoundingClientRect();
      const paperBounds = sheet.current!.getBoundingClientRect();
      const x = cx === undefined ? el.clientWidth / 2 : cx - bounds.left,
        y = cy === undefined ? el.clientHeight / 2 : cy - bounds.top;
      zoomAnchor.current = {
        x: (bounds.left + x - paperBounds.left) / renderedSize.current.width,
        y: (bounds.top + y - paperBounds.top) / renderedSize.current.height,
        clientX: x,
        clientY: y,
      };
    }
    setFit("custom");
    setScale(Math.min(4, Math.max(0.25, value)));
  }
  useEffect(() => {
    const el = viewport.current;
    if (!el || !active) return;
    const wheel = (e: WheelEvent) => {
      if (e.ctrlKey) {
        e.preventDefault();
        zoom(
          effectiveScale * Math.exp(-e.deltaY * 0.002),
          e.clientX,
          e.clientY,
        );
      }
    };
    el.addEventListener("wheel", wheel, { passive: false });
    const down = (e: KeyboardEvent) => {
      if (
        e.code === "Space" &&
        el.matches(":hover") &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(
          (e.target as HTMLElement).tagName,
        )
      ) {
        e.preventDefault();
        setSpace(true);
      }
    };
    const up = () => {
      setSpace(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", up);
    return () => {
      el.removeEventListener("wheel", wheel);
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", up);
    };
  }, [active, effectiveScale]);
  function selection() {
    if (pan || space || rendering) return;
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !sheet.current) return;
    const range = selected.getRangeAt(0);
    if (!textLayer.current?.contains(range.commonAncestorContainer)) return;
    const text = selected.toString().trim();
    if (!text) return;
    const box = sheet.current.getBoundingClientRect();
    const rects = Array.from(range.getClientRects())
      .filter((r) => r.width > 1 && r.height > 1)
      .map((r) => ({
        x: Math.max(0, (r.left - box.left) / box.width),
        y: Math.max(0, (r.top - box.top) / box.height),
        width: Math.min(1, r.width / box.width),
        height: Math.min(1, r.height / box.height),
      }));
    translateAbort.current?.abort();
    setAnchor({
      page,
      quote: text.slice(0, 12000),
      rects: rects.slice(0, 300),
    });
  }
  async function translate() {
    if (!anchor) return;
    onShowTools();
    const selected = anchor;
    translateAbort.current?.abort();
    const abort = new AbortController();
    translateAbort.current = abort;
    setSide("translation");
    setTranslating(true);
    setTranslationOk(false);
    setTranslation("");
    setTranslatedAnchor(selected);
    try {
      const result = await libraryRequest<{ text: string }>(
        apiKey,
        `/documents/${doc.id}/translate`,
        {
          method: "POST",
          body: JSON.stringify({ text: selected.quote, page: selected.page }),
          signal: abort.signal,
        },
      );
      if (!abort.signal.aborted) {
        setTranslation(result.text);
        setTranslationOk(true);
      }
    } catch (e) {
      if (!abort.signal.aborted) setTranslation(userFacingError(e));
    } finally {
      if (translateAbort.current === abort) setTranslating(false);
    }
  }
  async function search() {
    if (!pdf || !find.trim()) return;
    setFinding(true);
    try {
      for (let i = 1; i <= pdf.numPages; i++) {
        const content = await (await pdf.getPage(i)).getTextContent();
        if (
          content.items
            .map((item) => ("str" in item ? item.str : ""))
            .join(" ")
            .toLowerCase()
            .includes(find.toLowerCase())
        ) {
          setPage(i);
          setError("");
          return;
        }
      }
      setError("没有找到匹配文字；扫描页面需先进行 OCR。");
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setFinding(false);
    }
  }
  return (
    <section
      className="paper-reader"
      hidden={!active}
      aria-label={`阅读 ${doc.name}`}
    >
      <div className="reader-toolbar">
        <button
          disabled={page <= 1}
          onClick={() => {
            setPage((p) => p - 1);
            setAnchor(null);
          }}
        >
          上一页
        </button>
        <label>
          <input
            aria-label="页码"
            type="number"
            min={1}
            max={doc.pages}
            value={page}
            onChange={(e) => {
              setPage(
                Math.min(doc.pages, Math.max(1, Number(e.target.value) || 1)),
              );
              setAnchor(null);
            }}
          />{" "}
          / {doc.pages}
        </label>
        <button
          disabled={page >= doc.pages}
          onClick={() => {
            setPage((p) => p + 1);
            setAnchor(null);
          }}
        >
          下一页
        </button>
        <span className="toolbar-divider" />
        <button aria-label="缩小" onClick={() => zoom(effectiveScale - 0.1)}>
          −
        </button>
        <label>
          <input
            aria-label="缩放比例"
            type="number"
            min={25}
            max={400}
            value={Math.round(effectiveScale * 100)}
            onChange={(e) => zoom((Number(e.target.value) || 100) / 100)}
          />
          %
        </label>
        <button aria-label="放大" onClick={() => zoom(effectiveScale + 0.1)}>
          ＋
        </button>
        <button aria-pressed={fit === "width"} onClick={() => setFit("width")}>
          适应宽度
        </button>
        <button aria-pressed={fit === "page"} onClick={() => setFit("page")}>
          整页
        </button>
        <button aria-pressed={pan} onClick={() => setPan((p) => !p)}>
          {pan ? "手形拖动" : "选择文字"}
        </button>
      </div>
      <form
        className="reader-search"
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <input
          aria-label="在论文中查找"
          value={find}
          onChange={(e) => setFind(e.target.value)}
          placeholder="查找论文文字"
        />
        <button disabled={finding}>{finding ? "查找中…" : "查找"}</button>
        <button type="button" onClick={() => void newNote()}>
          随手记
        </button>
      </form>
      {error && (
        <p role="alert" className="notice notice-error">
          {error}
        </p>
      )}
      {scanned && (
        <p className="notice">
          此页未检测到文本层，可阅读和记整页笔记；划选翻译需要可识别的文字。
        </p>
      )}
      <div
        ref={viewport}
        onScroll={e => setScrollTop(e.currentTarget.scrollTop)}
        className={`pdf-viewport ${pan || space ? "hand-mode" : ""}`}
        onPointerDown={(e) => {
          if (!(pan || space) || e.button !== 0) return;
          e.preventDefault();
          const el = e.currentTarget;
          drag.current = {
            x: e.clientX,
            y: e.clientY,
            left: el.scrollLeft,
            top: el.scrollTop,
          };
          el.setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          if (drag.current) {
            e.currentTarget.scrollLeft =
              drag.current.left - (e.clientX - drag.current.x);
            e.currentTarget.scrollTop =
              drag.current.top - (e.clientY - drag.current.y);
          }
        }}
        onPointerUp={() => {
          drag.current = null;
          selection();
        }}
        onPointerCancel={() => {
          drag.current = null;
        }}
        onKeyUp={selection}
      >
        <div
          ref={sheet}
          className="pdf-sheet"
          style={
            {
              width: size.width * effectiveScale,
              height: size.height * effectiveScale,
              "--total-scale-factor": effectiveScale,
              "--scale-factor": effectiveScale,
            } as CSSProperties
          }
        >
          <canvas ref={canvas} style={{ width: "100%", height: "100%" }} />
          <div className="textLayer" ref={textLayer} />
          <div className="highlight-layer">
            {notes
              .filter((n) => n.anchor?.page === page)
              .flatMap((n) =>
                n.anchor!.rects.map((r, i) => (
                  <span
                    key={n.id + ":" + i}
                    style={{
                      left: r.x * 100 + "%",
                      top: r.y * 100 + "%",
                      width: r.width * 100 + "%",
                      height: r.height * 100 + "%",
                    }}
                  />
                )),
              )}
          </div>
        </div>
      </div>
      <div className="reader-selection">
        <span>
          {rendering
            ? "正在渲染…"
            : anchor
              ? `第 ${anchor.page} 页 · 已选 ${anchor.quote.length} 字`
              : "划选文字后可翻译或添加批注"}
        </span>
        <button
          disabled={!anchor || translating}
          onClick={() => void translate()}
        >
          翻译选段
        </button>
        <button disabled={!anchor} onClick={() => void newNote(anchor)}>
          添加批注
        </button>
      </div>
      {active &&
        toolsHost &&
        createPortal(
          <div className="reader-tools">
            <div className="settings-tabs">
              <button
                aria-pressed={side === "notes"}
                onClick={() => setSide("notes")}
              >
                笔记
              </button>
              <button
                aria-pressed={side === "translation"}
                onClick={() => setSide("translation")}
              >
                翻译
              </button>
            </div>
            {side === "translation" ? (
              <>
                <h2>选段翻译</h2>
                {translatedAnchor && (
                  <blockquote>
                    第 {translatedAnchor.page} 页<br />
                    {translatedAnchor.quote}
                  </blockquote>
                )}
                <p className="translation-text">
                  {translating
                    ? "正在翻译…"
                    : translation || "划选论文中的文字后点击翻译。"}
                </p>
                {translating ? (
                  <button onClick={() => translateAbort.current?.abort()}>
                    取消翻译
                  </button>
                ) : (
                  translationOk && (
                    <button
                      onClick={() =>
                        void newNote(translatedAnchor, translation)
                      }
                    >
                      译文加入笔记
                    </button>
                  )
                )}
              </>
            ) : (
              <>
                <div className="action-row">
                  <h2>论文笔记</h2>
                  <button onClick={() => void newNote()}>新建</button>
                </div>
                {draft && (
                  <>
                    <p className="panel-hint">
                      {draft.anchor
                        ? `第 ${draft.anchor.page} 页批注`
                        : "整篇笔记"}
                    </p>
                    {draft.anchor && (
                      <blockquote>{draft.anchor.quote}</blockquote>
                    )}
                    <textarea
                      aria-label="笔记正文"
                      value={draft.content}
                      onChange={(e) => {
                        setEditor({ ...draft, content: e.target.value }, true);
                        setSaveStatus("待保存");
                      }}
                      placeholder="记录你的理解和问题…"
                    />
                    <p className="note-save-status" role="status">
                      {saveStatus}
                    </p>
                    <button onClick={() => void flush()}>保存笔记</button>
                    {saveStatus.startsWith("保存失败") && (
                      <button
                        onClick={() => {
                          if (draftRef.current) {
                            setEditor(
                              {
                                ...draftRef.current,
                                id: crypto.randomUUID(),
                                revision: 0,
                              },
                              true,
                            );
                            void flush();
                          }
                        }}
                      >
                        草稿另存为新笔记
                      </button>
                    )}
                  </>
                )}
                <div className="paper-notes">
                  {notes.map((n) => (
                    <article key={n.id}>
                      <button
                        className="note-open"
                        onClick={() =>
                          void (async () => {
                            if (!(await flush())) return;
                            setEditor(n);
                            setSaveStatus("已保存");
                            if (n.anchor) setPage(n.anchor.page);
                          })()
                        }
                      >
                        <strong>
                          {n.anchor ? `第 ${n.anchor.page} 页` : "整篇笔记"}
                        </strong>
                        <p>{n.content || n.anchor?.quote || "空笔记"}</p>
                      </button>
                      <button
                        className="text-button"
                        onClick={() =>
                          void (async () => {
                            if (!window.confirm("删除这条笔记？")) return;
                            if (!(await flush())) return;
                            try {
                              await libraryRequest(
                                apiKey,
                                `/documents/${doc.id}/notes/${n.id}`,
                                { method: "DELETE" },
                              );
                              setNotes((values) =>
                                values.filter((v) => v.id !== n.id),
                              );
                              if (draftRef.current?.id === n.id) {
                                setEditor(null);
                                localStorage.removeItem(draftKey);
                              }
                            } catch (e) {
                              setSaveStatus(userFacingError(e));
                            }
                          })()
                        }
                      >
                        删除
                      </button>
                    </article>
                  ))}
                </div>
                {!notes.length && !draft && (
                  <p className="panel-hint">
                    可以随手记录整篇笔记，也可以选择原文创建批注。
                  </p>
                )}
              </>
            )}
          </div>,
          toolsHost,
        )}
    </section>
  );
}

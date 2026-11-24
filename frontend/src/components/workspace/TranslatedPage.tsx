import { useEffect, useRef, useState } from "react";
import { getDocument, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist";
import { userFacingError } from "../../lib/errors";
import type { TranslationSegment } from "./usePaperTranslation";

type Props = {
  apiKey: string;
  documentId: string;
  page: number;
  complete: boolean;
  segments: TranslationSegment[];
  hovered: string;
  onHover: (id: string) => void;
};

export function TranslatedPage({apiKey, documentId, page, complete, segments, hovered, onHover}: Props) {
  const base = `/api/v1/library/documents/${documentId}/translation`;
  const completedOnPage = segments.filter(s => s.translation !== null).length;
  const url = complete ? base + "/file" : base + `/preview?page=${page}&done=${completedOnPage}`;
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(0);
  const [regions, setRegions] = useState<Map<string, TranslationSegment["bounds"][]>>(new Map());
  const [width, setWidth] = useState(0);
  const host = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    setPdf(null);
    setError("");
    setReady(0);
    const task = getDocument({url, httpHeaders: apiKey ? {"X-API-Key":apiKey} : {},
      cMapUrl:"/pdfjs/cmaps/", cMapPacked:true, standardFontDataUrl:"/pdfjs/standard_fonts/", wasmUrl:"/pdfjs/wasm/"});
    let alive = true;
    task.promise.then(value => { if (alive) setPdf(value); })
      .catch(e => { if (alive) setError(userFacingError(e)); });
    return () => { alive = false; void task.destroy(); };
  }, [url, apiKey]);

  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!pdf || !width) return;
    let alive = true, task: RenderTask | undefined;
    void pdf.getPage(complete ? page : 1).then(async p => {
      if (!alive) return;
      const buffer = document.createElement("canvas");
      const scale = width * Math.min(2, window.devicePixelRatio || 1) / p.getViewport({scale:1}).width;
      const viewport = p.getViewport({scale});
      buffer.width = Math.ceil(viewport.width);
      buffer.height = Math.ceil(viewport.height);
      task = p.render({canvas:buffer, viewport});
      const [, annotations] = await Promise.all([task.promise, p.getAnnotations({intent:"any"})]);
      if (!alive || !canvas.current) return;
      const next = new Map<string, TranslationSegment["bounds"][]>();
      for (const ann of annotations) {
        if (ann.titleObj?.str !== "translation-segment" || !ann.contentsObj?.str) continue;
        const id = ann.contentsObj.str;
        const [x,y] = viewport.convertToViewportPoint(ann.rect[0],ann.rect[1]);
        const [r,b] = viewport.convertToViewportPoint(ann.rect[2],ann.rect[3]);
        const bounds: TranslationSegment["bounds"] = [Math.min(x,r)/viewport.width,
          Math.min(y,b)/viewport.height, Math.max(x,r)/viewport.width, Math.max(y,b)/viewport.height];
        if (!next.has(id)) next.set(id, []);
        next.get(id)!.push(bounds);
      }
      setRegions(next);
      canvas.current.width = buffer.width;
      canvas.current.height = buffer.height;
      canvas.current.getContext("2d")!.drawImage(buffer,0,0);
      setReady(page);
    }).catch(e => { if (alive) setError(userFacingError(e)); });
    return () => { alive = false; task?.cancel(); };
  }, [pdf,page,width,complete]);

  async function openFile() {
    try {
      if (window.desktop) {
        await window.desktop.openTranslation(documentId);
      } else if (!apiKey) {
        window.open(url, "_blank", "noopener");
      } else if (pdf) {
        const tab = window.open();
        if (!tab) throw new Error("浏览器阻止了新标签页");
        tab.opener = null;
        try {
          const blob = new Blob([new Uint8Array(await pdf.getData())], {type:"application/pdf"});
          const address = URL.createObjectURL(blob);
          tab.location.replace(address);
          setTimeout(() => URL.revokeObjectURL(address), 60000);
        } catch (e) { tab.close(); throw e; }
      }
    } catch (e) { setError(userFacingError(e)); }
  }

  return <>
    {complete && <button className="text-button" disabled={!pdf} onClick={() => void openFile()}>独立打开 PDF ↗</button>}
    {error && <p role="alert">{error}</p>}
    <div className="translated-sheet" ref={host}>
      <canvas aria-label={`第 ${page} 页中文 PDF`} ref={canvas} style={{visibility:ready===page?"visible":"hidden"}}/>
      {ready===page && segments.flatMap(({id,bounds}) => (regions.get(id) ?? [bounds]).map(([x,y,r,b],index) =>
        <div key={id+"-"+index} id={index===0 ? documentId+id : undefined} data-segment={id} className={id===hovered?"paired":""}
          onPointerEnter={()=>onHover(id)} onPointerLeave={()=>onHover("")}
          style={{left:x*100+"%",top:y*100+"%",width:(r-x)*100+"%",height:(b-y)*100+"%"}}/>))}
    </div>
  </>;
}

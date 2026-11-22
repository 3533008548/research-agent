import { useCallback, useEffect, useState } from "react";
import { libraryRequest } from "../../lib/library";

type Notice = {
  document_id: string;
  name: string;
  state: string;
  error: string;
  updated_at: string;
};

type Props = { apiKey: string; onOpen: (id: string) => Promise<void> };

export function TranslationNotices({ apiKey, onOpen }: Props) {
  const [notice, setNotice] = useState<Notice>();
  const timestamp = notice?.updated_at;
  const id = notice?.document_id;

  const dismiss = useCallback(async (id: string, timestamp: string) => {
    try {
      const path = `/documents/${id}/translation-notice/ack?updated_at=${encodeURIComponent(timestamp)}`;
      await libraryRequest(apiKey, path, { method: "POST" });
      setNotice(current => current?.document_id === id && current.updated_at === timestamp ? undefined : current);
    } catch { /* A pending notification stays available when the backend restarts. */ }
  }, [apiKey]);

  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const rows = await libraryRequest<Notice[]>(apiKey, "/translation-notices", { signal: abort.signal });
        if (!abort.signal.aborted) setNotice(rows[0]);
      } catch { /* Poll again after a temporary backend failure. */ }
      if (!abort.signal.aborted) timer = setTimeout(poll, 4000);
    }
    void poll();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [apiKey]);

  useEffect(() => {
    if (notice?.state !== "complete" || !id || !timestamp) return;
    const timer = setTimeout(() => void dismiss(id, timestamp), 15000);
    return () => clearTimeout(timer);
  }, [dismiss, id, timestamp, notice?.state]);

  if (!notice) return null;
  const complete = notice.state === "complete";
  const close = () => void dismiss(notice.document_id, notice.updated_at);
  return <aside className="translation-toast" role="status">
    <strong>{complete ? "翻译完成" : notice.state === "unconfigured" ? "翻译等待模型配置" : "翻译中断"}</strong>
    <span>{notice.name}</span>
    {notice.error && <span>{notice.error}</span>}
    <div>
      <button onClick={() => { void onOpen(notice.document_id); close(); }}>{complete ? "打开译文" : "查看进度"}</button>
      <button aria-label="关闭翻译提醒" onClick={close}>关闭</button>
    </div>
  </aside>;
}

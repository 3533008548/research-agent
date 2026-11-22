import { useEffect, useState } from "react";
import { libraryRequest } from "../../lib/library";
import { userFacingError } from "../../lib/errors";

export type TranslationSegment = {
  id: string;
  bounds: [number, number, number, number];
  kind: string;
  source: string;
  translation: string | null;
};
type Snapshot = {
  page: number;
  state: "idle" | "queued" | "running" | "failed" | "complete" | "paused" | "unconfigured";
  done: number;
  total: number;
  remaining_seconds?: number | null;
  error: string;
  incomplete_pages?: number[];
  segments: TranslationSegment[];
};

export function usePaperTranslation(key: string, id: string, page: number, visible: boolean) {
  const path = `/documents/${id}/translation`;
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [version, refresh] = useState(0);
  useEffect(() => {
    if (!visible) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        const value = await libraryRequest<Snapshot>(key, path + "?page=" + page, {signal: abort.signal});
        if (abort.signal.aborted) return;
        setData(value);
        setError("");
        if (value.state !== "complete") timer = setTimeout(read, value.state === "running" ? 1000 : 3000);
      } catch (e) {
        if (!abort.signal.aborted) {
          setError(userFacingError(e));
          timer = setTimeout(read, 3000);
        }
      }
    }
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [key, path, page, visible, version]);

  async function update(method: "POST" | "DELETE") {
    setBusy(true);
    setError("");
    try {
      await libraryRequest(key, path, {method});
      refresh(v => v + 1);
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setBusy(false);
    }
  }
  return {data, error, busy, update, segments: data?.page === page ? data.segments : []};
}

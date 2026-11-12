import { useCallback, useEffect, useState } from "react";
import {
  libraryRequest,
  patchDocument,
  type LibraryData,
  type LibraryDocument,
} from "../lib/library";
import { userFacingError } from "../lib/errors";
export function useLibrary(apiKey: string) {
  const [data, setData] = useState<LibraryData>({ documents: [], folders: [] });
  const [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await libraryRequest<LibraryData>(apiKey));
      setError("");
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setLoading(false);
    }
  }, [apiKey]);
  useEffect(() => {
    void load();
  }, [load]);
  const update = useCallback(
    async (id: string, change: Partial<LibraryDocument>) => {
      const doc = await patchDocument(apiKey, id, change);
      setData((d) => ({
        ...d,
        documents: d.documents.map((p) => (p.id === id ? doc : p)),
      }));
      return doc;
    },
    [apiKey],
  );
  const rememberProgress = useCallback(
    (id: string, progress: LibraryDocument["progress"]) =>
      setData((d) => ({
        ...d,
        documents: d.documents.map((p) =>
          p.id === id ? { ...p, progress } : p,
        ),
      })),
    [],
  );
  return { data, loading, error, load, update, rememberProgress };
}

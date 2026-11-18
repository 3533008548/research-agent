import { request } from "../api";
export type ReadingProgress = {
  page?: number;
  scale?: number;
  fit?: "width" | "page" | "custom";
  scroll_top?: number;
};
export type LibraryDocument = {
  id: string;
  name: string;
  sha256: string;
  pages: number;
  size_bytes: number;
  folder_id: string | null;
  trashed: boolean;
  created_at: string;
  progress: ReadingProgress;
};
export type LibraryFolder = {
  id: string;
  name: string;
  parent_id: string | null;
};
export type LibraryData = {
  documents: LibraryDocument[];
  folders: LibraryFolder[];
  import_errors?: { name: string; error: string }[];
};
export type NoteAnchor = {
  page: number;
  quote: string;
  rects: { x: number; y: number; width: number; height: number }[];
};
export type PaperReference = {
  document_id: string;
  page: number;
  kind: "area" | "text";
  bounds: [number, number, number, number];
  quote: string;
  name: string;
  preview?: string;
};
export type PaperLocation = Pick<PaperReference, "document_id" | "page" | "bounds">;
export function paperReferenceLink(ref: PaperReference) {
  return `#paper-ref/${ref.document_id}/${ref.page}/${ref.kind}/${ref.bounds.join(",")}`;
}
export function formatPaperReference(ref: PaperReference, index: number) {
  const label = ref.name.replace(/[\[\]\\\r\n]/g, " ");
  const lines = [`📎 [S${index + 1} · ${label} · 第 ${ref.page} 页](${paperReferenceLink(ref)})`];
  if (ref.quote) lines.push(...ref.quote.split(/\r?\n/).map(line => "> " + line));
  return lines.join("\n");
}
export function parsePaperLocation(link: string): PaperLocation | undefined {
  const match = /^#paper-ref\/(doc_[a-f0-9]{64})\/(\d+)\/(?:area|text)\/(\d+,\d+,\d+,\d+)$/.exec(link);
  if (!match) return;
  const [x,y,right,bottom] = match[3].split(",").map(Number);
  if (!(x < right && y < bottom && right <= 1e6 && bottom <= 1e6 && Number(match[2]) >= 1)) return;
  return { document_id: match[1], page: Number(match[2]), bounds: [x,y,right,bottom] };
}

export function paperCitationDefinitions(userMessage: string) {
  const sources = userMessage.matchAll(/^📎 \[(S[12]) · [^\n]*\]\((#paper-ref\/[^)\n]+)\)$/gm);
  return Array.from(sources)
    .filter(([, , link]) => parsePaperLocation(link))
    .map(([, label, link]) => `[${label}]: ${link}`)
    .join("\n");
}
export type PaperNote = {
  id: string;
  document_id: string;
  content: string;
  anchor: NoteAnchor | null;
  revision: number;
  updated_at: string;
};
export const libraryRequest = <T>(
  key: string,
  path = "",
  init: RequestInit = {},
) => request<T>("/library" + path, key, init);
export const patchDocument = (
  key: string,
  id: string,
  data: Partial<LibraryDocument>,
) =>
  libraryRequest<LibraryDocument>(key, `/documents/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
export function fileSize(bytes: number) {
  return bytes >= 1048576
    ? `${(bytes / 1048576).toFixed(1)} MB`
    : `${Math.ceil(bytes / 1024)} KB`;
}

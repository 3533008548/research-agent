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

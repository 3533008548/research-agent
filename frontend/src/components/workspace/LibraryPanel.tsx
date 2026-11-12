import { useRef, useState } from "react";
import {
  fileSize,
  libraryRequest,
  type LibraryData,
  type LibraryDocument,
} from "../../lib/library";
import { userFacingError } from "../../lib/errors";

type Props = {
  apiKey: string;
  data: LibraryData;
  loading: boolean;
  error: string;
  load: () => Promise<void>;
  update: (
    id: string,
    change: Partial<LibraryDocument>,
  ) => Promise<LibraryDocument>;
  onOpen: (doc: LibraryDocument) => void;
  onSelect: (doc: LibraryDocument | null) => void;
};
export function LibraryPanel({
  apiKey,
  data,
  loading,
  error,
  load,
  update,
  onOpen,
  onSelect,
}: Props) {
  const [folder, setFolder] = useState("all"),
    [search, setSearch] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  const [operation, setOperation] = useState<
      "folder" | "rename" | "move" | "renameFolder" | null
    >(null),
    [name, setName] = useState(""),
    [target, setTarget] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const visible = data.documents.filter(
    (d) =>
      d.trashed === (folder === "trash") &&
      (folder === "all" ||
        folder === "trash" ||
        (folder === "root" ? !d.folder_id : d.folder_id === folder)) &&
      d.name.toLowerCase().includes(search.toLowerCase()),
  );
  const currentFolder = data.folders.find((f) => f.id === folder);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setMessage("");
    try {
      await fn();
      setOperation(null);
    } catch (e) {
      setMessage(userFacingError(e));
    } finally {
      setBusy(false);
    }
  }
  function chooseFolder(id: string) {
    setFolder(id);
    setSelected([]);
    onSelect(null);
    setOperation(null);
  }
  function select(id: string, checked: boolean) {
    const ids = checked ? [...selected, id] : selected.filter((s) => s !== id);
    setSelected(ids);
    onSelect(
      ids.length === 1
        ? data.documents.find((d) => d.id === ids[0]) || null
        : null,
    );
  }
  async function submit() {
    await act(async () => {
      if (operation === "folder" || operation === "renameFolder") {
        await libraryRequest(
          apiKey,
          operation === "folder" ? "/folders" : `/folders/${folder}`,
          {
            method: operation === "folder" ? "POST" : "PATCH",
            body: JSON.stringify({
              name,
              parent_id: currentFolder?.id || null,
            }),
          },
        );
        await load();
      } else if (operation === "rename" && selected[0])
        await update(selected[0], { name });
      else if (operation === "move") {
        await Promise.all(
          selected.map((id) => update(id, { folder_id: target || null })),
        );
        setSelected([]);
        onSelect(null);
      }
    });
  }
  return (
    <article className="library-page">
      <header className="library-heading">
        <div>
          <h1>论文库</h1>
          <p>
            {data.documents.filter((d) => !d.trashed).length} 篇论文 ·{" "}
            {data.folders.length} 个文件夹
          </p>
        </div>
        <div className="action-row">
          <button
            className="button button-secondary"
            disabled={busy}
            onClick={() => {
              setName("");
              setOperation("folder");
            }}
          >
            新建文件夹
          </button>
          <button
            className="button button-primary"
            disabled={busy}
            onClick={() => input.current?.click()}
          >
            {busy ? "处理中…" : "导入论文"}
          </button>
        </div>
      </header>
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        hidden
        onChange={(e) => {
          const files = Array.from(e.target.files || []);
          e.target.value = "";
          void act(async () => {
            let imported = 0;
            try {
              for (const file of files) {
                const form = new FormData();
                form.append("file", file);
                if (currentFolder) form.append("folder_id", currentFolder.id);
                const result = await fetch("/api/v1/library/documents", {
                  method: "POST",
                  headers: apiKey ? { "X-API-Key": apiKey } : {},
                  body: form,
                });
                if (!result.ok) {
                  const detail = await result
                    .json()
                    .catch(() => ({ detail: "导入失败" }));
                  throw Error(`${file.name}：${detail.detail}`);
                }
                imported++;
              }
            } finally {
              await load();
              setMessage(`已导入 ${imported} 篇论文（相同内容自动合并）`);
            }
          });
        }}
      />
      <nav className="folder-navigation" aria-label="论文文件夹">
        <button
          className={folder === "all" ? "selected" : ""}
          onClick={() => chooseFolder("all")}
        >
          全部论文
        </button>
        <button
          className={folder === "root" ? "selected" : ""}
          onClick={() => chooseFolder("root")}
        >
          未分类
        </button>
        {currentFolder && (
          <button
            onClick={() => chooseFolder(currentFolder.parent_id || "all")}
          >
            ← 上级
          </button>
        )}
        {data.folders
          .filter((f) => f.parent_id === (currentFolder?.id || null))
          .map((f) => (
            <button key={f.id} onClick={() => chooseFolder(f.id)}>
              ▱ {f.name}
            </button>
          ))}
        <button
          className={folder === "trash" ? "selected" : ""}
          onClick={() => chooseFolder("trash")}
        >
          回收站
        </button>
      </nav>
      {currentFolder && (
        <div className="action-row folder-path">
          <strong>{currentFolder.name}</strong>
          <button
            className="text-button"
            onClick={() => {
              setName(currentFolder.name);
              setOperation("renameFolder");
            }}
          >
            重命名文件夹
          </button>
          <button
            className="text-button"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                await libraryRequest(apiKey, `/folders/${folder}`, {
                  method: "DELETE",
                });
                chooseFolder("all");
                await load();
              })
            }
          >
            删除空文件夹
          </button>
        </div>
      )}
      <div className="library-search">
        <input
          aria-label="搜索论文"
          placeholder="搜索论文名称"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          className="button button-secondary"
          disabled={loading || busy}
          onClick={() => void load()}
        >
          刷新
        </button>
      </div>
      <div className="action-row selection-actions">
        <span>已选择 {selected.length} 篇</span>
        <button
          disabled={selected.length !== 1 || folder === "trash" || busy}
          onClick={() =>
            onOpen(data.documents.find((d) => d.id === selected[0])!)
          }
        >
          打开
        </button>
        <button
          disabled={selected.length !== 1 || busy}
          onClick={() => {
            setName(data.documents.find((d) => d.id === selected[0])!.name);
            setOperation("rename");
          }}
        >
          重命名
        </button>
        <button
          disabled={!selected.length || busy}
          onClick={() => {
            setTarget("");
            setOperation("move");
          }}
        >
          移动到
        </button>
        <button
          disabled={!selected.length || busy}
          onClick={() =>
            void act(async () => {
              await Promise.all(
                selected.map((id) =>
                  update(id, { trashed: folder !== "trash" }),
                ),
              );
              setSelected([]);
              onSelect(null);
            })
          }
        >
          {folder === "trash" ? "恢复论文" : "移入回收站"}
        </button>
      </div>
      {operation && (
        <form
          className="library-operation"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label>
            {operation === "move"
              ? "目标文件夹"
              : operation === "folder"
                ? "新文件夹名称"
                : "新名称"}
            {operation === "move" ? (
              <select
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              >
                <option value="">未分类</option>
                {data.folders.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                autoFocus
                required
                maxLength={240}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            )}
          </label>
          <button disabled={busy} className="button button-primary">
            确定
          </button>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setOperation(null)}
          >
            取消
          </button>
        </form>
      )}
      {(error || message) && (
        <p className="notice" role="status">
          {error || message}
        </p>
      )}
      {data.import_errors?.map((e) => (
        <p className="notice notice-error" key={e.name}>
          {e.name}：{e.error}
        </p>
      ))}
      <div className="library-table-wrap">
        <table className="library-table">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  aria-label="选择当前列表全部论文"
                  checked={
                    visible.length > 0 &&
                    visible.every((d) => selected.includes(d.id))
                  }
                  onChange={(e) => {
                    setSelected(
                      e.target.checked ? visible.map((d) => d.id) : [],
                    );
                    onSelect(null);
                  }}
                />
              </th>
              <th>论文名称</th>
              <th>文件夹</th>
              <th>阅读状态</th>
              <th>添加时间</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((d) => (
              <tr
                key={d.id}
                className={selected.includes(d.id) ? "selected" : ""}
              >
                <td>
                  <input
                    type="checkbox"
                    aria-label={`选择 ${d.name}`}
                    checked={selected.includes(d.id)}
                    onChange={(e) => select(d.id, e.target.checked)}
                  />
                </td>
                <td>
                  <button
                    className="paper-name"
                    disabled={d.trashed}
                    onClick={() => onOpen(d)}
                  >
                    {d.name}
                  </button>
                  <span className="file-meta">
                    PDF · {fileSize(d.size_bytes)} · {d.pages} 页
                  </span>
                </td>
                <td>
                  {data.folders.find((f) => f.id === d.folder_id)?.name ||
                    "未分类"}
                </td>
                <td>{d.progress.page ? `第 ${d.progress.page} 页` : "待读"}</td>
                <td>{new Date(d.created_at).toLocaleDateString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!visible.length && (
          <p className="empty-panel">
            {loading
              ? "正在读取论文库…"
              : folder === "trash"
                ? "回收站为空"
                : "没有符合条件的论文，可导入 PDF 或调整搜索。"}
          </p>
        )}
      </div>
    </article>
  );
}

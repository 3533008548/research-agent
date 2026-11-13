import { useEffect, useState } from "react";
import type { DesktopUpdateState } from "../../desktop";
import { userFacingError } from "../../lib/errors";

const labels: Record<DesktopUpdateState["status"], string> = {
  idle: "可检查新版本", checking: "正在检查更新…", available: "发现新版本",
  downloading: "正在下载更新…", downloaded: "更新已就绪", installing: "正在重启安装…",
  "up-to-date": "当前已是最新版本", error: "更新未完成", unsupported: "开发模式不提供应用内更新",
};

export function DesktopUpdatePanel() {
  const [state, setState] = useState<DesktopUpdateState | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const updates = window.desktop?.updates;
    if (!updates) return;
    let alive = true;
    const accept = (value: DesktopUpdateState) => { if (alive) setState(value); };
    const unsubscribe = updates.onStatus(accept);
    void updates.status().then(accept).catch(e => { if (alive) setError(userFacingError(e)); });
    return () => { alive = false; unsubscribe(); };
  }, []);
  if (!window.desktop?.updates) return null;
  async function action(name: "check" | "download" | "install") {
    setError("");
    try { setState(await window.desktop!.updates[name]()); }
    catch (e) { setError(userFacingError(e)); }
  }
  const busy = !state || ["checking", "downloading", "installing", "unsupported"].includes(state.status);
  return <section className="desktop-updates" aria-label="应用更新">
    <h2>应用更新</h2>
    {state && <>
      <p>当前版本 {state.currentVersion}{state.availableVersion && ` · 新版本 ${state.availableVersion}`}</p>
      <p role="status">{labels[state.status]}{state.status === "downloading" && ` ${state.percent.toFixed(0)}%`}</p>
      {state.status === "downloading" && <progress aria-label="更新下载进度" max={100} value={state.percent} />}
      {state.releaseNotes && <details><summary>更新说明</summary><pre className="update-release-notes">{state.releaseNotes}</pre></details>}
    </>}
    {(error || state?.error) && <p className="notice" role="alert">{error || state?.error}</p>}
    <div className="action-row">
      <button className="button button-secondary" disabled={busy || state?.status === "downloaded"} onClick={() => void action("check")}>检查更新</button>
      {state?.availableVersion && ["available", "error"].includes(state.status) &&
        <button className="button button-primary" onClick={() => void action("download")}>下载更新</button>}
      {state?.status === "downloaded" && <button className="button button-primary" onClick={() => void action("install")}>重启并安装</button>}
    </div>
    <p className="panel-hint">下载时可继续使用。完成当前任务后重启安装，论文、笔记与设置会保留。</p>
  </section>;
}

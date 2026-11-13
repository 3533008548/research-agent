import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  getWorkspaceProfile,
  getWorkspaceSettings,
  updateWorkspaceSettings,
} from "../../api";
import type { DailyKeyword, UserProfile, WorkspaceSettings } from "../../types";
import { ProfilePanel } from "./SimplePanels";
import { userFacingError } from "../../lib/errors";
import { DesktopUpdatePanel } from "./DesktopUpdatePanel";

export function SettingsPanel({
  apiKey,
  apiKeyDraft,
  onApiKeyDraftChange,
  onApplyApiKey,
  keywords,
  keywordDraft,
  onKeywordDraftChange,
  onAddKeyword,
  onRemoveKeyword,
  section,
  onSectionChange,
  onLoadDaily,
  children,
}: {
  apiKey: string;
  apiKeyDraft: string;
  onApiKeyDraftChange: (v: string) => void;
  onApplyApiKey: () => void;
  keywords: DailyKeyword[];
  keywordDraft: string;
  onKeywordDraftChange: (v: string) => void;
  onAddKeyword: (e: FormEvent<HTMLFormElement>) => void;
  onRemoveKeyword: (v: string) => void;
  section: "general" | "daily" | "profile";
  onSectionChange: (s: "general" | "daily" | "profile") => void;
  onLoadDaily: () => void;
  children: ReactNode;
}) {
  const [settings, setSettings] = useState<WorkspaceSettings | null>(null),
    [profile, setProfile] = useState<UserProfile | null>(null),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  const [desktopInfo, setDesktopInfo] = useState<{
      configured: boolean;
      dataDir: string;
      version: string;
    } | null>(null),
    [key, setKey] = useState(""),
    [openalex, setOpenalex] = useState("");
  useEffect(() => {
    let alive = true;
    void Promise.all([
      getWorkspaceSettings(apiKey),
      getWorkspaceProfile(apiKey),
    ])
      .then(([s, p]) => {
        if (alive) {
          setSettings(s);
          setProfile(p);
        }
      })
      .catch((e) => setMessage(userFacingError(e)));
    if (window.desktop)
      void window.desktop
        .info()
        .then(setDesktopInfo)
        .catch((e) => setMessage(userFacingError(e)));
    return () => {
      alive = false;
    };
  }, [apiKey]);
  async function save() {
    if (!settings) return;
    setBusy(true);
    try {
      await updateWorkspaceSettings(apiKey, {
        model: settings.model,
        rag_enabled: settings.rag_enabled,
        pdf_max_pages: settings.pdf_max_pages,
        daily_search_enabled: settings.daily_search_enabled,
      });
      setMessage("已保存。重启后台后生效。");
    } catch (e) {
      setMessage(userFacingError(e));
    } finally {
      setBusy(false);
    }
  }
  function choose(value: "general" | "daily" | "profile") {
    onSectionChange(value);
    if (value === "daily") onLoadDaily();
    if (value === "profile")
      void getWorkspaceProfile(apiKey)
        .then(setProfile)
        .catch((e) => setMessage(userFacingError(e)));
  }
  return (
    <article className="settings-page">
      <h1>设置</h1>
      <nav className="settings-tabs" aria-label="设置分区">
        {(
          [
            ["general", "通用"],
            ["daily", "每日检索"],
            ["profile", "用户画像"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            aria-pressed={section === id}
            onClick={() => choose(id)}
          >
            {label}
          </button>
        ))}
      </nav>
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {section === "general" && (
        <div className="settings-content">
          <h2>模型与论文处理</h2>
          {settings ? (
            <>
              <label>
                模型
                <input
                  value={settings.model}
                  onChange={(e) =>
                    setSettings({ ...settings, model: e.target.value })
                  }
                />
              </label>
              <label>
                PDF 解析最大页数
                <input
                  type="number"
                  min={5}
                  max={100}
                  value={settings.pdf_max_pages}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      pdf_max_pages: Number(e.target.value) || 5,
                    })
                  }
                />
                <small>限制后台解析量，不限制阅读器打开原文页数。</small>
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={settings.rag_enabled}
                  onChange={(e) =>
                    setSettings({ ...settings, rag_enabled: e.target.checked })
                  }
                />
                启用论文检索增强
              </label>
              <button
                className="button button-primary"
                disabled={busy || !settings.model.trim()}
                onClick={() => void save()}
              >
                保存通用设置
              </button>
            </>
          ) : (
            <p>正在读取设置…</p>
          )}
          {desktopInfo ? (
            <>
              <DesktopUpdatePanel />
              <h2>客户端连接</h2>
              <p className="panel-hint">
                {desktopInfo.configured
                  ? "模型密钥已配置，留空保留现有密钥。"
                  : "先配置模型密钥，随后可使用对话、翻译和研究。论文阅读及笔记无需密钥。"}
              </p>
              <label>
                模型 API Key
                <input
                  type="password"
                  autoComplete="off"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder="输入新密钥"
                />
              </label>
              <label>
                OpenAlex API Key（可选）
                <input
                  type="password"
                  autoComplete="off"
                  value={openalex}
                  onChange={(e) => setOpenalex(e.target.value)}
                />
              </label>
              <button
                className="button button-secondary"
                disabled={busy || (!key.trim() && !openalex.trim())}
                onClick={() =>
                  void (async () => {
                    setBusy(true);
                    try {
                      const result = await window.desktop!.configure({
                        deepseekKey: key,
                        openalexKey: openalex,
                      });
                      if (!result.saved) {
                        setMessage("已取消重启，密钥未更改。");
                        return;
                      }
                      setKey("");
                      setOpenalex("");
                      setMessage("密钥已加密保存，正在重启后台…");
                    } catch (e) {
                      setMessage(userFacingError(e));
                    } finally {
                      setBusy(false);
                    }
                  })()
                }
              >
                保存密钥并重启
              </button>
              <h2>数据与后台</h2>
              <p className="data-directory">{desktopInfo.dataDir}</p>
              <div className="action-row">
                <button
                  className="button button-secondary"
                  onClick={() =>
                    void window
                      .desktop!.chooseDataDirectory()
                      .catch((e) => setMessage(userFacingError(e)))
                  }
                >
                  选择数据目录
                </button>
                <button
                  className="button button-secondary"
                  onClick={() =>
                    void window
                      .desktop!.restart()
                      .catch((e) => setMessage(userFacingError(e)))
                  }
                >
                  重启后台
                </button>
              </div>
              <p className="panel-hint">
                关闭窗口后后台继续运行。可从系统托盘重新打开或退出。
              </p>
            </>
          ) : (
            <>
              <h2>API 连接</h2>
              <label>
                API 令牌
                <input
                  type="password"
                  autoComplete="off"
                  value={apiKeyDraft}
                  onChange={(e) => onApiKeyDraftChange(e.target.value)}
                />
              </label>
              <button
                className="button button-secondary"
                onClick={onApplyApiKey}
              >
                应用令牌
              </button>
            </>
          )}
        </div>
      )}
      <section hidden={section !== "daily"} className="daily-settings">
        <h2>检索设置</h2>
        {settings && (
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={settings.daily_search_enabled}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  daily_search_enabled: e.target.checked,
                })
              }
            />
            启用每日自动检索
          </label>
        )}
        <form className="keyword-form" onSubmit={onAddKeyword}>
          <input
            aria-label="新关键词"
            value={keywordDraft}
            onChange={(e) => onKeywordDraftChange(e.target.value)}
            placeholder="输入检索关键词"
          />
          <button
            className="button button-secondary"
            disabled={!keywordDraft.trim()}
          >
            添加关键词
          </button>
        </form>
        <div className="settings-keywords">
          {keywords.map((k) => (
            <span key={k.keyword}>
              {k.keyword}
              <button
                aria-label={`删除关键词 ${k.keyword}`}
                onClick={() => onRemoveKeyword(k.keyword)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
        <button
          className="button button-secondary"
          disabled={busy}
          onClick={() => void save()}
        >
          保存检索设置
        </button>
        {children}
      </section>
      {section === "profile" &&
        (profile ? (
          <ProfilePanel profile={profile} />
        ) : (
          <p>正在读取用户画像…</p>
        ))}
    </article>
  );
}

import {
  getWorkspaceProfile,
  getWorkspaceSettings,
  listPapers,
  listResearchDocuments,
} from "../api";
import { userFacingError } from "../lib/errors";
import type { Notice, WorkbenchTab, WorkspacePanel } from "../lib/workspace";
import type { Paper, ResearchDocument, UserProfile, WorkspaceSettings } from "../types";

/**
 * The workspace chrome: opening a panel, loading what it shows, and the
 * shortcuts that drop a prepared instruction into the chat.
 *
 * Extracted from `App.tsx` without behaviour change.  The panel *contents*
 * live in their own components; this hook only decides which panel is open
 * and fetches what that panel needs.
 */
export function useWorkspaceChrome({
  apiKey,
  setNotice,
  setDocuments,
  setProfile,
  setPapers,
  setWorkspaceSettings,
  setSettingsDraft,
  openWorkspacePanel,
  setOpenWorkspacePanel,
  setWorkspaceLoading,
  setDraft,
  setWorkbenchTabs,
  setActiveWorkbenchTabId,
  refreshExperiments,
  loadKeywords,
}: {
  apiKey: string;
  setNotice: (notice: Notice) => void;
  setDocuments: (documents: ResearchDocument[]) => void;
  setProfile: (profile: UserProfile | null) => void;
  setPapers: (papers: Paper[]) => void;
  setWorkspaceSettings: (settings: WorkspaceSettings | null) => void;
  setSettingsDraft: (draft: {
    model: string;
    rag_enabled: boolean;
    pdf_max_pages: number;
    daily_search_enabled: boolean;
  }) => void;
  openWorkspacePanel: WorkspacePanel | null;
  setOpenWorkspacePanel: (panel: WorkspacePanel | null) => void;
  setWorkspaceLoading: (panel: WorkspacePanel | null) => void;
  setDraft: (value: string) => void;
  setWorkbenchTabs: (updater: (current: WorkbenchTab[]) => WorkbenchTab[]) => void;
  setActiveWorkbenchTabId: (tabId: string) => void;
  refreshExperiments: () => Promise<void>;
  loadKeywords: () => Promise<void>;
}) {
  async function toggleWorkspacePanel(panel: WorkspacePanel) {
    if (openWorkspacePanel === panel) {
      setOpenWorkspacePanel(null);
      return;
    }
    setOpenWorkspacePanel(panel);
    setWorkspaceLoading(panel);
    try {
      if (panel === "documents") {
        setDocuments(await listResearchDocuments(apiKey));
      } else if (panel === "profile") {
        setProfile(await getWorkspaceProfile(apiKey));
      } else if (panel === "papers") {
        setPapers(await listPapers(apiKey));
      } else if (panel === "experiments") {
        await refreshExperiments();
      } else if (panel === "keywords") {
        await loadKeywords();
      } else {
        const settings = await getWorkspaceSettings(apiKey);
        setWorkspaceSettings(settings);
        setSettingsDraft({
          model: settings.model,
          rag_enabled: settings.rag_enabled,
          pdf_max_pages: settings.pdf_max_pages,
          daily_search_enabled: settings.daily_search_enabled,
        });
      }
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setWorkspaceLoading(null);
    }
  }

  function preparePaperRelationReview(paper: Paper) {
    setDraft(
      `【论文关系审查】\n源论文：${paper.title}\nsource_paper_id=${paper.paper_id}\n\n`
      + "请先检查这篇论文与下列目标论文之间是否存在值得记录的关系。必须用两篇论文的页码或检索片段定位证据；只可提出候选，不要自动写入。请说明关系方向、关系类型（方法相似/方法改进/实验可比/结果冲突/明确引用）、依据和不确定性，最后等待我确认。\n\n"
      + "目标论文标题或 paper_id：- （在此填写）",
    );
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "chat") ? current : [...current, { id: "chat", kind: "chat", title: "对话" }]);
    setActiveWorkbenchTabId("chat");
    setNotice({ kind: "info", text: "论文关系审查指令已放入对话框。Agent 会先给出带证据的候选关系，只有你确认后才会保存。" });
  }

  return {
    toggleWorkspacePanel,
    preparePaperRelationReview,
  };
}

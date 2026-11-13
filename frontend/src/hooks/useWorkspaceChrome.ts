import {
  getWorkspaceProfile,
  getWorkspaceSettings,
  listPapers,
} from "../api";
import { userFacingError } from "../lib/errors";
import type { Notice, WorkspacePanel } from "../lib/workspace";
import type { Paper, UserProfile, WorkspaceSettings } from "../types";

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
  setProfile,
  setPapers,
  setWorkspaceSettings,
  setSettingsDraft,
  openWorkspacePanel,
  setOpenWorkspacePanel,
  setWorkspaceLoading,
  loadKeywords,
}: {
  apiKey: string;
  setNotice: (notice: Notice) => void;
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
      if (panel === "profile") {
        setProfile(await getWorkspaceProfile(apiKey));
      } else if (panel === "papers") {
        setPapers(await listPapers(apiKey));
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

  return {
    toggleWorkspacePanel,
  };
}

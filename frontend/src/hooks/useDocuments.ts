import { useState, type Dispatch, type FormEvent, type SetStateAction } from "react";

import {
  deleteResearchDocument,
  downloadResearchDocument,
  getResearchDocument,
  getWorkspaceProfile,
  routeResearchDocumentRequest,
} from "../api";
import { userFacingError } from "../lib/errors";
import type { Notice, WorkbenchTab } from "../lib/workspace";
import type { ResearchMode } from "../lib/labels";
import type { ResearchDocument, ResearchDocumentDetail, UserProfile } from "../types";

/**
 * The research-document workspace: the saved documents, the request composer
 * that asks the agent to work around one, and the user profile.
 *
 * Extracted from `App.tsx` without behaviour change.
 */
export function useDocuments({
  apiKey,
  isSending,
  setNotice,
  setWorkbenchTabs,
  setActiveWorkbenchTabId,
  closeWorkbenchTab,
  setMode,
  sendMessage,
}: {
  apiKey: string;
  isSending: boolean;
  setNotice: (notice: Notice) => void;
  setWorkbenchTabs: Dispatch<SetStateAction<WorkbenchTab[]>>;
  setActiveWorkbenchTabId: (tabId: string) => void;
  closeWorkbenchTab: (tabId: string) => void;
  setMode: (mode: ResearchMode) => void;
  sendMessage: (
    event: FormEvent<HTMLFormElement> | undefined,
    resumeResearch?: boolean,
    retryContent?: string,
    requestedMode?: ResearchMode,
  ) => Promise<void>;
}) {
  const [documents, setDocuments] = useState<ResearchDocument[]>([]);
  const [documentRequest, setDocumentRequest] = useState("");
  const [profile, setProfile] = useState<UserProfile | null>(null);

  async function selectResearchDocument(documentId: string) {
    try {
      const document = await getResearchDocument(apiKey, documentId);
      const tabId = `document:${document.document_id}`;
      setWorkbenchTabs((current) => {
        const existing = current.find((tab) => tab.id === tabId);
        if (existing?.kind === "document") {
          return current.map((tab) => tab.id === tabId ? { ...tab, title: document.title, document } : tab);
        }
        return [...current, { id: tabId, kind: "document", title: document.title, document }];
      });
      setActiveWorkbenchTabId(tabId);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function submitResearchDocumentRequest(event: FormEvent<HTMLFormElement>, document: ResearchDocumentDetail) {
    event.preventDefault();
    const request = documentRequest.trim();
    if (!request || isSending) {
      return;
    }
    try {
      const routed = await routeResearchDocumentRequest(apiKey, document.document_id, request);
      setDocumentRequest("");
      setMode("chat");
      setWorkbenchTabs((current) => current.some((tab) => tab.id === "chat") ? current : [...current, { id: "chat", kind: "chat", title: "对话" }]);
      setActiveWorkbenchTabId("chat");
      await sendMessage(undefined, false, routed.message, "chat");
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function openProfile() {
    try {
      const nextProfile = profile || await getWorkspaceProfile(apiKey);
      setProfile(nextProfile);
      setWorkbenchTabs((current) => current.some((tab) => tab.id === "profile")
        ? current.map((tab) => tab.id === "profile"
          ? { id: "profile", kind: "profile", title: "用户画像", profile: nextProfile }
          : tab)
        : [...current, { id: "profile", kind: "profile", title: "用户画像", profile: nextProfile }]);
      setActiveWorkbenchTabId("profile");
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function downloadDocument(document: ResearchDocumentDetail) {
    try {
      await downloadResearchDocument(apiKey, document);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function removeResearchDocument(document: ResearchDocument) {
    if (!window.confirm(`删除科研档案“${document.title}”？这会同时删除该档案的正文、证据与假设账本及历史版本文件，且不可恢复。`)) {
      return;
    }
    try {
      await deleteResearchDocument(apiKey, document.document_id);
      setDocuments((current) => current.filter((item) => item.document_id !== document.document_id));
      closeWorkbenchTab(`document:${document.document_id}`);
      setNotice({ kind: "info", text: "科研档案已删除。" });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  return {
    documents,
    documentRequest,
    profile,
    setDocumentRequest,
    setDocuments,
    setProfile,
    selectResearchDocument,
    submitResearchDocumentRequest,
    downloadDocument,
    removeResearchDocument,
    openProfile,
  };
}

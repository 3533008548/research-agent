import { useState, type Dispatch, type SetStateAction } from "react";
import { getWorkspaceProfile } from "../api";
import { userFacingError } from "../lib/errors";
import type { Notice, WorkbenchTab } from "../lib/workspace";
import type { UserProfile } from "../types";

export function useProfile({ apiKey, setNotice, setWorkbenchTabs, setActiveWorkbenchTabId }: {
  apiKey: string;
  setNotice: (notice: Notice) => void;
  setWorkbenchTabs: Dispatch<SetStateAction<WorkbenchTab[]>>;
  setActiveWorkbenchTabId: (tabId: string) => void;
}) {
  const [profile, setProfile] = useState<UserProfile | null>(null);

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

  return { profile, setProfile, openProfile };
}

export type SidebarSide = "left" | "right";
export type WorkspaceLayoutMode = "desktop" | "compact" | "stacked";
export type RightSidebarVisibility = "auto" | "shown" | "hidden";

export interface WorkspaceLayoutPreferences {
  version: 1;
  leftWidth: number;
  rightWidth: number;
  leftVisible: boolean;
  rightVisibility: RightSidebarVisibility;
}

export const WORKSPACE_LAYOUT_KEY = "research-agent.workspace-layout.v1";
export const CENTER_MIN_WIDTH = 480;
export const COMPACT_CENTER_MIN_WIDTH = 320;
export const SIDEBAR_LIMITS = {
  left: { min: 190, max: 420, initial: 240 },
  right: { min: 245, max: 480, initial: 310 },
} as const;

export function defaultLayoutPreferences(): WorkspaceLayoutPreferences {
  return {
    version: 1,
    leftWidth: SIDEBAR_LIMITS.left.initial,
    rightWidth: SIDEBAR_LIMITS.right.initial,
    leftVisible: true,
    rightVisibility: "auto",
  };
}

export function clampSidebarWidth(side: SidebarSide, width: number): number {
  const limits = SIDEBAR_LIMITS[side];
  return Number.isFinite(width)
    ? Math.round(Math.min(limits.max, Math.max(limits.min, width)))
    : limits.initial;
}

/** Stored preferences are untrusted; responsive compression never writes here. */
export function parseLayoutPreferences(raw: string | null): WorkspaceLayoutPreferences {
  const defaults = defaultLayoutPreferences();
  if (!raw) return defaults;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) return defaults;
    const stored = value as Record<string, unknown>;
    if (stored.version !== 1) return defaults;
    return {
      version: 1,
      leftWidth: typeof stored.leftWidth === "number" ? clampSidebarWidth("left", stored.leftWidth) : defaults.leftWidth,
      rightWidth: typeof stored.rightWidth === "number" ? clampSidebarWidth("right", stored.rightWidth) : defaults.rightWidth,
      leftVisible: typeof stored.leftVisible === "boolean" ? stored.leftVisible : defaults.leftVisible,
      rightVisibility: ["auto", "shown", "hidden"].includes(String(stored.rightVisibility))
        ? stored.rightVisibility as RightSidebarVisibility
        : defaults.rightVisibility,
    };
  } catch {
    return defaults;
  }
}

export function workspaceLayoutMode(viewportWidth: number): WorkspaceLayoutMode {
  return viewportWidth <= 760 ? "stacked" : viewportWidth <= 1120 ? "compact" : "desktop";
}

export function rightSidebarVisible(preference: RightSidebarVisibility, docFocusMode: boolean): boolean {
  return preference === "auto" ? !docFocusMode : preference === "shown";
}

export interface ResolvedWorkspaceWidths {
  mode: WorkspaceLayoutMode;
  leftWidth: number;
  rightWidth: number;
  centerWidth: number;
  leftBounds: { min: number; max: number };
  rightBounds: { min: number; max: number };
}

/** Keep the center usable while treating saved widths as requests, not CSS minima. */
export function resolveWorkspaceWidths({
  workspaceWidth,
  viewportWidth,
  preferences,
  leftVisible,
  rightVisible,
}: {
  workspaceWidth: number;
  viewportWidth: number;
  preferences: WorkspaceLayoutPreferences;
  leftVisible: boolean;
  rightVisible: boolean;
}): ResolvedWorkspaceWidths {
  const width = Number.isFinite(workspaceWidth) ? Math.max(0, workspaceWidth) : 0;
  const mode = workspaceLayoutMode(Math.min(viewportWidth, width));
  const leftInRow = leftVisible && mode !== "stacked";
  const rightInRow = rightVisible && mode !== "stacked";
  let leftWidth = leftInRow ? clampSidebarWidth("left", preferences.leftWidth) : 0;
  let rightWidth = rightInRow ? clampSidebarWidth("right", preferences.rightWidth) : 0;
  const leftMin = leftInRow ? SIDEBAR_LIMITS.left.min : 0;
  const rightMin = rightInRow ? SIDEBAR_LIMITS.right.min : 0;
  // Keep both rails beside chat in restored desktop windows. Only compact
  // widths relax the reading width; saved preferences remain untouched.
  const centerMin = Math.max(COMPACT_CENTER_MIN_WIDTH, Math.min(CENTER_MIN_WIDTH, width - leftMin - rightMin));
  const budget = Math.max(0, width - centerMin);

  if (leftWidth + rightWidth > budget) {
    const minTotal = leftMin + rightMin;
    if (budget >= minTotal) {
      // A stable allocation makes a requested 10px keyboard change remain a
      // 10px change while compressed. Proportional allocation would also
      // reinterpret the new request and make a separator jump unexpectedly.
      leftWidth = Math.min(leftWidth, budget - rightMin);
      rightWidth = Math.min(rightWidth, budget - leftWidth);
    } else if (minTotal > 0) {
      // A constrained embedding can be narrower than its viewport breakpoint.
      // Preserve the center first rather than overflowing the entire page.
      leftWidth = Math.floor(budget * leftMin / minTotal);
      rightWidth = Math.floor(budget * rightMin / minTotal);
    }
  }

  leftWidth = Math.floor(leftWidth);
  rightWidth = Math.floor(rightWidth);

  const bounds = (side: SidebarSide, enabled: boolean, otherWidth: number) => {
    const max = enabled ? Math.floor(Math.max(0, Math.min(SIDEBAR_LIMITS[side].max, budget - otherWidth))) : 0;
    return { min: Math.min(SIDEBAR_LIMITS[side].min, max), max };
  };
  return {
    mode,
    leftWidth,
    rightWidth,
    centerWidth: Math.max(0, width - leftWidth - rightWidth),
    leftBounds: bounds("left", leftInRow, rightWidth),
    rightBounds: bounds("right", rightInRow, leftWidth),
  };
}

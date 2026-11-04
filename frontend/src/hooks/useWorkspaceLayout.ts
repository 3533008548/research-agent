import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import type { SidebarResizeHandleProps } from "../components/SidebarResizeHandle";
import {
  defaultLayoutPreferences,
  parseLayoutPreferences,
  resolveWorkspaceWidths,
  rightSidebarVisible,
  WORKSPACE_LAYOUT_KEY,
  type SidebarSide,
  type WorkspaceLayoutPreferences,
} from "../lib/workspaceLayout";

interface ActiveDrag {
  side: SidebarSide;
  startX: number;
  startWidth: number;
  pointerId: number;
  target: HTMLDivElement;
  previousUserSelect: string;
  previousCursor: string;
}

function readPreferences() {
  try {
    return parseLayoutPreferences(localStorage.getItem(WORKSPACE_LAYOUT_KEY));
  } catch {
    return defaultLayoutPreferences();
  }
}

function persistPreferences(preferences: WorkspaceLayoutPreferences) {
  try {
    localStorage.setItem(WORKSPACE_LAYOUT_KEY, JSON.stringify(preferences));
  } catch {
    // Storage is optional; layout controls remain usable in restricted browsers.
  }
}

/** UI-only preferences: hiding a pane never changes a session, run or SSE stream. */
export function useWorkspaceLayout({ docFocusMode }: { docFocusMode: boolean }) {
  const workspaceRef = useRef<HTMLDivElement>(null);
  const [preferences, setPreferences] = useState(readPreferences);
  const preferencesRef = useRef(preferences);
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const [workspaceWidth, setWorkspaceWidth] = useState(() => Math.min(window.innerWidth, 1700));
  const dragRef = useRef<ActiveDrag | null>(null);
  const [draggingSide, setDraggingSide] = useState<SidebarSide | null>(null);

  const updatePreferences = useCallback((next: WorkspaceLayoutPreferences, persist = true) => {
    preferencesRef.current = next;
    setPreferences(next);
    if (persist) persistPreferences(next);
  }, []);

  const finishDrag = useCallback((persist = true, render = true) => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    document.body.style.userSelect = drag.previousUserSelect;
    document.body.style.cursor = drag.previousCursor;
    try {
      if (drag.target.hasPointerCapture(drag.pointerId)) drag.target.releasePointerCapture(drag.pointerId);
    } catch {
      // The pane may have disappeared and the browser already ended capture.
    }
    if (persist) persistPreferences(preferencesRef.current);
    if (render) setDraggingSide(null);
  }, []);

  useLayoutEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace) return;
    const measure = () => setWorkspaceWidth(workspace.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(workspace);
    const onResize = () => {
      finishDrag();
      setViewportWidth(window.innerWidth);
      measure();
    };
    window.addEventListener("resize", onResize);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, [finishDrag]);

  useEffect(() => {
    const onBlur = () => finishDrag();
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("blur", onBlur);
      finishDrag(false, false);
    };
  }, [finishDrag]);

  const leftVisible = preferences.leftVisible;
  const rightVisible = rightSidebarVisible(preferences.rightVisibility, docFocusMode);
  const widths = useMemo(() => resolveWorkspaceWidths({
    workspaceWidth, viewportWidth, preferences, leftVisible, rightVisible,
  }), [workspaceWidth, viewportWidth, preferences, leftVisible, rightVisible]);
  const leftResizable = leftVisible && widths.mode !== "stacked" && widths.leftBounds.max > widths.leftBounds.min;
  const rightResizable = rightVisible && widths.mode === "desktop" && widths.rightBounds.max > widths.rightBounds.min;

  useLayoutEffect(() => {
    const drag = dragRef.current;
    if (drag && !(drag.side === "left" ? leftResizable : rightResizable)) {
      // Document focus can hide a pane while its run finishes. A removed
      // handle need not deliver lostpointercapture back through React.
      finishDrag();
    }
  }, [leftResizable, rightResizable, draggingSide, finishDrag]);

  const setWidth = (side: SidebarSide, value: number, persist: boolean) => {
    const limits = side === "left" ? widths.leftBounds : widths.rightBounds;
    const width = Math.round(Math.max(limits.min, Math.min(limits.max, value)));
    // An explicit resize starts a new layout from the currently visible
    // dimensions. Keep the opposite pane stable instead of letting an older,
    // compressed request absorb every pixel released during this gesture.
    // Viewport-only changes never take this path and retain both requests.
    const next = { ...preferencesRef.current };
    if (leftVisible && widths.mode !== "stacked") next.leftWidth = widths.leftWidth;
    if (rightVisible && widths.mode === "desktop") next.rightWidth = widths.rightWidth;
    next[side === "left" ? "leftWidth" : "rightWidth"] = width;
    updatePreferences(next, persist);
  };

  const resizeProps = (side: SidebarSide): SidebarResizeHandleProps | null => {
    const allowed = side === "left" ? leftResizable : rightResizable;
    const { min, max } = side === "left" ? widths.leftBounds : widths.rightBounds;
    if (!allowed) return null;
    const width = side === "left" ? widths.leftWidth : widths.rightWidth;
    return {
      side, width, min, max, isDragging: draggingSide === side,
      onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
        if (event.button !== 0 || !event.isPrimary) return;
        event.preventDefault();
        finishDrag();
        const target = event.currentTarget;
        dragRef.current = {
          side, startX: event.clientX, startWidth: width,
          pointerId: event.pointerId, target,
          previousUserSelect: document.body.style.userSelect,
          previousCursor: document.body.style.cursor,
        };
        target.setPointerCapture(event.pointerId);
        target.focus({ preventScroll: true });
        document.body.style.userSelect = "none";
        document.body.style.cursor = "col-resize";
        setDraggingSide(side);
      },
      onPointerMove: (event: PointerEvent<HTMLDivElement>) => {
        const drag = dragRef.current;
        if (!drag || drag.side !== side || drag.pointerId !== event.pointerId) return;
        setWidth(side, drag.startWidth + (event.clientX - drag.startX) * (side === "left" ? 1 : -1), false);
      },
      onPointerUp: (event: PointerEvent<HTMLDivElement>) => {
        if (dragRef.current?.pointerId === event.pointerId) finishDrag();
      },
      onPointerCancel: (event: PointerEvent<HTMLDivElement>) => {
        if (dragRef.current?.pointerId === event.pointerId) finishDrag();
      },
      onLostPointerCapture: (event: PointerEvent<HTMLDivElement>) => {
        if (dragRef.current?.pointerId === event.pointerId) finishDrag();
      },
      onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
        let next: number;
        if (event.key === "Home") next = min;
        else if (event.key === "End") next = max;
        else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          const direction = event.key === "ArrowRight" ? 1 : -1;
          next = width + direction * (side === "left" ? 10 : -10);
        } else return;
        event.preventDefault();
        setWidth(side, next, true);
      },
    };
  };

  const toggleLeft = () => {
    finishDrag();
    updatePreferences({ ...preferencesRef.current, leftVisible: !leftVisible });
  };
  const toggleRight = () => {
    finishDrag();
    updatePreferences({ ...preferencesRef.current, rightVisibility: rightVisible ? "hidden" : "shown" });
  };
  const showRight = () => {
    finishDrag();
    updatePreferences({ ...preferencesRef.current, rightVisibility: "shown" });
  };
  const resetLayout = () => {
    finishDrag();
    updatePreferences(defaultLayoutPreferences(), false);
    try {
      localStorage.removeItem(WORKSPACE_LAYOUT_KEY);
    } catch {
      // Defaults are applied in memory even when storage cannot be changed.
    }
  };

  const workspaceStyle = {
    "--left-width": `${widths.leftWidth}px`,
    "--right-width": `${widths.rightWidth}px`,
  } as CSSProperties;
  return {
    workspaceRef, workspaceStyle, layoutMode: widths.mode,
    leftVisible, rightVisible, leftWidth: widths.leftWidth, rightWidth: widths.rightWidth,
    toggleLeft, toggleRight, showRight, resetLayout,
    leftResizeProps: resizeProps("left"), rightResizeProps: resizeProps("right"),
  };
}

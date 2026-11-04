import type { KeyboardEventHandler, PointerEventHandler } from "react";
import type { SidebarSide } from "../lib/workspaceLayout";

export interface SidebarResizeHandleProps {
  side: SidebarSide;
  width: number;
  min: number;
  max: number;
  isDragging: boolean;
  onPointerDown: PointerEventHandler<HTMLDivElement>;
  onPointerMove: PointerEventHandler<HTMLDivElement>;
  onPointerUp: PointerEventHandler<HTMLDivElement>;
  onPointerCancel: PointerEventHandler<HTMLDivElement>;
  onLostPointerCapture: PointerEventHandler<HTMLDivElement>;
  onKeyDown: KeyboardEventHandler<HTMLDivElement>;
}

/** Positioned inside its aside; this handle must never become another Grid item. */
export function SidebarResizeHandle({ side, width, min, max, isDragging, ...events }: SidebarResizeHandleProps) {
  const name = side === "left" ? "会话侧栏" : "运行检查器";
  return (
    <div
      {...events}
      className={`sidebar-resize-handle sidebar-resize-${side}${isDragging ? " dragging" : ""}`}
      role="separator"
      tabIndex={0}
      aria-label={`调整${name}宽度`}
      aria-orientation="vertical"
      aria-controls={side === "left" ? "session-sidebar" : "run-inspector"}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={Math.round(width)}
      aria-valuetext={`${Math.round(width)} 像素`}
      title={`拖动调整${name}宽度；左右方向键微调，Home / End 调至边界`}
    />
  );
}

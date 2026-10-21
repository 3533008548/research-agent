import type { ReactNode } from "react";

/**
 * A collapsible section of the left rail.
 *
 * Extracted from `App.tsx` without behaviour change.  The panel only renders
 * its children while open, so an unread list is not paid for while collapsed.
 */
export function WorkspacePanel({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className={`workspace-panel ${open ? "open" : ""}`}>
      <button className="workspace-panel-toggle" type="button" onClick={onToggle} aria-expanded={open}>
        <span>{title}</span>
        <span className="workspace-panel-chevron" aria-hidden="true">⌄</span>
      </button>
      {open && <div className="workspace-panel-content">{children}</div>}
    </section>
  );
}

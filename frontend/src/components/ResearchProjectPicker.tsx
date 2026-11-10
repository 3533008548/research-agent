import { useRef } from "react";
import type { ResearchProject } from "../types";

/** An in-page disclosure keeps full questions readable inside the rail.
 * Native select popups can size themselves to the longest option outside CSS.
 */
export function ResearchProjectPicker({ projects, selected, onSelect }: {
  projects: Array<Pick<ResearchProject, "project_id" | "question">>;
  selected: string;
  onSelect: (id: string) => void;
}) {
  const disclosure = useRef<HTMLDetailsElement>(null);
  const trigger = useRef<HTMLElement>(null);
  const close = () => {
    if (disclosure.current) disclosure.current.open = false;
    trigger.current?.focus({ preventScroll: true });
  };
  const current = projects.find(project => project.project_id === selected);
  return <div className="research-project-field">
    <span>研究问题</span>
    <details ref={disclosure} className="research-project-picker" onKeyDown={event => {
      if (event.key === "Escape" && disclosure.current?.open) {
        event.preventDefault(); event.stopPropagation(); close();
      }
    }} onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) event.currentTarget.open = false;
    }}>
      <summary ref={trigger} aria-label="选择研究问题">
        <span>{current?.question || "选择研究问题"}</span><span aria-hidden="true">⌄</span>
      </summary>
      <div className="research-project-options" role="group" aria-label="研究问题列表">
        {projects.map(project => <button type="button" key={project.project_id}
          aria-pressed={project.project_id === selected} onClick={() => { onSelect(project.project_id); close(); }}>
          {project.question}
        </button>)}
      </div>
    </details>
  </div>;
}

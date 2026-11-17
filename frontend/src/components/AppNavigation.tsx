import type { AppPage } from "../lib/workspace";

const pages = [
  { id: "workspace", label: "工作台" },
  { id: "library", label: "论文库" },
  { id: "settings", label: "设置" },
] as const;

function NavigationIcon({ page }: { page: AppPage }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {page === "workspace" ? <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z" /><path d="M9 21v-8h6v8" /></>
        : page === "library" ? <><path d="M12 5v16M12 5C9 3 5 3 2 4v15c3-1 7-1 10 2 3-3 7-3 10-2V4c-3-1-7-1-10 1Z" /></>
        : <><path d="m9.5 3-.6 2.2-1.6.9-2.2-.6-2.5 4.3 1.6 1.6v1.8l-1.6 1.6 2.5 4.3 2.2-.6 1.6.9.6 2.2h5l.6-2.2 1.6-.9 2.2.6 2.5-4.3-1.6-1.6v-1.8l1.6-1.6-2.5-4.3-2.2.6-1.6-.9-.6-2.2Z" /><circle cx="12" cy="12.3" r="3" /></>}
    </svg>
  );
}

export function AppNavigation({ page, onNavigate }: { page: AppPage; onNavigate: (page: AppPage) => void }) {
  return (
    <nav className="app-navigation" aria-label="应用导航">
      {pages.map(item => (
        <button key={item.id} type="button" className={`app-navigation-button app-navigation-${item.id}`}
          aria-label={item.label} title={item.label} aria-current={page === item.id ? "page" : undefined}
          aria-controls={`app-page-${item.id}`} onClick={() => onNavigate(item.id)}>
          <NavigationIcon page={item.id} />
        </button>
      ))}
    </nav>
  );
}

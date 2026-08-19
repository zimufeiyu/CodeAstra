import { ReactNode } from "react";

export type SettingsNavItem = {
  id: string;
  label: string;
};

type SettingsShellProps = {
  ariaLabel: string;
  activeId: string;
  items: SettingsNavItem[];
  onSelect: (id: string) => void;
  children: ReactNode;
  aside?: ReactNode;
};

export function SettingsShell({ ariaLabel, activeId, items, onSelect, children, aside }: SettingsShellProps) {
  return <div className="settings-shell">
    <aside className="settings-shell-nav-pane">
      {aside}
      <nav className="settings-shell-nav" aria-label={ariaLabel}>
        {items.map((item) => <button
          key={item.id}
          type="button"
          aria-current={item.id === activeId ? "page" : undefined}
          onClick={() => onSelect(item.id)}
        >{item.label}</button>)}
      </nav>
    </aside>
    <section className="settings-shell-content">{children}</section>
  </div>;
}

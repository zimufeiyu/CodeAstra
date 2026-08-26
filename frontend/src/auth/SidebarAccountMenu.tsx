import { LogOut, MoreHorizontal, Settings, ShieldCheck } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { AuthUser } from "../api/authClient";

type Props = { user: AuthUser; onOpenSettings: () => void; onOpenAdmin: () => void; onSignOut: () => void };

export function SidebarAccountMenu({ user, onOpenSettings, onOpenAdmin, onSignOut }: Props) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const initial = user.username.trim().slice(0, 1).toUpperCase() || "U";
  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) requestAnimationFrame(() => triggerRef.current?.focus());
  }
  function invoke(action: () => void) { close(); action(); }
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) close(true);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close(true); }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);
  return <div className="sidebar-account-menu" ref={rootRef}>
    {open ? <div id={menuId} className="sidebar-account-popover" role="menu" aria-label="账户菜单">
      <button type="button" role="menuitem" onClick={() => invoke(onOpenSettings)}><Settings size={16} aria-hidden="true" />账户设置</button>
      {user.role === "admin" ? <button type="button" role="menuitem" onClick={() => invoke(onOpenAdmin)}><ShieldCheck size={16} aria-hidden="true" />管理员管理</button> : null}
      <div className="sidebar-account-menu-divider" role="separator" />
      <button type="button" role="menuitem" className="sidebar-account-signout" onClick={() => invoke(onSignOut)}><LogOut size={16} aria-hidden="true" />退出登录</button>
    </div> : null}
    <button ref={triggerRef} type="button" className="sidebar-account-trigger" aria-label={`${user.username} 的账户菜单`} aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={() => setOpen((value) => !value)}>
      <span className="sidebar-account-avatar" aria-hidden="true">{initial}</span>
      <span className="sidebar-account-copy"><strong title={user.username}>{user.username}</strong><small>{user.role === "admin" ? "管理员" : "账户与设置"}</small></span>
      <MoreHorizontal size={18} aria-hidden="true" />
    </button>
  </div>;
}

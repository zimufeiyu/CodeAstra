import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { SidebarAccountMenu } from "./SidebarAccountMenu";

const user = {
  user_id: "u1", username: "alice", role: "user" as const,
  is_active: true, must_change_password: false,
  password_changed_at: "2026-08-17T00:00:00Z", csrf_token: "csrf",
};

test("opens the bottom-sidebar account menu by keyboard and restores focus on Escape", async () => {
  const interactions = userEvent.setup();
  render(<SidebarAccountMenu user={user} onOpenSettings={vi.fn()} onOpenAdmin={vi.fn()} onSignOut={vi.fn()} />);

  const trigger = screen.getByRole("button", { name: /alice.*账户菜单/i });
  await interactions.click(trigger);
  expect(screen.getByRole("menu", { name: "账户菜单" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "账户设置" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "退出登录" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "管理员管理" })).not.toBeInTheDocument();

  await interactions.keyboard("{Escape}");
  expect(screen.queryByRole("menu", { name: "账户菜单" })).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("shows the administrator-only action and opens settings through menu activation", async () => {
  const interactions = userEvent.setup();
  const onOpenSettings = vi.fn();
  const onOpenAdmin = vi.fn();
  render(<SidebarAccountMenu user={{ ...user, role: "admin", username: "admin" }} onOpenSettings={onOpenSettings} onOpenAdmin={onOpenAdmin} onSignOut={vi.fn()} />);

  await interactions.keyboard("{Tab}{Enter}");
  await interactions.click(screen.getByRole("menuitem", { name: "账户设置" }));
  expect(onOpenSettings).toHaveBeenCalledTimes(1);

  await interactions.click(screen.getByRole("button", { name: /admin.*账户菜单/i }));
  await interactions.click(screen.getByRole("menuitem", { name: "管理员管理" }));
  expect(onOpenAdmin).toHaveBeenCalledTimes(1);
});

test("keeps a long username discoverable after visual truncation", () => {
  const username = "very-long-user-name-for-code-review";
  render(<SidebarAccountMenu user={{ ...user, username }} onOpenSettings={vi.fn()} onOpenAdmin={vi.fn()} onSignOut={vi.fn()} />);
  expect(screen.getByText(username)).toHaveAttribute("title", username);
  expect(screen.getByRole("button", { name: `${username} 的账户菜单` })).toBeInTheDocument();
});

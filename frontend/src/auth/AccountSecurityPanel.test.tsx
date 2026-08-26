import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { AccountSecurityPanel } from "./AccountSecurityPanel";

const account = {
  user_id: "u1", username: "alice", role: "user" as const,
  is_active: true, must_change_password: false,
  password_changed_at: "2026-08-17T00:00:00Z", csrf_token: "csrf",
};

it("shows read-only account details and both logout actions", async () => {
  const api = { changePassword: vi.fn(), logout: vi.fn(), logoutAll: vi.fn() };
  render(<AccountSecurityPanel user={account} onSignedOut={vi.fn()} api={api} />);
  expect(screen.getByText("alice")).toBeInTheDocument();
  expect(screen.getByText("普通用户")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "退出所有设备" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "登录设备" }));
  expect(await screen.findByText(/单设备登录已启用/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "退出所有设备" }));
  expect(api.logoutAll).toHaveBeenCalledWith("csrf");
});

it("validates confirmation locally and sends current and new password", async () => {
  const api = { changePassword: vi.fn().mockResolvedValue({ ok: true }), logout: vi.fn(), logoutAll: vi.fn() };
  const signedOut = vi.fn();
  render(<AccountSecurityPanel user={account} onSignedOut={signedOut} forced api={api} />);
  await userEvent.type(screen.getByLabelText("当前密码"), "12345678");
  await userEvent.type(screen.getByLabelText("新密码"), "SafePass99");
  await userEvent.type(screen.getByLabelText("确认新密码"), "different");
  await userEvent.click(screen.getByRole("button", { name: "保存并重新登录" }));
  expect(api.changePassword).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("不一致");
  await userEvent.clear(screen.getByLabelText("确认新密码"));
  await userEvent.type(screen.getByLabelText("确认新密码"), "SafePass99");
  await userEvent.click(screen.getByRole("button", { name: "保存并重新登录" }));
  expect(api.changePassword).toHaveBeenCalledWith("12345678", "SafePass99", "csrf");
  expect(signedOut).toHaveBeenCalled();
});

it("uses account navigation to expose one profile, password, GitLab, or device section at a time", async () => {
  const api = { changePassword: vi.fn(), logout: vi.fn(), logoutAll: vi.fn() };
  render(<AccountSecurityPanel user={account} onSignedOut={vi.fn()} api={api} gitLabConnections={<div>GitLab test connection</div>} />);

  expect(screen.getByRole("navigation", { name: "账户设置导航" })).toBeVisible();
  expect(screen.getByText("alice")).toBeVisible();
  expect(screen.queryByText("GitLab test connection")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "退出所有设备" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "密码与安全" }));
  expect(screen.getByLabelText("当前密码")).toBeVisible();
  expect(screen.queryByText("alice")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "GitLab 连接" }));
  expect(screen.getByRole("region", { name: "GitLab 连接" })).toHaveTextContent("GitLab test connection");
  await userEvent.click(screen.getByRole("button", { name: "登录设备" }));
  expect(screen.getByRole("button", { name: "退出所有设备" })).toBeVisible();
});

it("does not expose account-settings navigation during forced password change", () => {
  const api = { changePassword: vi.fn(), logout: vi.fn(), logoutAll: vi.fn() };
  render(<AccountSecurityPanel user={account} forced onSignedOut={vi.fn()} api={api} />);

  expect(screen.queryByRole("navigation", { name: "账户设置导航" })).not.toBeInTheDocument();
});

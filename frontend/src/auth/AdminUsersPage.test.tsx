import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminUsersPage } from "./AdminUsersPage";

const admin = { user_id: "admin", username: "admin", role: "admin" as const, is_active: true, must_change_password: false, password_changed_at: "" };
const alice = { user_id: "u1", username: "alice", role: "user" as const, is_active: true, must_change_password: false, password_changed_at: "" };
const bob = { user_id: "u2", username: "bob", role: "user" as const, is_active: false, must_change_password: true, password_changed_at: "" };

function api() {
  return {
    listUsers: vi.fn().mockResolvedValue({ items: [admin, alice, bob], total: 23, page: 1, page_size: 20 }),
    stats: vi.fn().mockResolvedValue({ total: 23, active: 18, disabled: 5 }),
    createUser: vi.fn().mockResolvedValue({ ...alice, temporary_password: "12345678" }),
    disableUser: vi.fn().mockResolvedValue({ ...alice, is_active: false }),
    enableUser: vi.fn().mockResolvedValue({ ...bob, is_active: true }),
    resetPassword: vi.fn().mockResolvedValue({ temporary_password: "12345678" }),
    deleteUser: vi.fn().mockResolvedValue({ ok: true }),
    batchStatus: vi.fn().mockResolvedValue({ results: [{ user_id: "u1", ok: true }] }),
  };
}

describe("AdminUsersPage", () => {
  beforeEach(() => vi.restoreAllMocks());
  it("creates a normal user with the fixed temporary password and no password field", async () => {
    const mockApi = api();
    render(<AdminUsersPage csrfToken="csrf" api={mockApi} />);
    await userEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await userEvent.type(await screen.findByLabelText("用户名"), "new-user");
    await userEvent.click(screen.getByRole("button", { name: /create user/i }));
    expect(mockApi.createUser).toHaveBeenCalledWith({ username: "new-user" }, "csrf");
    expect(await screen.findByRole("status")).toHaveTextContent("12345678");
    expect(screen.queryByLabelText("临时密码")).not.toBeInTheDocument();
  });

  it("supports search, status filter, pagination and a horizontal-only user table container", async () => {
    const mockApi = api();
    render(<AdminUsersPage csrfToken="csrf" api={mockApi} />);
    expect(screen.queryByText("23")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "概览" }));
    expect(await screen.findByText("23")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "用户管理" }));
    expect(screen.getByTestId("user-table-scroll")).toHaveClass("auth-user-table-scroll");
    expect(screen.queryByTestId("user-scroll")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("搜索用户"), "ali");
    await userEvent.selectOptions(screen.getByLabelText("账号状态"), "active");
    await waitFor(() => expect(mockApi.listUsers).toHaveBeenLastCalledWith({ q: "ali", status: "active", page: 1, page_size: 20 }, "csrf"));
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(mockApi.listUsers).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }), "csrf"));
  });

  it("protects the sole administrator and permanently deletes a confirmed regular user", async () => {
    const mockApi = api(); render(<AdminUsersPage csrfToken="csrf" api={mockApi} />);
    await screen.findByText("alice"); expect(screen.getByText("唯一管理员")).toBeInTheDocument(); expect(screen.queryByTestId("delete-admin")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "删除 alice" })); expect(screen.getByRole("dialog")).toHaveTextContent("审查记录");
    await userEvent.type(screen.getByLabelText("输入用户名确认"), "alice"); await userEvent.click(screen.getByRole("button", { name: "确认永久删除" }));
    await waitFor(() => expect(mockApi.deleteUser).toHaveBeenCalledWith("u1", "csrf"));
  });

  it("batch disables selected regular users and sends CSRF through the client contract", async () => {
    const mockApi = api(); render(<AdminUsersPage csrfToken="csrf" api={mockApi} />);
    await userEvent.click(await screen.findByRole("checkbox", { name: "选择 alice" })); await userEvent.click(screen.getByRole("button", { name: "批量停用" }));
    expect(mockApi.batchStatus).toHaveBeenCalledWith(["u1"], false, "csrf");
  });

  it("uses a desktop navigation pane and keeps 50 users in the main document flow", async () => {
    const users = Array.from({ length: 50 }, (_, index) => ({ ...alice, user_id: `user-${index}`, username: `user-${index}` }));
    const mockApi = {
      ...api(),
      listUsers: vi.fn().mockImplementation(async ({ page_size }: { page_size: number }) => ({
        items: page_size === 50 ? users : [admin, alice, bob], total: page_size === 50 ? 50 : 23, page: 1, page_size,
      })),
    };
    render(<AdminUsersPage csrfToken="csrf" api={mockApi} />);
    expect(await screen.findByRole("navigation", { name: "账号管理导航" })).toBeVisible();
    expect(screen.getByRole("button", { name: "概览" })).toBeVisible(); expect(screen.getByRole("button", { name: "用户管理" })).toBeVisible(); expect(screen.getByRole("button", { name: "创建用户" })).toBeVisible();
    await userEvent.selectOptions(screen.getByLabelText("每页数量"), "50");
    await waitFor(() => expect(mockApi.listUsers).toHaveBeenLastCalledWith(expect.objectContaining({ page_size: 50 }), "csrf"));
    expect(await screen.findByText("user-49")).toBeVisible(); expect(screen.queryByTestId("user-scroll")).not.toBeInTheDocument();
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";
const me = vi.fn();
vi.mock("../api/authClient", () => ({
  getCurrentUser: () => me(), installAuthenticatedFetch: vi.fn(), login: vi.fn(), logout: vi.fn().mockResolvedValue(undefined), logoutAll: vi.fn(), listAuthSessions: vi.fn(), changePassword: vi.fn(),
  adminApi: { listUsers: vi.fn().mockResolvedValue([]), createUser: vi.fn(), disableUser: vi.fn(), enableUser: vi.fn(), resetPassword: vi.fn() },
}));
describe("AuthGate", () => {
  beforeEach(() => me.mockReset());
  it("forces password change before any role route", async () => {
    me.mockResolvedValue({ user_id: "u1", username: "user", role: "user", is_active: true, password_changed_at: "2026-08-17T00:00:00Z", must_change_password: true, csrf_token: "csrf" });
    render(<AuthGate app={<div>review app</div>} />);
    expect(await screen.findByLabelText("账户安全")).toBeInTheDocument();
    expect(screen.getByText("首次登录，请修改密码")).toBeInTheDocument();
    expect(screen.queryByText("review app")).not.toBeInTheDocument();
  });
  it("routes an administrator to account management", async () => {
    me.mockResolvedValue({ user_id: "admin", username: "admin", role: "admin", is_active: true, password_changed_at: "2026-08-17T00:00:00Z", must_change_password: false, csrf_token: "csrf" });
    render(<AuthGate app={<div>review app</div>} />);
    expect(await screen.findByRole("heading", { name: /account management/i })).toBeInTheDocument();
    expect(screen.queryByText("review app")).not.toBeInTheDocument();
  });
  it("routes a regular user to review without the retired top account controls", async () => {
    me.mockResolvedValue({ user_id: "user", username: "user", role: "user", is_active: true, password_changed_at: "2026-08-17T00:00:00Z", must_change_password: false, csrf_token: "csrf" });
    render(<AuthGate app={<div>review app</div>} />);
    expect(await screen.findByText("review app")).toBeInTheDocument();
    expect(document.querySelector(".auth-userbar")).toBeNull();
    expect(screen.queryByRole("button", { name: /账户安全/ })).not.toBeInTheDocument();
  });
  it("renders the shared product brand on the sign-in screen", async () => {
    me.mockResolvedValue({ user_id: "admin", username: "admin", role: "admin", is_active: true, password_changed_at: "2026-08-17T00:00:00Z", must_change_password: false, csrf_token: "csrf" });
    const user = userEvent.setup();
    render(<AuthGate app={<div>review app</div>} />);
    await user.click(await screen.findByRole("button", { name: "退出登录" }));
    expect(await screen.findByText("CodeAstra")).toBeVisible();
    expect(screen.getByText("星鉴")).toBeVisible();
  });
});

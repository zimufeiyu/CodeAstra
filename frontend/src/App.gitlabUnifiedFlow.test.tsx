import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import App from "./App";
import { verifyGitLabAccount } from "./api/client";
import { AuthSessionProvider } from "./auth/AuthSessionContext";

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    getModelProfiles: vi.fn().mockResolvedValue([{
      profile_id: "local-qwen3-8b",
      provider: "local",
      model: "Qwen3-8B",
      display_name: "本地 Qwen3-8B",
      available: true,
      unavailable_reason: null,
      supports_json: true,
      context_tokens: 40960,
    }]),
    getInstanceHealth: vi.fn().mockResolvedValue({
      instances: [{
        endpoint_id: "ppu-0",
        inflight_requests: 0,
        inflight_tokens: 0,
        circuit_open: false,
      }],
    }),
    listReviewSessions: vi.fn().mockResolvedValue({ items: [], limit: 100, offset: 0 }),
    verifyGitLabAccount: vi.fn().mockResolvedValue({
      gitlab_host: "https://gitlab.cigai.cn:1443",
      user_id: 42,
      username: "reviewer",
      name: "Code Reviewer",
      avatar_url: null,
      web_url: "https://gitlab.cigai.cn:1443/reviewer",
    }),
  };
});

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
});

function SignedInApp() {
  const [accountSettingsOpen, setAccountSettingsOpen] = useState(false);
  return (
    <AuthSessionProvider value={{
      user: { user_id: "user-1", username: "reviewer", role: "user", is_active: true, must_change_password: false, password_changed_at: "2026-08-17T00:00:00Z", csrf_token: "csrf" },
      accountSettingsOpen,
      openAccountSettings: () => setAccountSettingsOpen(true),
      closeAccountSettings: () => setAccountSettingsOpen(false),
      openAdminManagement: () => undefined,
      signOut: () => undefined,
    }}>
      <App />
    </AuthSessionProvider>
  );
}

test("connects from GitLab import and returns with the MR address and account selected", async () => {
  const user = userEvent.setup();
  render(<SignedInApp />);

  await user.click(screen.getByRole("button", { name: "添加内容" }));
  await user.click(screen.getByRole("menuitem", { name: /从 GitLab 导入/ }));
  const mergeRequestUrl =
    "https://gitlab.cigai.cn:1443/group/project/-/merge_requests/12";
  await user.type(screen.getByLabelText("合并请求地址"), mergeRequestUrl);
  await user.click(screen.getByRole("button", { name: "连接 GitLab 并继续" }));

  expect(screen.getByRole("region", { name: "账户安全" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "GitLab 连接" })).toBeInTheDocument();
  await user.clear(screen.getByLabelText("GitLab 地址"));
  await user.type(
    screen.getByLabelText("GitLab 地址"),
    "https://gitlab.cigai.cn:1443/",
  );
  await user.type(screen.getByLabelText("个人访问令牌"), "saved-token");
  await user.click(screen.getByRole("button", { name: "验证并保存" }));

  await waitFor(() => {
    expect(
      screen.getByRole("heading", { name: "从 GitLab 导入代码" }),
    ).toBeInTheDocument();
  });
  expect(screen.getByLabelText("合并请求地址")).toHaveValue(mergeRequestUrl);
  expect(
    screen.getByText("使用 @reviewer 读取 https://gitlab.cigai.cn:1443"),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("访问令牌（私有项目需要）")).not.toBeInTheDocument();
  expect(verifyGitLabAccount).toHaveBeenCalledWith(
    "https://gitlab.cigai.cn:1443/",
    "saved-token",
  );
});

test("opens account settings from the sidebar menu while hiding the review shell", async () => {
  const user = userEvent.setup();
  render(<SignedInApp />);

  await user.click(screen.getByRole("button", { name: "reviewer 的账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "账户设置" }));

  expect(document.querySelector(".app-shell")).not.toBeVisible();
  expect(screen.getByRole("region", { name: "账户安全" })).toBeVisible();
  expect(within(screen.getByRole("region", { name: "账户安全" })).getByText("reviewer")).toBeVisible();
  expect(screen.queryByRole("heading", { name: "GitLab 连接" })).not.toBeInTheDocument();
  await user.click(within(screen.getByRole("navigation", { name: "账户设置导航" })).getByRole("button", { name: "GitLab 连接" }));
  expect(screen.getByRole("heading", { name: "GitLab 连接" })).toBeVisible();
  expect(screen.getByRole("region", { name: "GitLab 连接" })).toBeVisible();
});

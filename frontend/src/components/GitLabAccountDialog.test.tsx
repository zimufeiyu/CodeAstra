import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import * as client from "../api/client";
import { GitLabAccountDialog } from "./GitLabAccountDialog";

test("verifies and saves a GitLab account", async () => {
  vi.spyOn(client, "verifyGitLabAccount").mockResolvedValue({
    gitlab_host: "https://gitlab.example.com",
    user_id: 42,
    username: "reviewer",
    name: "Code Reviewer",
  });
  const onSave = vi.fn();
  render(
    <GitLabAccountDialog
      open
      accounts={[]}
      activeAccountId={null}
      onClose={() => undefined}
      onSave={onSave}
      onActivate={() => undefined}
      onDelete={() => undefined}
    />,
  );

  await userEvent.clear(screen.getByLabelText("GitLab 地址"));
  await userEvent.type(screen.getByLabelText("GitLab 地址"), "https://gitlab.example.com");
  await userEvent.type(screen.getByLabelText("个人访问令牌"), "saved-token");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));

  await waitFor(() => expect(onSave).toHaveBeenCalled());
  expect(client.verifyGitLabAccount).toHaveBeenCalledWith(
    "https://gitlab.example.com",
    "saved-token",
  );
  expect(onSave.mock.calls[0][0]).toMatchObject({
    username: "reviewer",
    private_token: "saved-token",
  });
});

test("shows how to create a least-privilege personal access token", () => {
  render(
    <GitLabAccountDialog
      open
      accounts={[]}
      activeAccountId={null}
      onClose={() => undefined}
      onSave={() => undefined}
      onActivate={() => undefined}
      onDelete={() => undefined}
    />,
  );

  expect(screen.getByRole("heading", { name: "如何获取个人访问令牌" })).toBeInTheDocument();
  expect(screen.getByText("read_api")).toBeInTheDocument();
  expect(screen.getByText("read_repository")).toBeInTheDocument();
  expect(screen.getByText(/不要勾选 api、write_repository 或管理员权限/)).toBeInTheDocument();
  const officialLink = screen.getByRole("link", { name: "查看 GitLab 官方说明" });
  expect(officialLink).toHaveAttribute(
    "href",
    "https://docs.gitlab.com/user/profile/personal_access_tokens/",
  );
  expect(officialLink).toHaveAttribute("target", "_blank");
  expect(officialLink).toHaveAttribute("rel", "noreferrer");
});

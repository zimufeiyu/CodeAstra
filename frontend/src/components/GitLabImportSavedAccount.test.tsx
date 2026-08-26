import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import * as client from "../api/client";
import { GitLabImportDialog } from "./GitLabImportDialog";

test("uses a saved GitLab account token when previewing an MR", async () => {
  vi.spyOn(client, "previewGitLabMergeRequest").mockResolvedValue({
    gitlab_host: "https://gitlab.example.com",
    project_id: 7,
    project_path: "group/project",
    merge_request_iid: 12,
    title: "Improve parser",
    web_url: "https://gitlab.example.com/group/project/-/merge_requests/12",
    base_sha: "base",
    head_sha: "head",
    files: [],
  });
  render(
    <GitLabImportDialog
      open
      accounts={[{
        account_id: "account-1",
        gitlab_host: "https://gitlab.example.com",
        user_id: 42,
        username: "reviewer",
        name: "Code Reviewer",
        private_token: "saved-token",
        saved_at: "2026-08-09T00:00:00Z",
      }]}
      activeAccountId="account-1"
      onClose={() => undefined}
      onImport={() => undefined}
    />,
  );

  await userEvent.type(
    screen.getByLabelText("合并请求地址"),
    "https://gitlab.example.com/group/project/-/merge_requests/12",
  );
  await userEvent.click(screen.getByRole("button", { name: "读取合并请求" }));

  await waitFor(() => expect(client.previewGitLabMergeRequest).toHaveBeenCalledWith(
    "https://gitlab.example.com/group/project/-/merge_requests/12",
    "saved-token",
    expect.any(AbortSignal),
  ));
  expect(screen.queryByLabelText("访问令牌（旧版兼容方式）")).not.toBeInTheDocument();
});

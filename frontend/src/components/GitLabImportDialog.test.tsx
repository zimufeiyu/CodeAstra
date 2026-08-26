import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import * as client from "../api/client";
import * as oauth from "../utils/gitlabOAuth";
import { GitLabImportDialog } from "./GitLabImportDialog";

afterEach(() => {
  vi.restoreAllMocks();
});

test("previews a merge request and imports selected files", async () => {
  const preview = {
    gitlab_host: "https://gitlab.example.com",
    project_id: 7,
    project_path: "group/project",
    merge_request_iid: 12,
    title: "修复解析器",
    web_url: "https://gitlab.example.com/group/project/-/merge_requests/12",
    base_sha: "111111111111",
    head_sha: "222222222222",
    files: [{
      old_path: "src/parser.py",
      new_path: "src/parser.py",
      change_type: "modified" as const,
      language: "python" as const,
      old_content: "value = 1\n",
      new_content: "value = 2\n",
      diff: "@@ -1 +1 @@",
      changed_ranges: [{ start_line: 1, end_line: 1 }],
      diff_truncated: false,
      selectable: true,
      unavailable_reason: null,
    }],
  };
  vi.spyOn(client, "previewGitLabMergeRequest").mockResolvedValue(preview);
  const onImport = vi.fn();
  const user = userEvent.setup();

  render(<GitLabImportDialog open onClose={() => undefined} onImport={onImport} />);
  await user.type(screen.getByLabelText("合并请求地址"), preview.web_url);
  await user.click(screen.getByRole("button", { name: "读取合并请求" }));

  expect(await screen.findByText("修复解析器")).toBeInTheDocument();
  expect(screen.getByText("value = 1")).toBeInTheDocument();
  expect(screen.getByText("value = 2")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "添加到审查" }));
  expect(onImport).toHaveBeenCalledWith(preview, [preview.files[0]]);
});


test("aborts the request and clears the token when the dialog closes", async () => {
  let capturedSignal: AbortSignal | undefined;
  vi.spyOn(client, "previewGitLabMergeRequest").mockImplementation(
    async (_url, _token, signal) => {
      capturedSignal = signal;
      return await new Promise((_, reject) => {
        signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    },
  );
  const user = userEvent.setup();
  const props = {
    onClose: () => undefined,
    onImport: () => undefined,
  };
  const { rerender } = render(<GitLabImportDialog open {...props} />);

  await user.type(
    screen.getByLabelText("合并请求地址"),
    "https://gitlab.example.com/group/project/-/merge_requests/12",
  );
  await user.type(screen.getByLabelText("访问令牌（旧版兼容方式）"), "secret");
  await user.click(screen.getByRole("button", { name: "读取合并请求" }));
  expect(capturedSignal?.aborted).toBe(false);

  rerender(<GitLabImportDialog open={false} {...props} />);
  expect(capturedSignal?.aborted).toBe(true);

  rerender(<GitLabImportDialog open {...props} />);
  await waitFor(() => {
    expect(screen.getByLabelText("访问令牌（旧版兼容方式）")).toHaveValue("");
  });
});

test("rejects a branch tree URL before the legacy preview path", async () => {
  const legacyPreview = vi.spyOn(client, "previewGitLabMergeRequest");
  const user = userEvent.setup();

  render(<GitLabImportDialog open onClose={() => undefined} onImport={() => undefined} />);
  await user.type(
    screen.getByLabelText("合并请求地址"),
    "https://gitlab.cigai.cn:1443/lhy/crowd-sim/-/tree/main",
  );
  await user.click(screen.getByRole("button", { name: "读取合并请求" }));

  expect(await screen.findByText(/这是 GitLab 分支树地址，不是 Merge Request 地址/)).toBeInTheDocument();
  expect(legacyPreview).not.toHaveBeenCalled();
});

test("keeps OAuth prominent beside a saved PAT account", () => {
  vi.spyOn(oauth, "gitLabOAuthConfigured").mockReturnValue(true);
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
      onOAuthConnect={() => undefined}
      onClose={() => undefined}
      onImport={() => undefined}
    />,
  );

  expect(screen.getByRole("button", { name: "连接 GitLab（推荐）" })).toBeInTheDocument();
  expect(screen.getByText(/旧 PAT 账户属于其他连接方式/)).toBeInTheDocument();
});

test("explains when the administrator has not configured OAuth", () => {
  vi.spyOn(oauth, "gitLabOAuthConfigured").mockReturnValue(false);
  render(
    <GitLabImportDialog
      open
      onOAuthConnect={() => undefined}
      onClose={() => undefined}
      onImport={() => undefined}
    />,
  );

  expect(screen.getByRole("alert")).toHaveTextContent("管理员尚未配置 GitLab OAuth");
});

test("browses current user projects, branches and merge requests before selecting a file", async () => {
  vi.spyOn(oauth, "getGitLabCurrentUser").mockResolvedValue({ id: 4, username: "lixun", name: "测试用户" });
  vi.spyOn(oauth, "listGitLabProjects").mockResolvedValue([{ id: 7, path_with_namespace: "group/project", name: "project", default_branch: "main", web_url: "https://gitlab.example.com/group/project" }]);
  vi.spyOn(oauth, "listGitLabBranches").mockResolvedValue([{ name: "main", web_url: "https://gitlab.example.com/group/project/-/tree/main" }]);
  vi.spyOn(oauth, "listGitLabMergeRequests").mockResolvedValue([{ iid: 12, title: "修复解析器", state: "opened", source_branch: "fix", target_branch: "main", web_url: "https://gitlab.example.com/group/project/-/merge_requests/12", updated_at: "2026-08-24T00:00:00Z" }]);
  const preview = { gitlab_host: "https://gitlab.example.com", project_id: 7, project_path: "group/project", merge_request_iid: 12, title: "修复解析器", web_url: "https://gitlab.example.com/group/project/-/merge_requests/12", base_sha: "base", head_sha: "head", files: [{ old_path: "src/parser.py", new_path: "src/parser.py", change_type: "modified" as const, language: "python" as const, old_content: "a = 1\n", new_content: "a = 2\n", diff: "@@", changed_ranges: [], diff_truncated: false, selectable: true, unavailable_reason: null }] };
  vi.spyOn(oauth, "previewGitLabProjectMergeRequest").mockResolvedValue(preview);
  const user = userEvent.setup();
  render(<GitLabImportDialog open oauthToken="session-token" onClose={() => undefined} onImport={() => undefined} />);
  expect(await screen.findByText(/当前用户：测试用户/)).toBeInTheDocument();
  expect(screen.getByLabelText("搜索项目")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /group\/project/ }));
  expect(await screen.findByLabelText("搜索 Merge Request")).toBeInTheDocument();
    expect(screen.getByText(/分支：main/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /修复解析器/ }));
    expect((await screen.findAllByText("src/parser.py")).length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText(/修改前/)).toBeInTheDocument();
  expect(screen.getByText("a = 1")).toBeInTheDocument();
  expect(screen.getByText(/结构摘要：/)).toBeInTheDocument();
});

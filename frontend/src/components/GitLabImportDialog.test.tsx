import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import * as client from "../api/client";
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
  await user.type(screen.getByLabelText("访问令牌（私有项目需要）"), "secret");
  await user.click(screen.getByRole("button", { name: "读取合并请求" }));
  expect(capturedSignal?.aborted).toBe(false);

  rerender(<GitLabImportDialog open={false} {...props} />);
  expect(capturedSignal?.aborted).toBe(true);

  rerender(<GitLabImportDialog open {...props} />);
  await waitFor(() => {
    expect(screen.getByLabelText("访问令牌（私有项目需要）")).toHaveValue("");
  });
});

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import App from "./App";
import { cancelReviewSession, createReviewSession, getInstanceHealth, listReviewSessions, resumeReviewSession, streamReviewSession } from "./api/client";

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    getInstanceHealth: vi.fn(),
    listReviewSessions: vi.fn(),
    createReviewSession: vi.fn(),
    streamReviewSession: vi.fn(),
    cancelReviewSession: vi.fn(),
    resumeReviewSession: vi.fn(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
  vi.mocked(getInstanceHealth).mockResolvedValue({ instances: [] });
  vi.mocked(listReviewSessions).mockResolvedValue({ items: [], limit: 100, offset: 0 });
  vi.mocked(createReviewSession).mockResolvedValue({
    review_id: "review-1",
    status: "queued",
    expires_at: "2026-08-04T00:00:00+00:00",
  });
  vi.mocked(streamReviewSession).mockResolvedValue({
    review_id: "review-1",
    title: "代码审查 · 2026-08-03 00:00",
    mode: "paste",
    status: "completed",
    model: {
      profile_id: "local-qwen3-8b",
      provider: "local" as const,
      model: "Qwen3-8B",
      display_name: "Local Qwen3-8B",
    },
    created_at: "2026-08-03T00:00:00+00:00",
    expires_at: "2026-08-04T00:00:00+00:00",
    files: [{
      file_id: "file-1",
      relative_path: "snippet.py",
      language: "python",
      content: "eval(user_input)\n",
      sha256: "a".repeat(64),
      line_offsets: [0, 17],
    }],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "\u5ba1\u67e5\u5b8c\u6210" },
  });
});


it("keeps the failed review resumable while isolating and restoring the draft", async () => {
  const failed = {
    review_id: "review-1",
    title: "代码审查 · 2026-08-03 00:00",
    mode: "paste" as const,
    status: "failed" as const,
    model: {
      profile_id: "local-qwen3-8b",
      provider: "local" as const,
      model: "Qwen3-8B",
      display_name: "Local Qwen3-8B",
    },
    created_at: "2026-08-03T00:00:00+00:00",
    expires_at: "2026-08-04T00:00:00+00:00",
    files: [],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "未完成" },
    error: "部分分块失败",
  };
  vi.mocked(streamReviewSession).mockReset().mockResolvedValueOnce(failed).mockResolvedValueOnce({
    ...failed,
    status: "completed",
    error: null,
    summary: { ...failed.summary, text: "审查完成" },
  });
  const user = userEvent.setup();
  render(<App />);
  const composer = screen.getByRole("textbox", { name: "输入需要审查的代码" });

  await user.type(composer, "eval(user_input)");
  await user.click(screen.getByRole("button", { name: "发送审查" }));

  expect(await screen.findByRole("button", { name: "重新复查" })).toBeInTheDocument();
  expect(composer).toBeDisabled();
  expect(composer).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "重新复查" }));

  await waitFor(() => expect(resumeReviewSession).toHaveBeenCalledWith("review-1"));
  await waitFor(() => expect(streamReviewSession).toHaveBeenCalledTimes(2));
  await user.click(screen.getByRole("button", { name: "新建审查" }));
  expect(composer).toBeEnabled();
  expect(composer).toHaveValue("eval(user_input)");
});

it("uses one chat-style composer instead of separate entry modes", async () => {
  const user = userEvent.setup();
  render(<App />);

  expect(screen.queryByRole("button", { name: "\u590d\u5236\u4ee3\u7801" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "\u5355\u6587\u4ef6" })).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "\u8f93\u5165\u9700\u8981\u5ba1\u67e5\u7684\u4ee3\u7801" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "添加内容" }));
  expect(screen.getByRole("menuitem", { name: /选择本地文件/ })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /本地版本对比/ })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /从 GitLab 导入/ })).toBeInTheDocument();

  await user.type(screen.getByRole("textbox", { name: "\u8f93\u5165\u9700\u8981\u5ba1\u67e5\u7684\u4ee3\u7801" }), "eval(user_input)");
  await user.click(screen.getByRole("button", { name: "\u53d1\u9001\u5ba1\u67e5" }));

  expect(await screen.findByLabelText("\u5b8c\u6574\u4ee3\u7801 snippet.py")).toBeInTheDocument();
  expect(createReviewSession).toHaveBeenCalledWith(
    "paste",
    expect.objectContaining({ language: "python", content: "eval(user_input)" }),
  );
});

it("shows uploaded files in the composer and allows removing them", async () => {
  const user = userEvent.setup();
  render(<App />);
  const file = new File(["#include <iostream>\nint main() {}"], "main.cpp", { type: "text/plain" });

  await user.upload(screen.getByLabelText("\u6dfb\u52a0\u4ee3\u7801\u6587\u4ef6"), file);

  expect(await screen.findByText("main.cpp")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "\u5220\u9664 main.cpp" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "\u5220\u9664 main.cpp" }));
  expect(screen.queryByText("main.cpp")).not.toBeInTheDocument();
});

it("rejects an oversized upload before reading it into the review draft", async () => {
  const user = userEvent.setup();
  render(<App />);
  const file = new File(["x".repeat(2 * 1024 * 1024 + 1)], "large.py", {
    type: "text/plain",
  });

  await user.upload(screen.getByLabelText("添加代码文件"), file);

  expect(await screen.findByText("large.py：单个代码文件不能超过 2 MiB。"))
    .toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除 large.py" })).not.toBeInTheDocument();
  expect(createReviewSession).not.toHaveBeenCalled();
});


it("cancels a review stopped before the create response arrives", async () => {
  let resolveCreate: ((value: { review_id: string; status: string; expires_at: string }) => void) | undefined;
  vi.mocked(createReviewSession).mockReturnValue(
    new Promise((resolve) => {
      resolveCreate = resolve;
    }),
  );
  const user = userEvent.setup();
  render(<App />);

  await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(user_input)");
  await user.click(screen.getByRole("button", { name: "发送审查" }));
  await user.click(await screen.findByRole("button", { name: /停止生成/ }));

  await act(async () => {
    resolveCreate?.({
      review_id: "review-late",
      status: "queued",
      expires_at: "2026-08-04T00:00:00+00:00",
    });
  });

  await waitFor(() => expect(cancelReviewSession).toHaveBeenCalledWith("review-late"));
  expect(streamReviewSession).not.toHaveBeenCalled();
});

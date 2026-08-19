import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import App from "./App";
import {
  askReviewFollowup,
  createReviewSession,
  getInstanceHealth,
  getModelProfiles,
  getReviewFollowups,
  getReviewSession,
  listReviewSessions,
  streamReviewSession,
} from "./api/client";

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    askReviewFollowup: vi.fn(),
    createReviewSession: vi.fn(),
    getInstanceHealth: vi.fn(),
    getModelProfiles: vi.fn(),
    getReviewFollowups: vi.fn(),
    getReviewSession: vi.fn(),
    listReviewSessions: vi.fn(),
    streamReviewSession: vi.fn(),
  };
});

function completedSession(reviewId: string, title: string, content: string) {
  return {
    review_id: reviewId,
    title,
    mode: "paste" as const,
    status: "completed" as const,
    model: {
      profile_id: "local-qwen3-8b",
      provider: "local" as const,
      model: "Qwen3-8B",
      display_name: "Local Qwen3-8B",
    },
    created_at: "2026-08-05T08:00:00+00:00",
    expires_at: "2026-08-06T08:00:00+00:00",
    files: [{
      file_id: `file-${reviewId}`,
      relative_path: `${reviewId}.py`,
      language: "python" as const,
      content,
      sha256: "a".repeat(64),
      line_offsets: [0],
    }],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "审查完成。" },
  };
}

function historyItem(reviewId: string, title: string, status: "queued" | "completed") {
  return {
    review_id: reviewId,
    title,
    mode: "paste" as const,
    status,
    created_at: "2026-08-05T08:00:00+00:00",
    expires_at: "2026-08-06T08:00:00+00:00",
    file_count: 1,
    file_names: [`${reviewId}.py`],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: status === "completed" ? "审查完成。" : "等待审查。" },
    error: null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getInstanceHealth).mockResolvedValue({ instances: [] });
  vi.mocked(getModelProfiles).mockResolvedValue([
    {
      profile_id: "local-qwen3-8b",
      provider: "local",
      model: "Qwen3-8B",
      display_name: "Local Qwen3-8B",
      available: true,
      unavailable_reason: null,
      supports_json: true,
      context_tokens: 40960,
    },
    {
      profile_id: "deepseek-api",
      provider: "deepseek",
      model: "deepseek-v4-flash",
      display_name: "DeepSeek API",
      available: false,
      unavailable_reason: "Server API key is not configured.",
      supports_json: true,
      context_tokens: 1000000,
    },
  ]);
  vi.mocked(getReviewFollowups).mockResolvedValue([]);
});

it("adds a created review to history immediately and keeps another history session visible", async () => {
  const oldSession = completedSession("old", "旧审查", "print('old')\n");
  const newSession = completedSession("new", "新审查", "print('new')\n");
  const oldHistory = historyItem("old", "旧审查", "completed");
  const newHistory = historyItem("new", "新审查", "queued");
  vi.mocked(listReviewSessions)
    .mockResolvedValueOnce({ items: [oldHistory], limit: 100, offset: 0 })
    .mockResolvedValue({ items: [newHistory, oldHistory], limit: 100, offset: 0 });
  vi.mocked(createReviewSession).mockResolvedValue({
    review_id: "new",
    status: "queued",
    expires_at: newSession.expires_at,
  });
  vi.mocked(getReviewSession).mockResolvedValue(oldSession);
  let finishReview: ((session: typeof newSession) => void) | undefined;
  vi.mocked(streamReviewSession).mockImplementation(
    () => new Promise((resolve) => { finishReview = resolve; }),
  );

  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("button", { name: "旧审查" });
  await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "print('new')");
  await user.click(screen.getByRole("button", { name: "发送审查" }));

  expect(await screen.findByRole("button", { name: "新审查" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "旧审查" }));
  expect(await screen.findByText("print('old')")).toBeInTheDocument();
  expect(screen.queryByLabelText("模型正在生成")).not.toBeInTheDocument();

  finishReview?.(newSession);
  await waitFor(() => expect(streamReviewSession).toHaveBeenCalledWith("new", expect.any(Function), expect.any(AbortSignal)));
  await waitFor(() => expect(screen.getByText("print('old')")).toBeInTheDocument());
});

it("does not steal focus when creation finishes after the user opens history", async () => {
  const oldSession = completedSession("old", "旧审查", "print('old')\n");
  const oldHistory = historyItem("old", "旧审查", "completed");
  const newHistory = historyItem("new", "新审查", "queued");
  vi.mocked(listReviewSessions)
    .mockResolvedValueOnce({ items: [oldHistory], limit: 100, offset: 0 })
    .mockResolvedValue({ items: [newHistory, oldHistory], limit: 100, offset: 0 });
  vi.mocked(getReviewSession).mockResolvedValue(oldSession);
  let resolveCreate: ((created: { review_id: string; status: string; expires_at: string }) => void) | undefined;
  vi.mocked(createReviewSession).mockImplementation(
    () => new Promise((resolve) => { resolveCreate = resolve; }),
  );
  vi.mocked(streamReviewSession).mockImplementation(() => new Promise(() => undefined));

  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("button", { name: "旧审查" });
  await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "print('new')");
  await user.click(screen.getByRole("button", { name: "发送审查" }));
  await waitFor(() => expect(createReviewSession).toHaveBeenCalled());

  await user.click(screen.getByRole("button", { name: "旧审查" }));
  expect(await screen.findByText("print('old')")).toBeInTheDocument();

  resolveCreate?.({
    review_id: "new",
    status: "queued",
    expires_at: "9999-12-31T23:59:59.999999+00:00",
  });
  await waitFor(() => expect(streamReviewSession).toHaveBeenCalled());
  expect(screen.getByText("print('old')")).toBeInTheDocument();
  expect(screen.queryByLabelText("模型正在生成")).not.toBeInTheDocument();
});
it("keeps a delayed follow-up answer with its source review", async () => {
  const finding = {
    finding_id: "finding-old",
    source: "static" as const,
    analyzer: "python-ast",
    rule_id: "python.eval",
    category: "security",
    severity: "high" as const,
    confidence: 1,
    file_id: "file-old",
    start_line: 1,
    start_column: 1,
    end_line: 1,
    end_column: 12,
    title: "测试问题",
    hover_summary: "测试问题摘要",
    detail: "测试问题详情",
    evidence: "eval(value)",
    impact: "影响",
    suggestion: "建议",
    verification: {
      range_valid: true,
      evidence_matched: true,
      static_confirmed: true,
      cross_file_checked: false,
      deduplicated: false,
    },
  };
  const oldSession = { ...completedSession("old", "旧审查", "eval(value)\n"), findings: [finding] };
  const otherSession = completedSession("other", "其他审查", "print('other')\n");
  const oldHistory = historyItem("old", "旧审查", "completed");
  const otherHistory = historyItem("other", "其他审查", "completed");
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [oldHistory, otherHistory],
    limit: 100,
    offset: 0,
  });
  vi.mocked(getReviewSession).mockImplementation(async (reviewId) =>
    reviewId === "old" ? oldSession : otherSession
  );
  let resolveFollowup: ((messages: Array<{
    message_id: string;
    review_id: string;
    role: "user" | "assistant";
    content: string;
    created_at: string;
  }>) => void) | undefined;
  vi.mocked(askReviewFollowup).mockImplementation(
    () => new Promise((resolve) => { resolveFollowup = resolve; }),
  );

  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "旧审查" }));
  await user.click(await screen.findByRole("button", { name: "测试问题，第 1 行" }));
  await user.type(screen.getByRole("textbox", { name: "针对当前代码追问" }), "为什么？");
  await user.click(screen.getByRole("button", { name: "发送追问" }));
  await waitFor(() => expect(askReviewFollowup).toHaveBeenCalled());

  await user.click(screen.getByRole("button", { name: "其他审查" }));
  expect(await screen.findByText("print('other')")).toBeInTheDocument();
  resolveFollowup?.([
    {
      message_id: "question-old",
      review_id: "old",
      role: "user",
      content: "为什么？",
      created_at: "2026-08-05T08:10:00+00:00",
    },
    {
      message_id: "answer-old",
      review_id: "old",
      role: "assistant",
      content: "这是旧审查的回答",
      created_at: "2026-08-05T08:10:01+00:00",
    },
  ]);
  await waitFor(() => expect(askReviewFollowup).toHaveBeenCalledTimes(1));
  expect(screen.queryByText("这是旧审查的回答")).not.toBeInTheDocument();
  expect(screen.getByText("print('other')")).toBeInTheDocument();
});

it("restored DeepSeek history uses its persisted profile availability", async () => {
  const deepseekSession = {
    ...completedSession("deepseek", "DeepSeek review", "print('api')\n"),
    model: {
      profile_id: "deepseek-api",
      provider: "deepseek" as const,
      model: "deepseek-v4-flash",
      display_name: "DeepSeek API",
    },
  };
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [historyItem("deepseek", "DeepSeek review", "completed")],
    limit: 100,
    offset: 0,
  });
  vi.mocked(getReviewSession).mockResolvedValue(deepseekSession);

  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "DeepSeek review" }));

  expect(await screen.findByText("由 DeepSeek API 提供审查支持")).toBeInTheDocument();
  expect(screen.getByLabelText("\u6a21\u578b\u5065\u5eb7\u72b6\u6001")).toHaveTextContent("\u672a\u914d\u7f6e");
  expect(screen.getByRole("combobox", { name: "\u5ba1\u67e5\u6a21\u578b" })).toHaveValue(
    "local-qwen3-8b",
  );
});

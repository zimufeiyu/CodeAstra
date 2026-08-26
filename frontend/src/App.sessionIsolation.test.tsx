import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import App, { parseReviewRoute } from "./App";
import {
  askReviewFollowup,
  createReviewSession,
  getInstanceHealth,
  getModelProfiles,
  getReviewFollowups,
  getReviewSession,
  listReviewSessions,
  resumeReviewSession,
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
    resumeReviewSession: vi.fn(),
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
  window.history.replaceState({}, "", "/");
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

it("parses review and finding routes without accepting unrelated paths", () => {
  expect(parseReviewRoute("/reviews/review-1/findings/finding-2")).toEqual({ reviewId: "review-1", findingId: "finding-2" });
  expect(parseReviewRoute("/settings")).toBeNull();
});

it("loads 101 history records in 50-item pages without clearing the current selection", async () => {
  const records = Array.from({ length: 101 }, (_, index) => (
    historyItem(`paged-${index}`, `分页审查 ${index}`, "completed")
  ));
  vi.mocked(listReviewSessions).mockImplementation(async (limit, offset) => ({
    items: records.slice(offset ?? 0, (offset ?? 0) + (limit ?? 20)),
    limit: limit ?? 20,
    offset: offset ?? 0,
  }));
  vi.mocked(getReviewSession).mockResolvedValue(
    completedSession("paged-0", "分页审查 0", "print('page')\n"),
  );
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: "分页审查 0" }));
  expect(screen.getByRole("button", { name: "分页审查 0" })).toHaveAttribute("aria-current", "page");
  await user.click(screen.getByRole("button", { name: "加载更多" }));
  await screen.findByRole("button", { name: "分页审查 99" });
  await user.click(screen.getByRole("button", { name: "加载更多" }));

  expect(await screen.findByRole("button", { name: "分页审查 100" })).toBeInTheDocument();
  expect(screen.getByText("已加载全部")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "分页审查 0" })).toHaveAttribute("aria-current", "page");
  expect(vi.mocked(listReviewSessions).mock.calls).toEqual(expect.arrayContaining([
    [50, 0],
    [50, 50],
    [50, 100],
  ]));
});

it("reattaches the stream when opening a queued history review", async () => {
  const queued = { ...completedSession("queued", "进行中审查", "print('working')\n"), status: "queued" as const };
  const completed = { ...queued, status: "completed" as const };
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [historyItem("queued", "进行中审查", "queued")], limit: 100, offset: 0,
  });
  vi.mocked(getReviewSession).mockResolvedValue(queued);
  vi.mocked(streamReviewSession).mockResolvedValue(completed);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: "进行中审查" }));

  await waitFor(() => expect(streamReviewSession).toHaveBeenCalledWith("queued", expect.any(Function), expect.any(AbortSignal)));
  expect(await screen.findByText("print('working')")).toBeInTheDocument();
});

it("restores a recoverable recheck terminal snapshot without waiting for old SSE", async () => {
  const failed = {
    ...completedSession("failed-recheck", "复查超时", "value = 2\n"),
    status: "failed" as const,
    error: "修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
    summary: {
      ...completedSession("failed-recheck", "复查超时", "value = 2\n").summary,
      text: "修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
    },
    finding_states: { "finding-fixed": "fixed_pending_revalidation" as const },
    recheck_attempt_id: "recheck-timeout",
    recheck_attempt_status: "timed_out" as const,
  };
  vi.mocked(listReviewSessions).mockResolvedValue({ items: [], limit: 50, offset: 0 });
  vi.mocked(getReviewSession).mockResolvedValue(failed);
  window.history.replaceState({}, "", "/reviews/failed-recheck");

  render(<App />);

  expect(await screen.findByText("value = 2")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("统一复查超时");
  expect(screen.getByRole("button", { name: "重新复查" })).toBeInTheDocument();
  expect(streamReviewSession).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("模型正在生成")).not.toBeInTheDocument();
});

it("ends recheck busy from a failed snapshot when SSE never sends a terminal event", async () => {
  const failed = {
    ...completedSession("failed-recheck-live", "复查超时", "value = 3\n"),
    status: "failed" as const,
    error: "修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
    summary: {
      ...completedSession("failed-recheck-live", "复查超时", "value = 3\n").summary,
      text: "修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
    },
  };
  vi.mocked(listReviewSessions).mockResolvedValue({ items: [], limit: 50, offset: 0 });
  vi.mocked(getReviewSession).mockResolvedValue(failed);
  vi.mocked(resumeReviewSession).mockImplementation(() => new Promise(() => undefined));
  vi.mocked(streamReviewSession).mockImplementation(() => new Promise(() => undefined));
  window.history.replaceState({}, "", "/reviews/failed-recheck-live");
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: "重新复查" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("统一复查超时");
  expect(screen.getByRole("button", { name: "重新复查" })).toBeInTheDocument();
  expect(screen.queryByText("修复已保留，正在重新复查修改后的代码。")).not.toBeInTheDocument();
  expect(resumeReviewSession).toHaveBeenCalledTimes(1);
});

it("restores a review from the URL and follows popstate without a history click", async () => {
  const oldSession = completedSession("old", "旧审查", "print('old-url')\n");
  const otherSession = completedSession("other", "其他审查", "print('other-url')\n");
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [historyItem("old", "旧审查", "completed"), historyItem("other", "其他审查", "completed")],
    limit: 100,
    offset: 0,
  });
  vi.mocked(getReviewSession).mockImplementation(async (reviewId) => reviewId === "old" ? oldSession : otherSession);
  window.history.replaceState({}, "", "/reviews/old");

  render(<App />);
  expect(await screen.findByText("print('old-url')")).toBeInTheDocument();

  act(() => {
    window.history.pushState({}, "", "/reviews/other");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(await screen.findByText("print('other-url')")).toBeInTheDocument();
  expect(screen.queryByText("print('old-url')")).not.toBeInTheDocument();
});

it("isolates a new-review draft while viewing history and restores it on new review", async () => {
  const oldSession = completedSession("old", "旧审查", "print('old')\n");
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [historyItem("old", "旧审查", "completed")], limit: 50, offset: 0,
  });
  vi.mocked(getReviewSession).mockResolvedValue(oldSession);
  const user = userEvent.setup();
  render(<App />);
  const composer = screen.getByRole("textbox", { name: "输入需要审查的代码" });
  await user.type(composer, "print('draft')");
  await user.click(await screen.findByRole("button", { name: "旧审查" }));
  expect(composer).toBeDisabled();
  expect(composer).toHaveValue("");
  expect(screen.getByRole("button", { name: "发送审查" })).toBeDisabled();
  expect(screen.getByText(/点击“新建审查”后可继续未提交草稿/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "新建审查" }));
  expect(composer).toBeEnabled();
  expect(composer).toHaveValue("print('draft')");
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
  let resolveFollowup: ((response: {
    action: "answer";
    messages: Array<{
    message_id: string;
    review_id: string;
    role: "user" | "assistant";
    content: string;
    created_at: string;
    }>;
  }) => void) | undefined;
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
  resolveFollowup?.({
    action: "answer",
    messages: [{
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
    }],
  });
  await waitFor(() => expect(askReviewFollowup).toHaveBeenCalledTimes(1));
  expect(screen.queryByText("这是旧审查的回答")).not.toBeInTheDocument();
  expect(screen.getByText("print('other')")).toBeInTheDocument();
});

it("opens the existing diff confirmation after a finding follow-up candidate succeeds", async () => {
  const finding = {
    finding_id: "finding-fix",
    source: "static" as const,
    analyzer: "python-ast",
    rule_id: "python.undefined-name",
    category: "correctness",
    severity: "medium" as const,
    confidence: 1,
    file_id: "file-fix",
    start_line: 1,
    start_column: 5,
    end_line: 1,
    end_column: 6,
    title: "名称未定义：b",
    hover_summary: "b 未定义",
    detail: "b 在当前作用域不可解析",
    evidence: "b",
    impact: "运行时失败",
    suggestion: "请确认 b 的业务含义",
    verification: {
      range_valid: true,
      evidence_matched: true,
      static_confirmed: true,
      cross_file_checked: false,
      deduplicated: false,
    },
  };
  const review = {
    ...completedSession("fix", "追问修复", "a = b\n"),
    files: [{
      file_id: "file-fix",
      relative_path: "fix.py",
      language: "python" as const,
      content: "a = b\n",
      sha256: "a".repeat(64),
      line_offsets: [0, 6],
    }],
    findings: [finding],
  };
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [historyItem("fix", "追问修复", "completed")],
    limit: 100,
    offset: 0,
  });
  vi.mocked(getReviewSession).mockResolvedValue(review);
  vi.mocked(askReviewFollowup).mockResolvedValue({
    action: "fix_candidate",
    phase: "awaiting_confirmation",
    candidate: {
      candidate_id: "followup-fix-1",
      review_id: "fix",
      finding_id: "finding-fix",
      file_id: "file-fix",
      relative_path: "fix.py",
      created_at: "2026-08-21T00:00:00+00:00",
      expires_at: "2026-08-21T00:10:00+00:00",
      base_sha256: "a".repeat(64),
      after_sha256: "b".repeat(64),
      diff: "--- a/fix.py\n+++ b/fix.py\n@@ -1 +1 @@\n-a = b\n+a = {}\n",
      explanation: "按追问指令生成候选",
      validation: ["Python 语法解析通过"],
      output_token_budget: 256,
    },
  });

  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "追问修复" }));
  await user.click(await screen.findByRole("button", { name: "名称未定义：b，第 1 行" }));
  await user.type(
    screen.getByRole("textbox", { name: "针对当前代码追问" }),
    "将 b 替换为空字典",
  );
  await user.click(screen.getByRole("button", { name: "发送追问" }));

  await waitFor(() => expect(askReviewFollowup).toHaveBeenCalledWith(
    "fix",
    "将 b 替换为空字典",
    expect.objectContaining({
      kind: "finding",
      file_id: "file-fix",
      finding_id: "finding-fix",
    }),
    undefined,
    "a".repeat(64),
  ));
  expect(screen.queryByRole("textbox", { name: "针对当前代码追问" })).not.toBeInTheDocument();
  expect(await screen.findByText("按追问指令生成候选")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认应用" })).toBeInTheDocument();
});

it("does not let an older context load overwrite a newly selected finding", async () => {
  const baseFinding = {
    source: "static" as const,
    analyzer: "python-ast",
    rule_id: "python.test",
    category: "correctness",
    severity: "medium" as const,
    confidence: 1,
    file_id: "file-contexts",
    start_column: 1,
    end_column: 6,
    hover_summary: "摘要",
    detail: "详情",
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
  const findingA = {
    ...baseFinding,
    finding_id: "finding-a",
    start_line: 1,
    end_line: 1,
    title: "问题 A",
    evidence: "a = 1",
  };
  const findingB = {
    ...baseFinding,
    finding_id: "finding-b",
    start_line: 2,
    end_line: 2,
    title: "问题 B",
    evidence: "b = 2",
  };
  const review = {
    ...completedSession("contexts", "上下文隔离", "a = 1\nb = 2\n"),
    findings: [findingA, findingB],
  };
  vi.mocked(listReviewSessions).mockResolvedValue({
    items: [historyItem("contexts", "上下文隔离", "completed")],
    limit: 100,
    offset: 0,
  });
  vi.mocked(getReviewSession).mockResolvedValue(review);
  let resolveA: ((messages: Array<{
    message_id: string;
    review_id: string;
    role: "user" | "assistant";
    content: string;
    created_at: string;
  }>) => void) | undefined;
  vi.mocked(getReviewFollowups)
    .mockImplementationOnce(() => new Promise((resolve) => { resolveA = resolve; }))
    .mockResolvedValueOnce([{
      message_id: "answer-b",
      review_id: "contexts",
      role: "assistant",
      content: "B context answer",
      created_at: "2026-08-20T00:00:00+00:00",
    }]);

  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "上下文隔离" }));
  await user.click(await screen.findByRole("button", { name: "问题 A，第 1 行" }));
  await user.click(screen.getByRole("button", { name: "关闭二次追问" }));
  await user.click(screen.getByRole("button", { name: "问题 B，第 2 行" }));
  await user.click(screen.getByRole("button", { name: "问题 B，第 2 行" }));
  expect(await screen.findByText("B context answer")).toBeInTheDocument();
  resolveA?.([{
    message_id: "answer-a",
    review_id: "contexts",
    role: "assistant",
    content: "A stale answer",
    created_at: "2026-08-20T00:00:01+00:00",
  }]);
  await waitFor(() => expect(getReviewFollowups).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("A stale answer")).not.toBeInTheDocument();
  expect(screen.getByText("B context answer")).toBeInTheDocument();
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

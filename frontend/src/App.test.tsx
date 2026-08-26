import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { repairFailureMessage } from "./App";
import {
  ApiError,
  cancelReviewSession,
  confirmReviewFix,
  createReviewSession,
  decideReviewFinding,
  downloadArtifact,
  getInstanceHealth,
  getModelProfiles,
  getDeepSeekModels,
  getReviewFollowups,
  listReviewSessions,
  previewReviewFix,
  previewReviewFixWithIntent,
  reopenReviewFinding,
  resumeReviewSession,
  previewLocalDiff,
  streamReviewSession,
} from "./api/client";

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    getModelProfiles: vi.fn(),
    getDeepSeekModels: vi.fn(),
    getInstanceHealth: vi.fn(),
    getReviewFollowups: vi.fn(),
    listReviewSessions: vi.fn(),
    previewLocalDiff: vi.fn(),
    createReviewSession: vi.fn(),
    decideReviewFinding: vi.fn(),
    downloadArtifact: vi.fn(),
    previewReviewFix: vi.fn(),
    previewReviewFixWithIntent: vi.fn(),
    reopenReviewFinding: vi.fn(),
    resumeReviewSession: vi.fn(),
    confirmReviewFix: vi.fn(),
    streamReviewSession: vi.fn(),
    cancelReviewSession: vi.fn(),
  };
});

it("distinguishes invalid model content from an unsafe replacement boundary", () => {
  expect(repairFailureMessage(
    new ApiError("backend detail", 409, "model_output_invalid"),
    "fallback",
  )).toContain("模型返回的修改内容无效");
  expect(repairFailureMessage(
    new ApiError("backend detail", 409, "scope_mismatch"),
    "fallback",
  )).toContain("系统无法安全定位替换边界");
});
const mockedProfiles = vi.mocked(getModelProfiles);
const mockedDeepSeekModels = vi.mocked(getDeepSeekModels);

const mockedHealth = vi.mocked(getInstanceHealth);
const mockedFollowups = vi.mocked(getReviewFollowups);
const mockedHistory = vi.mocked(listReviewSessions);
const mockedLocalDiffPreview = vi.mocked(previewLocalDiff);
const mockedCreate = vi.mocked(createReviewSession);
const mockedDecision = vi.mocked(decideReviewFinding);
const mockedDownload = vi.mocked(downloadArtifact);
const mockedFixPreview = vi.mocked(previewReviewFix);
const mockedIntentPreview = vi.mocked(previewReviewFixWithIntent);
const mockedReopen = vi.mocked(reopenReviewFinding);
const mockedResume = vi.mocked(resumeReviewSession);
const mockedFixConfirm = vi.mocked(confirmReviewFix);
const mockedStream = vi.mocked(streamReviewSession);
const mockedCancel = vi.mocked(cancelReviewSession);

function completedSession() {
  return {
    review_id: "review-1",
    title: "代码审查 · 2026-08-03 00:00",
    mode: "paste" as const,
    status: "completed" as const,
    model: {
      profile_id: "local-qwen3-8b",
      provider: "local" as const,
      model: "Qwen3-8B",
      display_name: "Local Qwen3-8B",
    },
    created_at: "2026-08-03T00:00:00+00:00",
    expires_at: "2026-08-04T00:00:00+00:00",
    files: [
      {
        file_id: "file-1",
        relative_path: "snippet.py",
        language: "python" as const,
        content: "value = input()\neval(value)\n",
        sha256: "a".repeat(64),
        line_offsets: [0, 16, 28],
      },
    ],
    findings: [
      {
        finding_id: "finding-1",
        source: "static" as const,
        analyzer: "python-ast",
        rule_id: "python.dangerous-eval",
        category: "security",
        severity: "high" as const,
        confidence: 1,
        file_id: "file-1",
        file: "snippet.py",
        start_line: 2,
        start_column: 1,
        end_line: 2,
        end_column: 12,
        title: "危险的 eval 调用",
        hover_summary: "不可信输入可能被执行。",
        detail: "不可信输入可能执行任意代码。",
        evidence: "eval(value)",
        impact: "用户输入可能执行任意代码。",
        suggestion: "改用安全解析器。",
        verification: {
          range_valid: true,
          evidence_matched: true,
          static_confirmed: true,
          cross_file_checked: false,
          deduplicated: false,
        },
      },
    ],
    coverage: [],
    summary: { total: 1, critical: 0, high: 1, medium: 0, low: 0, info: 0, text: "发现 1 个高风险问题。" },
  };
}

describe("review workspace app", () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockedDeepSeekModels.mockResolvedValue([
      { id: "deepseek-v4-flash", display_name: "DeepSeek V4 Flash" },
    ]);
    mockedHealth.mockResolvedValue({
      instances: [{ endpoint_id: "local-qwen3-8b-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: false }],
    });
    mockedProfiles.mockResolvedValue([
      {
        profile_id: "local-qwen3-8b",
        provider: "local",
        model: "Qwen3-8B",
        display_name: "本地 Qwen3-8B",
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
        available: true,
        unavailable_reason: null,
        supports_json: true,
        context_tokens: 1000000,
      },
    ]);
    mockedHistory.mockResolvedValue({ items: [], limit: 100, offset: 0 });
    mockedFollowups.mockResolvedValue([]);
    mockedCreate.mockResolvedValue({
      review_id: "review-1",
      status: "queued",
      expires_at: "2026-08-04T00:00:00+00:00",
    });
    mockedStream.mockResolvedValue(completedSession());
    mockedCancel.mockResolvedValue();
    mockedDownload.mockResolvedValue("artifact.bin");
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it("polls health at idle and hidden intervals while retaining the last good state", async () => {
    vi.useFakeTimers();
    mockedHealth
      .mockResolvedValueOnce({
        instances: [{ endpoint_id: "local-qwen3-8b-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: false }],
      })
      .mockRejectedValue(new TypeError("offline"));
    render(<App />);
    await act(async () => undefined);
    expect(mockedHealth).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("模型健康状态")).toHaveTextContent("可用");

    await act(async () => vi.advanceTimersByTimeAsync(4_999));
    expect(mockedHealth).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(mockedHealth).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("模型健康状态")).toHaveTextContent("状态暂不可用");

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(mockedHealth).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(mockedHealth).toHaveBeenCalledTimes(3);
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => undefined);
    expect(mockedHealth).toHaveBeenCalledTimes(4);
  });

  it("polls health every second while a review is active", async () => {
    vi.useFakeTimers();
    mockedStream.mockImplementation(() => new Promise(() => undefined));
    const { unmount } = render(<App />);
    await act(async () => undefined);
    fireEvent.change(screen.getByRole("textbox", { name: "输入需要审查的代码" }), {
      target: { value: "print('active')" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送审查" }));
    await act(async () => undefined);
    const callsAfterActivation = mockedHealth.mock.calls.length;
    expect(callsAfterActivation).toBeGreaterThanOrEqual(2);

    await act(async () => vi.advanceTimersByTimeAsync(999));
    expect(mockedHealth).toHaveBeenCalledTimes(callsAfterActivation);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(mockedHealth).toHaveBeenCalledTimes(callsAfterActivation + 1);
    unmount();
  });

  it("uses only the selected profile health and refreshes immediately on profile switch", async () => {
    mockedProfiles.mockResolvedValue([
      {
        profile_id: "local-qwen3-8b", provider: "local", model: "Qwen3-8B",
        display_name: "本地 Qwen3-8B", available: true, supports_json: true, context_tokens: 40960,
      },
      {
        profile_id: "local-qwen3-32b", provider: "local", model: "Qwen3-32B",
        display_name: "本地 Qwen3-32B", available: true, supports_json: true, context_tokens: 40960,
      },
    ]);
    mockedHealth.mockResolvedValue({ instances: [
      { endpoint_id: "local-qwen3-8b-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: false },
      { endpoint_id: "local-qwen3-32b-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: true },
    ] });
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText("模型健康状态")).toHaveTextContent("可用"));
    const callsBeforeSwitch = mockedHealth.mock.calls.length;

    fireEvent.change(screen.getByRole("combobox", { name: "审查模型" }), {
      target: { value: "local-qwen3-32b" },
    });

    await waitFor(() => expect(screen.getByLabelText("模型健康状态")).toHaveTextContent("熔断"));
    await waitFor(() => expect(mockedHealth.mock.calls.length).toBeGreaterThan(callsBeforeSwitch));
  });

  it("renders the three-panel workspace and unified review composer", async () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "以可验证证据为中心的代码审查" })).toBeInTheDocument();
    expect(screen.getByText("CodeAstra")).toBeVisible();
    expect(screen.getByText("星鉴")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "输入需要审查的代码" })).toBeInTheDocument();
    const addButton = screen.getByRole("button", { name: "添加内容" });
    expect(addButton).toBeEnabled();
    await userEvent.click(addButton);
    expect(screen.getByRole("menuitem", { name: /选择本地文件/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /本地版本对比/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /从 GitLab 导入/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "复制代码" })).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "问题导航" })).toBeInTheDocument();
    expect(await screen.findByLabelText("模型健康状态")).toHaveTextContent("可用");
  });

  it("submits the model selected in the left navigation", async () => {
    const user = userEvent.setup();
    render(<App />);
    const selector = await screen.findByRole("combobox", { name: "审查模型" });
    await user.selectOptions(selector, "deepseek-api");
    await user.type(screen.getByLabelText("API Key（保存在当前浏览器，并按账号隔离）"), "sk-test");
    await user.type(
      screen.getByRole("textbox", { name: "输入需要审查的代码" }),
      "print('ok')",
    );
    await user.click(screen.getByRole("button", { name: "发送审查" }));

    expect(mockedCreate).toHaveBeenCalledWith(
      "paste",
      expect.objectContaining({
        model_profile_id: "deepseek-api",
        deepseek_selection_mode: "auto",
      }),
      "sk-test",
    );
  });

  it("labels a pending automatic DeepSeek review without claiming Qwen3-8B", async () => {
    const user = userEvent.setup();
    mockedStream.mockImplementation(() => new Promise(() => {}));
    render(<App />);

    const selector = await screen.findByRole("combobox", { name: "\u5ba1\u67e5\u6a21\u578b" });
    await user.selectOptions(selector, "deepseek-api");
    await user.type(screen.getByLabelText(/API Key/), "sk-test");
    await user.type(
      screen.getByRole("textbox", { name: "\u8f93\u5165\u9700\u8981\u5ba1\u67e5\u7684\u4ee3\u7801" }),
      "print('ok')",
    );
    await user.click(screen.getByRole("button", { name: "\u53d1\u9001\u5ba1\u67e5" }));

    expect(await screen.findByText("DeepSeek \u81ea\u52a8\u9009\u62e9")).toBeInTheDocument();
    expect(screen.queryByText("Qwen3-8B")).not.toBeInTheDocument();
  });
  it("submits only the new local version with its old base and local-diff origin", async () => {
    mockedLocalDiffPreview.mockResolvedValue({
      old_label: "service-old.py",
      new_label: "service.py",
      files: [{
        old_path: "service-old.py",
        new_path: "service.py",
        change_type: "renamed",
        language: "python",
        old_content: "value = 1\n",
        new_content: "value = 2\n",
        old_sha256: "a".repeat(64),
        new_sha256: "b".repeat(64),
        diff: "@@ -1 +1 @@",
        changed_ranges: [{ start_line: 1, end_line: 1 }],
        diff_truncated: false,
        selectable: true,
        unavailable_reason: null,
      }],
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "添加内容" }));
    await user.click(screen.getByRole("menuitem", { name: /本地版本对比/ }));
    await user.upload(
      screen.getByLabelText("修改前文件"),
      new File(["value = 1\n"], "service-old.py", { type: "text/plain" }),
    );
    await user.upload(
      screen.getByLabelText("修改后文件"),
      new File(["value = 2\n"], "service.py", { type: "text/plain" }),
    );
    await user.click(screen.getByRole("button", { name: "生成对比" }));
    await user.click(await screen.findByRole("button", { name: "添加到审查" }));

    expect(screen.getByText("版本对比 · 1 处变更")).toBeInTheDocument();
    expect(screen.getByText("service-old.py → service.py · 仅新版本送审")).toBeInTheDocument();
    mockedStream.mockImplementation(() => new Promise(() => {}));
    await user.click(screen.getByRole("button", { name: "发送审查" }));

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledWith(
      "single",
      expect.objectContaining({
        filename: "service.py",
        content: "value = 2\n",
        local_diff_base_files: [expect.objectContaining({ filename: "service.py", content: "value = 1\n" })],
        origin: expect.objectContaining({
          type: "local_diff",
          selected_paths: ["service.py"],
          changed_ranges: { "service.py": [{ start_line: 1, end_line: 1 }] },
        }),
      }),
    ));
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "删除 service.py" })).not.toBeInTheDocument();
    });
  });

  it("keeps an unavailable DeepSeek profile disabled and submits with local", async () => {
    mockedProfiles.mockResolvedValue([
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
    const user = userEvent.setup();
    render(<App />);

    expect(
      await screen.findByRole("option", { name: /DeepSeek API/ }),
    ).toBeDisabled();
    await user.type(
      screen.getByRole("textbox", { name: "\u8f93\u5165\u9700\u8981\u5ba1\u67e5\u7684\u4ee3\u7801" }),
      "print('ok')",
    );
    await user.click(screen.getByRole("button", { name: "\u53d1\u9001\u5ba1\u67e5" }));

    expect(mockedCreate).toHaveBeenCalledWith(
      "paste",
      expect.objectContaining({ model_profile_id: "local-qwen3-8b" }),
    );
  });

  it("disables submission until code or files are provided", async () => {
    render(<App />);
    expect(screen.getByRole("button", { name: "发送审查" })).toBeDisabled();
    expect(mockedCreate).not.toHaveBeenCalled();
    expect(await screen.findByLabelText("模型健康状态")).toHaveTextContent("可用");
  });

  it("creates a session and renders full code, finding detail and navigation", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(user_input)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));

    expect(mockedCreate).toHaveBeenCalledWith(
      "paste",
      expect.objectContaining({ filename: "snippet.py", language: "python", content: "eval(user_input)" }),
    );
    expect(await screen.findByText("发现 1 个高风险问题。")).toBeInTheDocument();
    expect(screen.getByLabelText("完整代码 snippet.py")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "危险的 eval 调用" })).toBeInTheDocument();
    expect(screen.getByText("改用安全解析器。")).toBeInTheDocument();
    const nav = screen.getByRole("complementary", { name: "问题导航" });
    expect(within(nav).getByRole("button", { name: /危险的 eval 调用/ })).toBeInTheDocument();
    const apply = within(nav).getByRole("button", { name: "应用修复" });
    const accept = within(nav).getByRole("button", { name: "接受风险" });
    expect(apply).toHaveClass("finding-decision-action");
    expect(accept).toHaveClass("finding-decision-action");
    expect(apply).toHaveTextContent(/^应用修复$/);
    expect(accept).toHaveTextContent(/^接受风险$/);
    expect(within(nav).queryByRole("button", { name: "稍后处理" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("button", { name: "判定不成立" })).not.toBeInTheDocument();
  });

  it("removes accepted risk without starting a redundant semantic re-review", async () => {
    const completed = completedSession();
    const noFindings = {
      ...completed, findings: [],
      summary: { ...completed.summary, total: 0, high: 0, text: "未发现新的明确问题。" },
    };
    mockedStream.mockResolvedValueOnce(completed);
    mockedDecision.mockResolvedValue({
      session: {
        ...noFindings,
        finding_decisions: { "finding-1": "accepted_risk" },
        decided_findings: { "finding-1": completed.findings[0] },
      },
      revised_review: null,
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(value)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "接受风险" }));
    await waitFor(() => {
      expect(within(screen.getByRole("complementary", { name: "问题导航" }))
        .queryByRole("button", { name: /危险的 eval 调用/ })).not.toBeInTheDocument();
    });
    expect(mockedStream).toHaveBeenCalledTimes(1);
  });

  it("shows accepted risk in processed findings, reopens without a model call, then previews a fix", async () => {
    const completed = completedSession();
    const acceptedAt = "2026-08-23T08:00:00+00:00";
    const acceptedSession = {
      ...completed,
      findings: [],
      finding_decisions: { "finding-1": "accepted_risk" as const },
      decided_findings: { "finding-1": completed.findings[0] },
      finding_decision_history: [{
        finding_id: "finding-1",
        action: "decided" as const,
        decision: "accepted_risk" as const,
        created_at: acceptedAt,
        reason: "用户明确接受该问题的风险。",
        revision_retained: false,
      }],
      summary: { ...completed.summary, total: 0, high: 0, text: "没有活动问题。" },
    };
    mockedStream.mockResolvedValueOnce(completed);
    mockedDecision.mockResolvedValue({ session: acceptedSession, revised_review: null });
    mockedReopen.mockResolvedValue({
      session: {
        ...acceptedSession,
        findings: completed.findings,
        finding_decisions: {},
        finding_states: { "finding-1": "reopened" },
        summary: completed.summary,
      },
      revision_retained: false,
      already_reopened: false,
    });
    mockedFixPreview.mockResolvedValue({
      candidate_id: "candidate-reopened",
      review_id: completed.review_id,
      finding_id: "finding-1",
      file_id: "file-1",
      relative_path: "snippet.py",
      created_at: acceptedAt,
      expires_at: "2026-08-23T08:15:00+00:00",
      base_sha256: "a".repeat(64),
      after_sha256: "b".repeat(64),
      diff: "--- a/snippet.py\n+++ b/snippet.py\n",
      explanation: "重新生成安全修复",
      validation: ["语法通过"],
      output_token_budget: 256,
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(value)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "接受风险" }));
    await user.click(await screen.findByText("已处理问题（1）"));
    expect(screen.getByText("用户明确接受该问题的风险。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新打开" }));

    expect(mockedReopen).toHaveBeenCalledWith("review-1", "finding-1");
    expect(mockedStream).toHaveBeenCalledTimes(1);
    await user.click(await screen.findByRole("button", { name: "应用修复" }));
    expect(await screen.findByRole("dialog", { name: "确认应用候选修复" })).toBeInTheDocument();
  });

  it("debounces controlled artifact downloads and shows nearby completion state", async () => {
    let resolveDownload: (value: string) => void = () => undefined;
    mockedDownload.mockImplementation(() => new Promise((resolve) => {
      resolveDownload = resolve;
    }));
    mockedStream.mockResolvedValueOnce({
      ...completedSession(),
      revisions: [{
        revision_id: "revision-1", finding_id: "finding-1", file_id: "file-1",
        relative_path: "snippet.py", created_at: "2026-08-23T08:00:00+00:00",
        before_sha256: "a".repeat(64), after_sha256: "b".repeat(64), undone_at: null,
      }],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(value)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    const report = await screen.findByRole("button", { name: "导出报告" });
    fireEvent.click(report);
    fireEvent.click(report);
    expect(mockedDownload).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "下载中…" })).toBeDisabled();

    resolveDownload("review-1-report.md");
    expect(await screen.findByText("已开始下载 review-1-report.md")).toBeInTheDocument();
  });

  it("aborts only the client wait for fix generation with accurate wording", async () => {
    mockedFixPreview.mockImplementation((_review, _finding, _key, signal) =>
      new Promise((_resolve, reject) => signal?.addEventListener(
        "abort", () => reject(new DOMException("stopped", "AbortError")),
      )),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(value)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "应用修复" }));
    await user.click(screen.getByRole("button", { name: "停止等待生成" }));
    expect(await screen.findByText(/服务端任务可能继续到结束/)).toBeInTheDocument();
  });

  it("opens intent dialog on needs_intent and makes only the selected intent request", async () => {
    const completed = completedSession();
    const evidence = {
      unresolved_name: "valux",
      scope_kind: "function",
      scope_symbol: "f",
      statement_kind: "Return",
      statement_start_line: 2,
      statement_end_line: 2,
      statement_text: "return valux",
      visible_parameters: ["value"],
      visible_imports: [],
      visible_assignments: [],
      explanation: "没有唯一高置信计划，需要确认。",
      outcome: "needs_intent" as const,
      options: [
        {
          option_id: "rename:value",
          kind: "rename_existing" as const,
          label: "将 valux 改为已有符号 value",
          symbol: "value",
          requires_input: "none" as const,
        },
        {
          option_id: "custom_behavior",
          kind: "custom_behavior" as const,
          label: "描述期望行为",
          requires_input: "behavior" as const,
        },
      ],
    };
    const session = {
      ...completed,
      files: [{
        ...completed.files[0],
        content: "def f(value):\n    return valux\n",
        sha256: "c".repeat(64),
      }],
      findings: [{
        ...completed.findings[0],
        rule_id: "python.undefined-name",
        title: "名称未定义：valux",
        evidence: "valux",
        use_def_evidence: evidence,
      }],
    };
    mockedStream.mockResolvedValue(session);
    mockedFixPreview.mockRejectedValue(new ApiError(
      "已定位症状，但无法唯一确定开发者意图。",
      409,
      "needs_intent",
      { base_sha: "c".repeat(64), use_def_evidence: evidence },
    ));
    mockedIntentPreview.mockResolvedValue({
      candidate_id: "intent-fix", review_id: "review-1", finding_id: "finding-1",
      file_id: "file-1", relative_path: "snippet.py", created_at: "now",
      expires_at: "later", base_sha256: "c".repeat(64), after_sha256: "d".repeat(64),
      diff: "-    return valux\n+    return value", explanation: "按选择重命名",
      validation: ["Python 语法解析通过"], output_token_budget: 1024,
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(
      screen.getByRole("textbox", { name: "输入需要审查的代码" }),
      "def f(value):\n    return valux",
    );
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "应用修复" }));
    expect(await screen.findByRole("dialog", { name: "确认修改目标" })).toBeInTheDocument();
    expect(screen.queryByText("请求未能完成，请检查输入后重试。")).not.toBeInTheDocument();
    expect(mockedFixPreview).toHaveBeenCalledTimes(1);
    expect(mockedIntentPreview).not.toHaveBeenCalled();
    await user.type(screen.getByRole("textbox", { name: "告诉 CodeAstra 你希望如何修改" }), "改用已有参数 value");
    await user.click(screen.getByRole("button", { name: "生成修改预览" }));
    expect(mockedIntentPreview).toHaveBeenCalledWith(
      "review-1",
      "finding-1",
      expect.objectContaining({
        base_sha: "c".repeat(64),
        option_id: "custom_behavior",
        intent_kind: "custom_behavior",
        user_intent: "改用已有参数 value",
      }),
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole("dialog", { name: "确认应用候选修复" })).toBeInTheDocument();
    expect(mockedFixPreview).toHaveBeenCalledTimes(1);
  });

  it("shows semantic progress and stops generation immediately", async () => {
    mockedStream.mockImplementation((_id, onEvent, signal) => {
      onEvent({ event: "stage", data: { message: "正在进行静态分析" } });
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("stopped", "AbortError")));
      });
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(user_input)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    expect(await screen.findByText("正在进行静态分析")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /停止生成/ }));

    await waitFor(() => expect(screen.getByRole("button", { name: "发送审查" })).toBeEnabled());
    expect(mockedCancel).toHaveBeenCalledWith("review-1");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("ignores buffered events after stop", async () => {
    let emit: ((event: { event: "stage"; data: { message: string } }) => void) | undefined;
    mockedStream.mockImplementation((_id, onEvent) => {
      emit = onEvent as typeof emit;
      return new Promise(() => undefined);
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(user_input)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: /停止生成/ }));
    await act(async () => emit?.({ event: "stage", data: { message: "迟到事件" } }));
    expect(screen.queryByText("迟到事件")).not.toBeInTheDocument();
  });

  it("keeps the confirmation layer through re-review then immediately allows a selection follow-up", async () => {
    const initial = completedSession();
    const revisedContent = "value = input()\nsafe_value = value.strip()\n";
    const applied = {
      ...initial,
      files: [{
        ...initial.files[0],
        content: revisedContent,
        sha256: "b".repeat(64),
      }],
      findings: [],
      revisions: [{
        revision_id: "revision-1",
        finding_id: "finding-1",
        file_id: "file-1",
        relative_path: "snippet.py",
        created_at: "2026-08-21T00:00:00+00:00",
        before_sha256: "a".repeat(64),
        after_sha256: "b".repeat(64),
        diff: "-eval(value)\n+safe_value = value.strip()",
        explanation: "移除危险执行",
        validation: ["静态检查通过"],
      }],
    };
    const queued = { ...applied, status: "queued" as const };
    const completed = { ...applied, status: "completed" as const };
    let finishReReview: ((session: typeof completed) => void) | undefined;
    mockedStream
      .mockResolvedValueOnce(initial)
      .mockImplementationOnce(() => new Promise((resolve) => { finishReReview = resolve; }));
    mockedFixPreview.mockResolvedValue({
      candidate_id: "fix-rereview",
      review_id: "review-1",
      finding_id: "finding-1",
      file_id: "file-1",
      relative_path: "snippet.py",
      created_at: "2026-08-21T00:00:00+00:00",
      expires_at: "2026-08-21T00:10:00+00:00",
      base_sha256: "a".repeat(64),
      after_sha256: "b".repeat(64),
      diff: "-eval(value)\n+safe_value = value.strip()",
      explanation: "移除危险执行",
      validation: ["静态检查通过"],
      output_token_budget: 1024,
    });
    mockedFixConfirm.mockResolvedValue({
      session: applied,
      revised_review: queued,
      phase: "applied",
    });

    const user = userEvent.setup();
    const { container } = render(<App />);
    await user.type(
      screen.getByRole("textbox", { name: "输入需要审查的代码" }),
      "eval(value)",
    );
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "应用修复" }));
    await user.click(await screen.findByRole("button", { name: "确认应用" }));

    expect(await screen.findByRole("dialog", { name: "确认应用候选修复" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /正在应用并准备复查/ })).toBeDisabled();

    await act(async () => finishReReview?.(completed));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "确认应用候选修复" })).not.toBeInTheDocument();
    });
    expect(screen.getByText("safe_value = value.strip()")).toBeInTheDocument();

    const selectedNode = container.querySelector('[data-line-number="2"] code')?.firstChild as Node;
    const selection = vi.spyOn(window, "getSelection").mockReturnValue({
      isCollapsed: false,
      rangeCount: 1,
      toString: () => "safe_value = value.strip()",
      getRangeAt: () => ({
        commonAncestorContainer: selectedNode,
        startContainer: selectedNode,
        endContainer: selectedNode,
        getBoundingClientRect: () => ({ left: 120, top: 120, width: 80 }),
      }),
      removeAllRanges: vi.fn(),
    } as unknown as Selection);
    fireEvent.mouseUp(container.querySelector(".code-lines") as HTMLElement, {
      clientX: 160,
      clientY: 120,
    });
    await user.click(screen.getByRole("button", { name: "针对选区追问" }));

    const followup = await screen.findByRole("dialog", { name: "针对代码的二次追问" });
    expect(within(followup).getByRole("heading", { name: "所选代码 · snippet.py" })).toBeInTheDocument();
    expect(within(followup).getByText(/safe_value = value\.strip\(\)/)).toBeInTheDocument();
    expect(mockedFollowups).toHaveBeenCalledWith(
      "review-1",
      expect.objectContaining({
        kind: "selection",
        file_id: "file-1",
        start_line: 2,
        end_line: 2,
        selected_code: "safe_value = value.strip()",
      }),
    );
    selection.mockRestore();
  });

  it("closes the consumed candidate and reports when re-review fails after confirmation", async () => {
    const initial = completedSession();
    const applied = { ...initial, findings: [], finding_states: { "finding-1": "fixed_pending_revalidation" as const } };
    const queued = { ...applied, status: "queued" as const };
    const failed = {
      ...queued,
      status: "failed" as const,
      error: "修复已应用并保留；统一复查未完成，可在模型恢复后重新复查。",
      summary: { ...queued.summary, text: "修复已应用并保留；统一复查未完成，可在模型恢复后重新复查。" },
    };
    mockedStream
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(failed);
    mockedResume.mockResolvedValue(undefined);
    mockedFixPreview.mockResolvedValue({
      candidate_id: "fix-stream-failure",
      review_id: "review-1",
      finding_id: "finding-1",
      file_id: "file-1",
      relative_path: "snippet.py",
      created_at: "2026-08-21T00:00:00+00:00",
      expires_at: "2026-08-21T00:10:00+00:00",
      base_sha256: "a".repeat(64),
      after_sha256: "b".repeat(64),
      diff: "-eval(value)\n+safe_value = value.strip()",
      explanation: "移除危险执行",
      validation: ["静态检查通过"],
      output_token_budget: 1024,
    });
    mockedFixConfirm.mockResolvedValue({
      session: applied,
      revised_review: queued,
      phase: "applied",
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(value)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "应用修复" }));
    await user.click(await screen.findByRole("button", { name: "确认应用" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "修复已应用，但统一复查失败：修复已应用并保留；统一复查未完成，可在模型恢复后重新复查。",
    );
    expect(screen.queryByRole("dialog", { name: "确认应用候选修复" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新复查" })).toBeInTheDocument();
  });

  it("shows separate local model availability and a recoverable failed review", async () => {
    mockedHealth.mockResolvedValue({
      instances: [
        {
          endpoint_id: "local-qwen3-8b-0",
          inflight_requests: 0,
          inflight_tokens: 0,
          circuit_open: true,
          available: false,
          reason_code: "connection_refused",
        },
        {
          endpoint_id: "local-qwen3-32b-0",
          inflight_requests: 0,
          inflight_tokens: 0,
          circuit_open: true,
          available: false,
          reason_code: "connection_refused",
        },
      ],
    });
    const failed = {
      ...completedSession(),
      status: "failed" as const,
      error: "本地模型服务未运行或正在恢复。已创建的审查和完成分块已保留，可重新复查。",
      error_code: "local_model_circuit_open",
    };
    mockedStream.mockResolvedValueOnce(failed);

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "x = 1");
    await user.click(screen.getByRole("button", { name: "发送审查" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("本地模型服务未运行或正在恢复");
    expect(screen.getByLabelText("模型健康状态")).toHaveTextContent("Qwen3-8B（未运行）");
    expect(screen.getByLabelText("模型健康状态")).toHaveTextContent("Qwen3-32B（未运行）");
    expect(screen.getByRole("button", { name: "重新复查" })).toBeInTheDocument();
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
  });

  it("blocks unsafe pasted replacement characters before creating a review", async () => {
    render(<App />);
    const composer = screen.getByRole("textbox", { name: "输入需要审查的代码" });
    fireEvent.change(composer, { target: { value: "value = \uFFFD" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("UTF-8");
    expect(screen.getByRole("button", { name: "发送审查" })).toBeDisabled();
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("allows decisions for findings retained by a failed re-review", async () => {
    const failed = {
      ...completedSession(),
      status: "failed" as const,
      error: "统一复查中途失败。",
    };
    mockedStream.mockResolvedValue(failed);
    mockedFixPreview.mockResolvedValue({
      candidate_id: "fix-1", review_id: "review-1", finding_id: "finding-1", file_id: "file-1",
      relative_path: "snippet.py", created_at: "now", expires_at: "later",
      base_sha256: "a".repeat(64), after_sha256: "b".repeat(64), diff: "-old\n+new",
      explanation: "修复问题", validation: ["静态检查通过"], output_token_budget: 2048,
    });
    mockedFixConfirm.mockResolvedValue({
      session: { ...failed, findings: [] }, revised_review: null, phase: "applied",
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(
      screen.getByRole("textbox", { name: "输入需要审查的代码" }),
      "eval(user_input)",
    );
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "应用修复" }));
    await user.click(await screen.findByRole("button", { name: "确认应用" }));

    expect(mockedFixPreview).toHaveBeenCalledWith(
      "review-1", "finding-1", undefined, expect.any(AbortSignal),
    );
    expect(mockedFixConfirm).toHaveBeenCalledWith("review-1", "fix-1");
  });

  it("shows sanitized review errors without clearing the code", async () => {
    mockedStream.mockRejectedValue(new Error("模型服务暂时不可用，请稍后重试。"));
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "输入需要审查的代码" }), "eval(user_input)");
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    expect(await screen.findByText("模型服务暂时不可用，请稍后重试。")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "输入需要审查的代码" })).toHaveValue("eval(user_input)");
  });
});

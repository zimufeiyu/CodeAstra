import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  cancelReviewSession,
  createReviewSession,
  decideReviewFinding,
  getInstanceHealth,
  getModelProfiles,
  getDeepSeekModels,
  listReviewSessions,
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
    listReviewSessions: vi.fn(),
    previewLocalDiff: vi.fn(),
    createReviewSession: vi.fn(),
    decideReviewFinding: vi.fn(),
    streamReviewSession: vi.fn(),
    cancelReviewSession: vi.fn(),
  };
});
const mockedProfiles = vi.mocked(getModelProfiles);
const mockedDeepSeekModels = vi.mocked(getDeepSeekModels);

const mockedHealth = vi.mocked(getInstanceHealth);
const mockedHistory = vi.mocked(listReviewSessions);
const mockedLocalDiffPreview = vi.mocked(previewLocalDiff);
const mockedCreate = vi.mocked(createReviewSession);
const mockedDecision = vi.mocked(decideReviewFinding);
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
      instances: [{ endpoint_id: "ppu-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: false }],
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
    mockedCreate.mockResolvedValue({
      review_id: "review-1",
      status: "queued",
      expires_at: "2026-08-04T00:00:00+00:00",
    });
    mockedStream.mockResolvedValue(completedSession());
    mockedCancel.mockResolvedValue();
  });

  afterEach(() => {
    vi.clearAllMocks();
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
    expect(screen.queryByRole("button", { name: "Server migration" })).not.toBeInTheDocument();
  });

  it("restores the account-scoped DeepSeek binding as the next login default", async () => {
    window.localStorage.setItem("code-review.user.anonymous.deepseek-preferences.v3", JSON.stringify({
      selectionMode: "auto",
      manualModel: "",
      preferredProfileId: "deepseek-api",
    }));
    window.localStorage.setItem("code-review.user.anonymous.deepseek-api-key.v1", "sk-remembered");

    render(<App />);

    expect(await screen.findByRole("combobox", { name: "审查模型" })).toHaveValue("deepseek-api");
    expect(screen.getByLabelText("API Key（保存在当前浏览器，并按账号隔离）"))
      .toHaveValue("sk-remembered");
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
    expect(screen.queryByRole("button", { name: "Server migration" })).not.toBeInTheDocument();
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
    expect(within(nav).getByRole("button", { name: "按建议修改" })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: "暂不修改" })).toBeInTheDocument();
  });

  it("removes a kept finding from navigation before the request finishes", async () => {
    let resolveDecision:
      | ((value: Awaited<ReturnType<typeof decideReviewFinding>>) => void)
      | undefined;
    mockedDecision.mockReturnValue(
      new Promise((resolve) => {
        resolveDecision = resolve;
      }),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.type(
      screen.getByRole("textbox", { name: "输入需要审查的代码" }),
      "eval(user_input)",
    );
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "暂不修改" }));

    const nav = screen.getByRole("complementary", { name: "问题导航" });
    expect(
      within(nav).queryByRole("button", { name: /危险的 eval 调用/ }),
    ).not.toBeInTheDocument();

    await act(async () => {
      resolveDecision?.({
        session: { ...completedSession(), findings: [] },
        revised_review: null,
        explanation: null,
      });
    });
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

  it("allows decisions for findings retained by a failed re-review", async () => {
    const failed = {
      ...completedSession(),
      status: "failed" as const,
      error: "统一复查中途失败。",
    };
    mockedStream.mockResolvedValue(failed);
    mockedDecision.mockResolvedValue({
      session: { ...failed, findings: [] },
      revised_review: null,
      explanation: null,
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(
      screen.getByRole("textbox", { name: "输入需要审查的代码" }),
      "eval(user_input)",
    );
    await user.click(screen.getByRole("button", { name: "发送审查" }));
    await user.click(await screen.findByRole("button", { name: "按建议修改" }));

    expect(mockedDecision).toHaveBeenCalledWith("review-1", "finding-1", "apply");
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

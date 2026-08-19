import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  askReviewFollowup,
  getInstanceHealth,
  getReviewFollowups,
  getReviewSession,
  listReviewSessions,
} from "./api/client";

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    getInstanceHealth: vi.fn(),
    getReviewSession: vi.fn(),
    listReviewSessions: vi.fn(),
    getReviewFollowups: vi.fn(),
    askReviewFollowup: vi.fn(),
  };
});

const session = {
  review_id: "review-1",
  title: "代码审查 · 2026-08-04 00:00",
  mode: "paste" as const,
  status: "completed" as const,
    model: {
      profile_id: "local-qwen3-8b",
      provider: "local" as const,
      model: "Qwen3-8B",
      display_name: "Local Qwen3-8B",
    },
  created_at: "2026-08-04T00:00:00+00:00",
  expires_at: "2026-08-05T00:00:00+00:00",
  files: [
    {
      file_id: "file-1",
      relative_path: "snippet.py",
      language: "python" as const,
      content: "print('ok')\n",
      sha256: "a".repeat(64),
      line_offsets: [0, 12],
    },
  ],
  findings: [
    {
      finding_id: "finding-1",
      source: "static" as const,
      analyzer: "python-ast",
      rule_id: "python.print",
      category: "quality",
      severity: "info" as const,
      confidence: 1,
      file_id: "file-1",
      file: "snippet.py",
      start_line: 1,
      start_column: 1,
      end_line: 1,
      end_column: 12,
      title: "输出调用",
      hover_summary: "检查输出内容。",
      detail: "该处直接输出内容。",
      evidence: "print('ok')",
      impact: "可能暴露调试信息。",
      suggestion: "确认输出符合预期。",
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
  summary: {
    total: 0,
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    info: 0,
    text: "审查完成。",
  },
};

describe("history, report and follow-up interactions", () => {
  beforeEach(() => {
    vi.mocked(getInstanceHealth).mockResolvedValue({ instances: [] });
    vi.mocked(listReviewSessions).mockResolvedValue({
      items: [
        {
          review_id: "review-1",
          title: session.title,
          mode: "paste",
          status: "completed",
          created_at: session.created_at,
          expires_at: session.expires_at,
          file_count: 1,
          file_names: ["snippet.py"],
          summary: session.summary,
          error: null,
        },
      ],
      limit: 100,
      offset: 0,
    });
    vi.mocked(getReviewSession).mockResolvedValue(session);
    vi.mocked(getReviewFollowups).mockResolvedValue([
      {
        message_id: "answer-old",
        review_id: "review-1",
        role: "assistant",
        content: "已有回答。",
        created_at: "2026-08-04T01:00:00+00:00",
      },
    ]);
    vi.mocked(askReviewFollowup).mockResolvedValue([
      {
        message_id: "question-new",
        review_id: "review-1",
        role: "user",
        content: "如何修复？",
        created_at: "2026-08-04T01:01:00+00:00",
      },
      {
        message_id: "answer-new",
        review_id: "review-1",
        role: "assistant",
        content: "使用安全解析器。",
        created_at: "2026-08-04T01:01:01+00:00",
      },
    ]);
  });

  it("opens history, restores a session, exposes report download and sends follow-up", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: session.title }));

    expect(await screen.findByLabelText("完整代码 snippet.py")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "导出报告" })).toHaveAttribute(
      "href",
      "/v1/reviews/review-1/report",
    );
    const findingNavigation = screen.getByRole("complementary", { name: "问题导航" });
    await user.click(within(findingNavigation).getByRole("button", { name: /输出调用/ }));
    expect(screen.queryByRole("dialog", { name: "针对代码的二次追问" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "输出调用，第 1 行" }));
    expect(await screen.findByRole("dialog", { name: "针对代码的二次追问" })).toBeInTheDocument();
    expect(screen.getByText("已有回答。")).toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "针对当前代码追问" }), "如何修复？");
    await user.click(screen.getByRole("button", { name: "发送追问" }));

    expect(await screen.findByText("使用安全解析器。")).toBeInTheDocument();
    await waitFor(() =>
      expect(askReviewFollowup).toHaveBeenCalledWith(
        "review-1",
        "如何修复？",
        expect.objectContaining({
          kind: "finding",
          file_id: "file-1",
          finding_id: "finding-1",
          selected_code: "print('ok')",
        }),
      ),
    );
  });


  it("keeps exactly one complete finding open and switches code focus with the accordion", async () => {
    const user = userEvent.setup();
    const secondFinding = {
      ...session.findings[0],
      finding_id: "finding-2",
      title: "第二个问题",
      detail: "第二个问题的完整说明。",
      evidence: "print('second')",
      suggestion: "处理第二个问题。",
      start_line: 1,
      end_line: 1,
    };
    vi.mocked(getReviewSession).mockResolvedValue({
      ...session,
      findings: [...session.findings, secondFinding],
    });

    render(<App />);
    await user.click(await screen.findByRole("button", { name: session.title }));

    const navigation = screen.getByRole("complementary", { name: "问题导航" });
    const firstTrigger = within(navigation).getByRole("button", { name: "输出调用" });
    const secondTrigger = within(navigation).getByRole("button", { name: "第二个问题" });

    expect(firstTrigger).toHaveAttribute("aria-expanded", "true");
    expect(secondTrigger).toHaveAttribute("aria-expanded", "false");
    expect(within(navigation).getByLabelText("问题详情：输出调用")).toBeVisible();
    expect(within(navigation).queryByLabelText("问题详情：第二个问题")).not.toBeInTheDocument();

    await user.click(secondTrigger);

    expect(firstTrigger).toHaveAttribute("aria-expanded", "false");
    expect(secondTrigger).toHaveAttribute("aria-expanded", "true");
    expect(within(navigation).queryByLabelText("问题详情：输出调用")).not.toBeInTheDocument();
    const secondPanel = within(navigation).getByLabelText("问题详情：第二个问题");
    expect(secondPanel).toBeVisible();
    expect(within(secondPanel).getByRole("button", { name: "按建议修改" })).toBeVisible();
    expect(within(secondPanel).getByRole("button", { name: "暂不修改" })).toBeVisible();
  });

});

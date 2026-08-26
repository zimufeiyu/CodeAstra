import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import App from "./App";
import {
  confirmReviewFix,
  downloadArtifact,
  getInstanceHealth,
  getModelProfiles,
  getReviewFollowups,
  getReviewSession,
  listReviewSessions,
  previewReviewFix,
  reopenReviewFinding,
  streamReviewSession,
} from "./api/client";

vi.mock("./api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api/client")>();
  return {
    ...original,
    confirmReviewFix: vi.fn(),
    downloadArtifact: vi.fn(),
    getInstanceHealth: vi.fn(),
    getModelProfiles: vi.fn(),
    getReviewFollowups: vi.fn(),
    getReviewSession: vi.fn(),
    listReviewSessions: vi.fn(),
    previewReviewFix: vi.fn(),
    reopenReviewFinding: vi.fn(),
    streamReviewSession: vi.fn(),
  };
});

const finding = {
  finding_id: "finding-old",
  source: "static" as const,
  analyzer: "python-ast",
  rule_id: "python.undefined-name",
  category: "correctness",
  severity: "high" as const,
  confidence: 1,
  file_id: "file-old",
  file: "old.py",
  start_line: 1,
  start_column: 5,
  end_line: 1,
  end_column: 6,
  title: "名称未定义：b",
  hover_summary: "b 无法解析",
  detail: "b 在当前作用域不可见",
  evidence: "b",
  impact: "运行失败",
  suggestion: "选择明确符号后修复",
  verification: {
    range_valid: true,
    evidence_matched: true,
    static_confirmed: true,
    cross_file_checked: false,
    deduplicated: false,
  },
};

function session(status: "completed" | "queued" = "completed") {
  return {
    review_id: "review-old",
    title: "旧审查 0",
    mode: "paste" as const,
    status,
    model: {
      profile_id: "local-qwen3-32b",
      provider: "local" as const,
      model: "Qwen3-32B",
      display_name: "本地 Qwen3-32B",
    },
    created_at: "2026-08-23T08:00:00+00:00",
    expires_at: "2026-08-24T08:00:00+00:00",
    files: [{
      file_id: "file-old", relative_path: "old.py", language: "python" as const,
      content: "a = b\n", sha256: "a".repeat(64), line_offsets: [0, 6],
    }],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "问题已处理。" },
    finding_decisions: { "finding-old": "accepted_risk" as const },
    decided_findings: { "finding-old": finding },
    finding_decision_history: [{
      finding_id: "finding-old", action: "decided" as const,
      decision: "accepted_risk" as const, created_at: "2026-08-23T08:01:00+00:00",
      reason: "用户明确接受该问题的风险。", revision_retained: false,
    }],
  };
}

function historyItem(index: number) {
  return {
    review_id: index === 0 ? "review-old" : `review-${index}`,
    title: index === 0 ? "旧审查 0" : `历史审查 ${index}`,
    mode: "paste" as const,
    status: "completed" as const,
    created_at: "2026-08-23T08:00:00+00:00",
    expires_at: "2026-08-24T08:00:00+00:00",
    file_count: 1,
    file_names: [`file-${index}.py`],
    summary: session().summary,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.history.replaceState({}, "", "/");
  const history = Array.from({ length: 101 }, (_, index) => historyItem(index));
  vi.mocked(listReviewSessions).mockImplementation(async (limit, offset) => ({
    items: history.slice(offset ?? 0, (offset ?? 0) + (limit ?? 20)),
    limit: limit ?? 20,
    offset: offset ?? 0,
  }));
  vi.mocked(getReviewSession).mockResolvedValue(session());
  vi.mocked(getReviewFollowups).mockResolvedValue([]);
  vi.mocked(getModelProfiles).mockResolvedValue([
    { profile_id: "local-qwen3-8b", provider: "local", model: "Qwen3-8B", display_name: "本地 Qwen3-8B", available: true, context_tokens: 40960, supports_json: true },
    { profile_id: "local-qwen3-32b", provider: "local", model: "Qwen3-32B", display_name: "本地 Qwen3-32B", available: true, context_tokens: 40960, supports_json: true },
  ]);
  vi.mocked(getInstanceHealth).mockResolvedValue({ instances: [
    { endpoint_id: "local-qwen3-8b-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: false },
    { endpoint_id: "local-qwen3-32b-0", inflight_requests: 0, inflight_tokens: 0, circuit_open: true },
  ] });
  vi.mocked(downloadArtifact).mockResolvedValue("review-old-report.md");
});

it("completes the E-G journey without an extra review or download generation", async () => {
  const reopened = {
    ...session(),
    findings: [finding],
    finding_decisions: {},
    finding_states: { "finding-old": "reopened" as const },
    summary: { ...session().summary, total: 1, high: 1, text: "1 个问题待处理。" },
  };
  const fixed = {
    ...session(),
    findings: [],
    finding_decisions: { "finding-old": "fixed" as const },
    revisions: [{
      revision_id: "revision-1", finding_id: "finding-old", file_id: "file-old",
      relative_path: "old.py", created_at: "2026-08-23T08:10:00+00:00",
      before_sha256: "a".repeat(64), after_sha256: "b".repeat(64), undone_at: null,
    }],
    files: [{ ...session().files[0], content: "a = 1\n", sha256: "b".repeat(64) }],
    finding_decision_history: [
      ...session().finding_decision_history,
      { finding_id: "finding-old", action: "reopened" as const, decision: "accepted_risk" as const, created_at: "2026-08-23T08:05:00+00:00", reason: "重新打开", revision_retained: false },
      { finding_id: "finding-old", action: "decided" as const, decision: "fixed" as const, created_at: "2026-08-23T08:10:00+00:00", reason: "明确替换 b", revision_retained: false },
    ],
  };
  vi.mocked(reopenReviewFinding).mockResolvedValue({
    session: reopened, revision_retained: false, already_reopened: false,
  });
  vi.mocked(previewReviewFix).mockResolvedValue({
    candidate_id: "candidate-1", review_id: "review-old", finding_id: "finding-old",
    file_id: "file-old", relative_path: "old.py",
    created_at: "2026-08-23T08:06:00+00:00", expires_at: "2026-08-23T08:21:00+00:00",
    base_sha256: "a".repeat(64), after_sha256: "b".repeat(64),
    diff: "--- a/old.py\n+++ b/old.py\n-a = b\n+a = 1\n",
    explanation: "明确替换 b", validation: ["语法通过"], output_token_budget: 256,
  });
  vi.mocked(confirmReviewFix).mockResolvedValue({
    session: fixed,
    revised_review: { ...fixed, status: "queued" as const },
    phase: "applied",
  });
  vi.mocked(streamReviewSession).mockResolvedValue(fixed);
  const user = userEvent.setup();

  render(<App />);
  await user.click(await screen.findByRole("button", { name: "加载更多" }));
  await user.click(screen.getByRole("button", { name: "加载更多" }));
  expect(await screen.findByText("已加载全部")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "旧审查 0" }));
  expect(await screen.findByLabelText("模型健康状态")).toHaveTextContent("熔断");
  await user.click(screen.getByText("已处理问题（1）"));
  await user.click(screen.getByRole("button", { name: "重新打开" }));
  await user.click(await screen.findByRole("button", { name: "应用修复" }));
  await user.click(await screen.findByRole("button", { name: "确认应用" }));
  await waitFor(() => expect(streamReviewSession).toHaveBeenCalledTimes(1));
  await user.click(await screen.findByRole("button", { name: "导出报告" }));

  expect(reopenReviewFinding).toHaveBeenCalledTimes(1);
  expect(previewReviewFix).toHaveBeenCalledTimes(1);
  expect(confirmReviewFix).toHaveBeenCalledTimes(1);
  expect(downloadArtifact).toHaveBeenCalledTimes(1);
  expect(await screen.findByText("已开始下载 review-old-report.md")).toBeInTheDocument();
});

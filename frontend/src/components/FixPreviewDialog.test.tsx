import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { FixPreviewDialog } from "./FixPreviewDialog";

const candidate = {
  candidate_id: "fix-1",
  review_id: "review-1",
  finding_id: "finding-1",
  file_id: "file-1",
  relative_path: "src/example.py",
  created_at: "2026-08-20T00:00:00Z",
  expires_at: "2026-08-20T00:15:00Z",
  base_sha256: "a".repeat(64),
  after_sha256: "b".repeat(64),
  diff: "--- a/src/example.py\n+++ b/src/example.py\n-old\n+new\n",
  explanation: "修复边界检查",
  validation: ["Python 语法解析通过", "静态检查未引入新的诊断"],
  output_token_budget: 2048,
};

test("shows reason diff hashes and validation before confirmation", () => {
  render(<FixPreviewDialog candidate={candidate} busy={false} onCancel={vi.fn()} onConfirm={vi.fn()} />);
  expect(screen.getByRole("dialog", { name: "确认应用候选修复" })).toBeInTheDocument();
  expect(screen.getByText("修复边界检查")).toBeInTheDocument();
  expect(screen.getByLabelText("候选修复统一 Diff")).toHaveTextContent("+new");
  const lines = screen.getByLabelText("候选修复统一 Diff").querySelectorAll(".fix-diff-line");
  expect(Array.from(lines).map((line) => line.textContent)).toEqual([
    "--- a/src/example.py", "+++ b/src/example.py", "-old", "+new", " ",
  ]);
  expect(lines[2]).toHaveClass("removed");
  expect(lines[3]).toHaveClass("added");
  expect(screen.getByText(candidate.base_sha256)).toBeInTheDocument();
  expect(screen.getByText("Python 语法解析通过")).toBeInTheDocument();
});

test("cancel and confirm are explicit separate actions", () => {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  render(<FixPreviewDialog candidate={candidate} busy={false} onCancel={onCancel} onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole("button", { name: "取消，不改变会话" }));
  fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
  expect(onCancel).toHaveBeenCalledTimes(1);
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

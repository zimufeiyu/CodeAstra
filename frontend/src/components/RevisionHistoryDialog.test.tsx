import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { RevisionHistoryDialog } from "./RevisionHistoryDialog";

test("only allows undoing the latest active revision", async () => {
  const onUndo = vi.fn();
  render(
    <RevisionHistoryDialog
      open
      busyRevisionId={null}
      onClose={() => undefined}
      onUndo={onUndo}
      items={[
        {
          revision_id: "new",
          finding_id: "f2",
          file_id: "file-1",
          relative_path: "snippet.py",
          created_at: "2026-08-09T10:00:00Z",
          before_sha256: "c".repeat(64),
          after_sha256: "b".repeat(64),
          diff: "-old\n+new",
          undone_at: null,
        },
        {
          revision_id: "old",
          finding_id: "f1",
          file_id: "file-1",
          relative_path: "snippet.py",
          created_at: "2026-08-09T09:00:00Z",
          before_sha256: "d".repeat(64),
          after_sha256: "a".repeat(64),
          diff: "-before\n+after",
          undone_at: null,
        },
      ]}
    />,
  );

  const buttons = screen.getAllByRole("button", { name: /撤销/ });
  expect(buttons[0]).toBeEnabled();
  expect(buttons[1]).toBeDisabled();
  await userEvent.click(buttons[0]);
  expect(onUndo).toHaveBeenCalledWith("new");
  const diffs = screen.getAllByLabelText("snippet.py 修改差异");
  const diffLines = diffs[0].querySelectorAll(".fix-diff-line");
  expect(diffLines[0]).toHaveTextContent("-old");
  expect(diffLines[1]).toHaveTextContent("+new");
  expect(diffLines[0]).toHaveClass("removed");
  expect(diffLines[1]).toHaveClass("added");
});

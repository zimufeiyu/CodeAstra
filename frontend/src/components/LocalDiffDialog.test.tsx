import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import * as client from "../api/client";
import { LocalDiffDialog } from "./LocalDiffDialog";

test("previews local versions and imports only the new version", async () => {
  const preview: client.LocalDiffPreview = {
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
  };
  vi.spyOn(client, "previewLocalDiff").mockResolvedValue(preview);
  const onImport = vi.fn();
  const user = userEvent.setup();
  render(<LocalDiffDialog open onClose={vi.fn()} onImport={onImport} />);

  await user.upload(
    screen.getByLabelText("修改前文件"),
    new File(["value = 1\n"], "service-old.py", { type: "text/plain" }),
  );
  await user.upload(
    screen.getByLabelText("修改后文件"),
    new File(["value = 2\n"], "service.py", { type: "text/plain" }),
  );
  await user.click(screen.getByRole("button", { name: "生成对比" }));

  await waitFor(() => expect(client.previewLocalDiff).toHaveBeenCalledWith(
    { filename: "service-old.py", content: "value = 1\n" },
    { filename: "service.py", content: "value = 2\n" },
    expect.any(AbortSignal),
  ));
  expect(await screen.findByText("service-old.py → service.py")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "添加到审查" }));
  expect(onImport).toHaveBeenCalledWith(preview, "value = 1\n");
});

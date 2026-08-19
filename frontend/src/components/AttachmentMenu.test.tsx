import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AttachmentMenu } from "./AttachmentMenu";

test("opens three add choices and dispatches the selected action", async () => {
  const user = userEvent.setup();
  const selectLocalDiff = vi.fn();
  render(
    <AttachmentMenu
      onSelectLocalFiles={vi.fn()}
      onSelectLocalDiff={selectLocalDiff}
      onSelectGitLab={vi.fn()}
    />,
  );

  const trigger = screen.getByRole("button", { name: "添加内容" });
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  await user.click(trigger);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("menuitem", { name: /选择本地文件/ })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /本地版本对比/ })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /从 GitLab 导入/ })).toBeInTheDocument();

  await user.click(screen.getByRole("menuitem", { name: /本地版本对比/ }));
  expect(selectLocalDiff).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("closes the add menu with Escape and restores trigger focus", async () => {
  const user = userEvent.setup();
  render(
    <AttachmentMenu
      onSelectLocalFiles={vi.fn()}
      onSelectLocalDiff={vi.fn()}
      onSelectGitLab={vi.fn()}
    />,
  );
  const trigger = screen.getByRole("button", { name: "添加内容" });
  await user.click(trigger);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

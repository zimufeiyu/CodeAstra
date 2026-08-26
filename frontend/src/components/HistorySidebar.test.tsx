import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import type { ReviewHistoryItem } from "../api/client";
import { HistorySidebar } from "./HistorySidebar";

function item(reviewId: string, title: string, daysAgo = 0): ReviewHistoryItem {
  const created = new Date();
  created.setDate(created.getDate() - daysAgo);
  return {
    review_id: reviewId,
    title,
    mode: "paste",
    status: "completed",
    created_at: created.toISOString(),
    expires_at: created.toISOString(),
    file_count: 1,
    file_names: ["snippet.py"],
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
}

it("shows persistent grouped history without an upload navigation action", () => {
  render(
    <HistorySidebar
      items={[item("today", "今天审查"), item("week", "本周审查", 3), item("old", "更早审查", 10)]}
      activeReviewId="today"
      onNewReview={() => undefined}
      onOpen={() => undefined}
      onRename={() => undefined}
      onDelete={() => undefined}
    />,
  );

  expect(screen.getByRole("button", { name: "新建审查" })).toBeInTheDocument();
  expect(screen.getByLabelText("今天")).toBeInTheDocument();
  expect(screen.getByLabelText("过去 7 天")).toBeInTheDocument();
  expect(screen.getByLabelText("更早")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "上传文件" })).not.toBeInTheDocument();
});

it("renames inline and requires confirmation before permanent deletion", async () => {
  const user = userEvent.setup();
  const onRename = vi.fn().mockResolvedValue(undefined);
  const onDelete = vi.fn().mockResolvedValue(undefined);
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
  render(
    <HistorySidebar
      items={[item("review-1", "代码审查")]}
      activeReviewId="review-1"
      onNewReview={() => undefined}
      onOpen={() => undefined}
      onRename={onRename}
      onDelete={onDelete}
    />,
  );

  await user.click(screen.getByRole("button", { name: "更多操作 代码审查" }));
  await user.click(screen.getByRole("menuitem", { name: "重命名" }));
  const editor = screen.getByRole("textbox", { name: "重命名 代码审查" });
  await user.clear(editor);
  await user.type(editor, "订单服务审查{Enter}");
  await waitFor(() => expect(onRename).toHaveBeenCalledWith("review-1", "订单服务审查"));

  await user.click(screen.getByRole("button", { name: "更多操作 代码审查" }));
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(onDelete).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "更多操作 代码审查" }));
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  await waitFor(() => expect(onDelete).toHaveBeenCalledWith("review-1"));
  expect(confirm).toHaveBeenCalledTimes(2);
});

it("keeps 101 grouped records in one scroll surface and exposes paged loading states", async () => {
  const user = userEvent.setup();
  const onLoadMore = vi.fn().mockResolvedValue(undefined);
  const records = Array.from({ length: 101 }, (_, index) => (
    item(`review-${index}`, `很长的历史记录标题 ${index}`)
  ));
  const { container, rerender } = render(
    <HistorySidebar
      items={records}
      activeReviewId="review-100"
      onNewReview={() => undefined}
      onOpen={() => undefined}
      onRename={() => undefined}
      onDelete={() => undefined}
      hasMore
      onLoadMore={onLoadMore}
    />,
  );

  expect(container.querySelectorAll(".history-title-button")).toHaveLength(101);
  expect(container.querySelectorAll(".history-scroll")).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "加载更多" }));
  expect(onLoadMore).toHaveBeenCalledTimes(1);

  rerender(
    <HistorySidebar
      items={records}
      activeReviewId="review-100"
      onNewReview={() => undefined}
      onOpen={() => undefined}
      onRename={() => undefined}
      onDelete={() => undefined}
      hasMore={false}
      loadError=""
    />,
  );
  expect(screen.getByText("已加载全部")).toBeInTheDocument();
});

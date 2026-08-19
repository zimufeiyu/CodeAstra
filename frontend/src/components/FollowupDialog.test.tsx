import { render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { FollowupDialog } from "./FollowupDialog";

it("labels follow-up assistant messages with the review's concrete model", () => {
  render(
    <FollowupDialog
      context={{
        kind: "selection",
        title: "snippet.py",
        detail: "print('ok')",
        fileId: "file-1",
      }}
      messages={[
        {
          message_id: "question-1",
          review_id: "review-1",
          role: "user",
          content: "Why?",
          created_at: "2026-08-11T00:00:00+00:00",
        },
        {
          message_id: "answer-1",
          review_id: "review-1",
          role: "assistant",
          content: "Because.",
          created_at: "2026-08-11T00:00:01+00:00",
        },
      ]}
      assistantName="DeepSeek V4 Flash"
      busy={false}
      onClose={vi.fn()}
      onSubmit={vi.fn()}
    />,
  );

  const messages = screen.getAllByRole("article");
  expect(within(messages[0]).getByText("\u4f60")).toBeInTheDocument();
  expect(within(messages[1]).getByText("DeepSeek V4 Flash")).toBeInTheDocument();
  expect(screen.queryByText("Qwen3-8B")).not.toBeInTheDocument();
});
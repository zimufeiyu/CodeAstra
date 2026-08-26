import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

it("keeps the draft when the isolated follow-up request fails", async () => {
  const user = userEvent.setup();
  const submit = vi.fn().mockResolvedValue(false);
  render(
    <FollowupDialog
      context={{
        kind: "finding",
        title: "问题 A",
        detail: "a = 1",
        fileId: "file-a",
        findingId: "finding-a",
      }}
      messages={[]}
      assistantName="Qwen3-8B"
      error="当前上下文请求失败"
      busy={false}
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  const input = screen.getByRole("textbox", { name: "针对当前代码追问" });
  await user.type(input, "保留这段输入");
  await user.click(screen.getByRole("button", { name: "发送追问" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith("保留这段输入"));
  expect(input).toHaveValue("保留这段输入");
  expect(screen.getByRole("alert")).toHaveTextContent("当前上下文请求失败");
});

it("uses one send action and explains conservative automatic routing", async () => {
  const user = userEvent.setup();
  const submit = vi.fn().mockResolvedValue(true);
  const { rerender } = render(
    <FollowupDialog
      context={{
        kind: "finding",
        title: "名称未定义：b",
        detail: "a = b",
        fileId: "file-a",
        findingId: "finding-a",
      }}
      messages={[]}
      assistantName="Qwen3-8B"
      busy={false}
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  const input = screen.getByRole("textbox", { name: "针对当前代码追问" });
  expect(screen.getAllByRole("button")).toHaveLength(2);
  expect(screen.getByText(/输入明确祈使句可生成受限修改候选/)).toBeInTheDocument();
  await user.type(input, "将 b 替换为空字典");
  await user.click(screen.getByRole("button", { name: "发送追问" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith("将 b 替换为空字典"));

  rerender(
    <FollowupDialog
      context={{
        kind: "selection",
        title: "snippet.py",
        detail: "a = b",
        fileId: "file-a",
      }}
      messages={[]}
      assistantName="Qwen3-8B"
      busy={false}
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  expect(screen.getByText(/代码选区仅支持解释追问/)).toBeInTheDocument();
});

it("keeps an automatically routed instruction when candidate generation fails", async () => {
  const user = userEvent.setup();
  const submit = vi.fn().mockResolvedValue(false);
  render(
    <FollowupDialog
      context={{
        kind: "finding",
        title: "名称未定义：b",
        detail: "a = b",
        fileId: "file-a",
        findingId: "finding-a",
      }}
      messages={[]}
      assistantName="Qwen3-8B"
      busy={false}
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  const input = screen.getByRole("textbox", { name: "针对当前代码追问" });
  await user.type(input, "删除无效分支");
  await user.click(screen.getByRole("button", { name: "发送追问" }));
  await waitFor(() => expect(submit).toHaveBeenCalled());
  expect(input).toHaveValue("删除无效分支");
});

it("sends explanation questions through the same single entry", async () => {
  const user = userEvent.setup();
  const submit = vi.fn().mockResolvedValue(true);
  render(
    <FollowupDialog
      context={{
        kind: "finding",
        title: "问题 A",
        detail: "a = b",
        fileId: "file-a",
        findingId: "finding-a",
      }}
      messages={[]}
      assistantName="Qwen3-8B"
      busy={false}
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  await user.type(screen.getByRole("textbox", { name: "针对当前代码追问" }), "只解释原因");
  await user.click(screen.getByRole("button", { name: "发送追问" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith("只解释原因"));
});

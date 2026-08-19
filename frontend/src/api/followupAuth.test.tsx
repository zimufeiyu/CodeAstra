import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { askReviewFollowup } from "./client";
import { FollowupDialog } from "../components/FollowupDialog";

afterEach(() => vi.unstubAllGlobals());

it("preserves the follow-up service's dedicated 502 detail", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "追问模型输出不完整，请缩短问题后重试。" }), { status: 502 })));
  await expect(askReviewFollowup("review-1", "为什么？")).rejects.toMatchObject({ status: 502, message: "追问模型输出不完整，请缩短问题后重试。" });
});

it("shows the failure inside the dialog and preserves the entered question", async () => {
  const user = userEvent.setup();
  render(<FollowupDialog context={{ kind: "selection", title: "snippet.py", detail: "print('ok')", fileId: "file-1" }} messages={[]} assistantName="本地 Qwen3-8B" error="追问模型输出不完整，请缩短问题后重试。" busy={false} onClose={vi.fn()} onSubmit={vi.fn()} />);
  const input = screen.getByRole("textbox", { name: "针对当前代码追问" });
  await user.type(input, "如何修复？");
  expect(screen.getByRole("alert")).toHaveTextContent("追问模型输出不完整，请缩短问题后重试。");
  expect(input).toHaveValue("如何修复？");
});

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  askReviewFollowup,
  decideReviewFinding,
  deleteReviewSession,
  getReviewFollowups,
  listReviewSessions,
  previewReviewFollowupFix,
  renameReviewSession,
} from "./client";

describe("review interaction API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads lightweight history and reads and writes follow-ups", async () => {
    const history = {
      items: [
        {
          review_id: "review-1",
          title: "代码审查 · 2026-08-04 00:00",
          mode: "paste",
          status: "completed",
          created_at: "2026-08-04T00:00:00+00:00",
          expires_at: "2026-08-05T00:00:00+00:00",
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
          error: null,
        },
      ],
      limit: 20,
      offset: 0,
    };
    const exchange = {
      messages: [
        {
          message_id: "question-1",
          review_id: "review-1",
          role: "user",
          content: "为什么危险？",
          created_at: "2026-08-04T01:00:00+00:00",
        },
        {
          message_id: "answer-1",
          review_id: "review-1",
          role: "assistant",
          content: "因为输入可能被执行。",
          created_at: "2026-08-04T01:00:01+00:00",
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(history), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(exchange), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(exchange), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listReviewSessions()).resolves.toEqual(history);
    await expect(getReviewFollowups("review-1")).resolves.toEqual(exchange.messages);
    const context = {
      kind: "finding" as const,
      file_id: "file-1",
      finding_id: "finding-1",
      selected_code: "eval(user_input)",
    };
    await expect(askReviewFollowup("review-1", "为什么危险？", context)).resolves.toEqual({
      action: "answer",
      messages: exchange.messages,
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/reviews?limit=20&offset=0");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/v1/reviews/review-1/followups");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/v1/reviews/review-1/followups");
    const post = fetchMock.mock.calls[2]?.[1] as RequestInit;
    expect(post.method).toBe("POST");
    expect(JSON.parse(String(post.body))).toEqual({ question: "为什么危险？", context });
  });
  it("renames and permanently deletes a review", async () => {
    const renamed = { review_id: "review-1", title: "订单服务性能审查" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(renamed), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(renameReviewSession("review-1", "订单服务性能审查")).resolves.toEqual(
      renamed,
    );
    await expect(deleteReviewSession("review-1")).resolves.toBeUndefined();

    expect(fetchMock.mock.calls[0]).toEqual([
      "/v1/reviews/review-1",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "订单服务性能审查" }),
      },
    ]);
    expect(fetchMock.mock.calls[1]).toEqual([
      "/v1/reviews/review-1",
      { method: "DELETE" },
    ]);
  });
  it("submits a finding decision", async () => {
    const result = {
      session: { review_id: "review-1", finding_decisions: { "finding-1": "keep" } },
      revised_review: null,
      explanation: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(result), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      decideReviewFinding("review-1", "finding-1", "deferred"),
    ).resolves.toEqual(result);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/reviews/review-1/findings/finding-1/decision",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: "deferred" }),
      },
    );
  });

  it("submits a bounded finding follow-up fix preview payload", async () => {
    const candidate = {
      candidate_id: "candidate-1",
      review_id: "review-1",
      finding_id: "finding-1",
      file_id: "file-1",
      relative_path: "snippet.py",
      created_at: "2026-08-21T00:00:00+00:00",
      expires_at: "2026-08-21T00:10:00+00:00",
      base_sha256: "a".repeat(64),
      after_sha256: "b".repeat(64),
      diff: "--- a/snippet.py\n+++ b/snippet.py\n",
      explanation: "按追问指令生成候选",
      validation: ["Python 语法解析通过"],
      output_token_budget: 256,
      finding_state: "candidate_ready",
      fix_safety: "requires_review",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ candidate, phase: "awaiting_confirmation" }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const payload = {
      instruction: "b 缺失时使用空字典",
      base_sha: "a".repeat(64),
      context: {
        kind: "finding" as const,
        file_id: "file-1",
        finding_id: "finding-1",
        selected_code: "a = b",
      },
    };

    await expect(
      previewReviewFollowupFix("review-1", payload, "deepseek-key"),
    ).resolves.toEqual(candidate);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/reviews/review-1/followups/fix-preview",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify(payload),
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "X-DeepSeek-API-Key": "deepseek-key",
        }),
      }),
    );
  });

});

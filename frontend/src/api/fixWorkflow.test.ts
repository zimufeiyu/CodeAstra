import { afterEach, expect, test, vi } from "vitest";

import { ApiError, cancelReviewFix, confirmReviewFix, previewReviewFix, previewReviewFixWithIntent } from "./client";
import { installAuthenticatedFetch } from "./authClient";

afterEach(() => vi.restoreAllMocks());

test("preview confirm and cancel use separate endpoints", async () => {
  const candidate = {
    candidate_id: "fix-1", review_id: "review-1", finding_id: "finding-1", file_id: "file-1",
    relative_path: "example.py", created_at: "now", expires_at: "later", base_sha256: "a".repeat(64),
    after_sha256: "b".repeat(64), diff: "diff", explanation: "reason", validation: [], output_token_budget: 1024,
  };
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ candidate }), { status: 200, headers: { "content-type": "application/json" } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ session: {}, revised_review: null, phase: "applied" }), { status: 200, headers: { "content-type": "application/json" } }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));

  expect((await previewReviewFix("review-1", "finding-1")).candidate_id).toBe("fix-1");
  await confirmReviewFix("review-1", "fix-1");
  await cancelReviewFix("review-1", "fix-1");

  expect(fetchMock.mock.calls[0][0]).toContain("/findings/finding-1/fix-preview");
  expect(fetchMock.mock.calls[1][0]).toContain("/fix-candidates/confirm");
  expect(fetchMock.mock.calls[1][1]?.body).toBe(JSON.stringify({ candidate_id: "fix-1" }));
  expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");
});

test("submits an explicit repair intent without a model default", async () => {
  installAuthenticatedFetch("csrf-token");
  const candidate = { candidate_id: "fix-intent" };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify({ candidate }), { status: 200, headers: { "content-type": "application/json" } }),
  );
  const intent = {
    review_id: "review-1",
    finding_id: "finding-1",
    base_sha: "a".repeat(64),
    option_id: "rename:value",
    intent_kind: "rename_existing" as const,
    selected_symbol: "value",
  };
  await previewReviewFixWithIntent("review-1", "finding-1", intent);
  expect(fetchMock.mock.calls[0][0]).toContain("/fix-preview/intent");
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual(intent);
  expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("X-CSRF-Token")).toBe("csrf-token");
});

test("sends custom behavior separately from a Python initializer", async () => {
  installAuthenticatedFetch("csrf-token");
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify({ candidate: { candidate_id: "fix-custom" } }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  const intent = {
    review_id: "review-1",
    finding_id: "finding-1",
    base_sha: "a".repeat(64),
    option_id: "custom_behavior",
    intent_kind: "custom_behavior" as const,
    initializer: null,
    user_intent: "缺少值时返回当前用户的默认配置",
  };
  await previewReviewFixWithIntent("review-1", "finding-1", intent);
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(body.user_intent).toBe("缺少值时返回当前用户的默认配置");
  expect(body.initializer).toBeNull();
});

test("needs_intent keeps its code, message and use-def context", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({
      detail: {
        error_code: "needs_intent",
        message: "已定位症状，但无法唯一确定开发者意图。",
        context: { base_sha: "a".repeat(64), use_def_evidence: { options: [] } },
      },
    }), { status: 409, headers: { "content-type": "application/json" } }),
  );
  const error = await previewReviewFix("review-1", "finding-1").catch((caught) => caught);
  expect(error).toMatchObject({
    code: "needs_intent",
    message: "已定位症状，但无法唯一确定开发者意图。",
    details: { base_sha: "a".repeat(64), use_def_evidence: { options: [] } },
  });
});

test("repair error exposes the backend reason code and message", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        detail: {
          code: "already_compliant",
          message: "函数参数已经全部符合 snake_case，未发现可执行的命名修复。",
        },
      }),
      { status: 409, headers: { "content-type": "application/json" } },
    ),
  );

  const error = await previewReviewFix("review-1", "finding-1").catch((caught) => caught);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({
    status: 409,
    code: "already_compliant",
    message: "函数参数已经全部符合 snake_case，未发现可执行的命名修复。",
  });
});

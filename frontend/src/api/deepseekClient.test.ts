import { afterEach, describe, expect, it, vi } from "vitest";

import {
  askReviewFollowup,
  createReviewSession,
  decideReviewFinding,
  getDeepSeekModels,
  resumeReviewSession,
  undoReviewRevision,
} from "./client";

afterEach(() => vi.restoreAllMocks());

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("DeepSeek user credential forwarding", () => {
  it("discovers account models without putting the key in the URL", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ models: [{ id: "deepseek-v4-flash", display_name: "DeepSeek V4 Flash" }] }),
    );
    await getDeepSeekModels("sk-private");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/integrations/deepseek/models",
      expect.objectContaining({ headers: { "X-DeepSeek-API-Key": "sk-private" } }),
    );
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("sk-private");
  });

  it("forwards the same key for create and every model-backed continuation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ review_id: "r1", status: "queued", expires_at: "later" }))
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse({ session: {}, revised_review: null, explanation: null }))
      .mockResolvedValueOnce(jsonResponse({ revised_review: {} }))
      .mockResolvedValueOnce(jsonResponse({ messages: [] }));

    await createReviewSession("paste", {
      language: "python",
      content: "print(1)",
      model_profile_id: "deepseek-api",
      deepseek_selection_mode: "auto",
    }, "sk-private");
    await resumeReviewSession("r1", "sk-private");
    await decideReviewFinding("r1", "f1", "keep", "sk-private");
    await undoReviewRevision("r1", "rev1", "sk-private");
    await askReviewFollowup("r1", "why?", undefined, "sk-private");

    for (const call of fetchMock.mock.calls) {
      const headers = new Headers((call[1]?.headers ?? {}) as HeadersInit);
      expect(headers.get("X-DeepSeek-API-Key")).toBe("sk-private");
      expect(JSON.stringify(call[1]?.body ?? "")).not.toContain("sk-private");
    }
  });
});

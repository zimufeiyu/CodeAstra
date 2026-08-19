import { afterEach, expect, it, vi } from "vitest";

import { createReviewSession, streamReviewSession } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("creates a pasted-code review through the session API", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        review_id: "review-1",
        status: "queued",
        expires_at: "2026-08-04T00:00:00+00:00",
      }),
      { status: 202, headers: { "Content-Type": "application/json" } },
    ),
  );
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    createReviewSession("paste", { language: "python", content: "print('ok')" }),
  ).resolves.toMatchObject({ review_id: "review-1" });
  expect(fetchMock).toHaveBeenCalledWith(
    "/v1/reviews/paste",
    expect.objectContaining({ method: "POST" }),
  );
});

it("consumes semantic events and loads the completed session", async () => {
  const eventBody = [
    'id: 1\nevent: stage\ndata: {"stage":"static_analysis","progress":20}\n\n',
    'id: 2\nevent: complete\ndata: {"review_id":"review-1"}\n\n',
  ].join("");
  const completed = {
    review_id: "review-1",
    mode: "paste",
    status: "completed",
    created_at: "2026-08-03T00:00:00+00:00",
    expires_at: "2026-08-04T00:00:00+00:00",
    files: [],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "完成" },
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(new Response(eventBody, { status: 200 }))
    .mockResolvedValueOnce(
      new Response(JSON.stringify(completed), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  vi.stubGlobal("fetch", fetchMock);
  const events: string[] = [];

  await expect(streamReviewSession("review-1", (event) => events.push(event.event))).resolves.toEqual(completed);
  expect(events).toEqual(["stage", "complete"]);
  expect(fetchMock).toHaveBeenNthCalledWith(1, "/v1/reviews/review-1/events", expect.objectContaining({ method: "GET" }));
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/v1/reviews/review-1", { method: "GET" });
});


it("reconnects from the last SSE event after a premature EOF", async () => {
  const reviewing = {
    review_id: "review-1",
    mode: "paste",
    status: "reviewing",
    created_at: "2026-08-03T00:00:00+00:00",
    expires_at: "2026-08-04T00:00:00+00:00",
    files: [],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "进行中" },
  };
  const completed = { ...reviewing, status: "completed", summary: { ...reviewing.summary, text: "完成" } };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(new Response(`id: 7
event: progress
data: {"completed":1,"total":2}
`))
    .mockResolvedValueOnce(new Response(JSON.stringify(reviewing), { status: 200 }))
    .mockResolvedValueOnce(new Response(`id: 8
event: complete
data: {"review_id":"review-1"}
`))
    .mockResolvedValueOnce(new Response(JSON.stringify(completed), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(streamReviewSession("review-1", () => undefined)).resolves.toMatchObject({ status: "completed" });
  expect(fetchMock).toHaveBeenNthCalledWith(
    3,
    "/v1/reviews/review-1/events",
    expect.objectContaining({ headers: expect.objectContaining({ "Last-Event-ID": "7" }) }),
  );
});

it("does not open a stream when the request is already stopped", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const controller = new AbortController();
  controller.abort();

  await expect(streamReviewSession("review-1", () => undefined, controller.signal)).rejects.toMatchObject({
    name: "AbortError",
  });
  expect(fetchMock).not.toHaveBeenCalled();
});

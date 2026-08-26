import { afterEach, expect, it, vi } from "vitest";

import { ApiError, createReviewSession, streamReviewSession } from "./client";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

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

it("maps a native fetch failure to an actionable CodeAstra connection error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

  const error = await createReviewSession("paste", {
    language: "python",
    content: "x = 1",
  }).catch((caught: unknown) => caught);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ code: "codeastra_unreachable", status: 0 });
  expect((error as Error).message).toContain("无法连接 CodeAstra 服务");
  expect((error as Error).message).not.toContain("Failed to fetch");
});

it("accepts a 32148-byte 985-line paste and reaches SSE without a fetch error", async () => {
  const content = Array.from(
    { length: 985 },
    (_, index) => "x".repeat(31 + (index < 629 ? 1 : 0)),
  ).join("\n");
  expect(new TextEncoder().encode(content)).toHaveLength(32148);
  expect(content.split("\n")).toHaveLength(985);
  const failed = {
    review_id: "review-large-paste",
    mode: "paste",
    status: "failed",
    created_at: "2026-08-25T00:00:00+00:00",
    expires_at: "2026-08-26T00:00:00+00:00",
    files: [],
    findings: [],
    coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "模型待恢复" },
    error: "本地模型服务未运行或正在恢复。已创建的审查已保留，可重新复查。",
    error_code: "local_model_circuit_open",
  };
  const terminal = 'id: 2\nevent: error\ndata: {"code":"local_model_circuit_open","retryable":true}\n\n';
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      review_id: "review-large-paste",
      status: "queued",
      expires_at: "2026-08-26T00:00:00+00:00",
    }), { status: 202, headers: { "Content-Type": "application/json" } }))
    .mockResolvedValueOnce(new Response(terminal, { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(failed), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  vi.stubGlobal("fetch", fetchMock);

  const created = await createReviewSession("paste", { language: "python", content });
  const session = await streamReviewSession(created.review_id, () => undefined);

  expect(created.review_id).toBe("review-large-paste");
  expect(session).toMatchObject({ status: "failed", error_code: "local_model_circuit_open" });
  expect(fetchMock).toHaveBeenNthCalledWith(1, "/v1/reviews/paste", expect.objectContaining({ method: "POST" }));
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/v1/reviews/review-large-paste/events", expect.objectContaining({ method: "GET" }));
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

it("ends recheck busy state from a terminal timeout event and failed snapshot", async () => {
  const failed = {
    review_id: "review-1",
    mode: "paste",
    status: "failed",
    created_at: "2026-08-03T00:00:00+00:00",
    expires_at: "2026-08-04T00:00:00+00:00",
    files: [],
    findings: [],
    coverage: [],
    summary: {
      total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0,
      text: "修复已应用并保留；统一复查超时，可重新复查。",
    },
    error: "修复已应用并保留；统一复查超时，可重新复查。",
    finding_states: { "finding-1": "fixed_pending_revalidation" },
    recheck_attempt_id: "recheck-1",
    recheck_attempt_status: "timed_out",
  };
  const terminal = [
    "id: 9",
    "event: error",
    'data: {"code":"revalidation_timeout","retryable":true,"terminal":true,"recheck_attempt_id":"recheck-1"}',
    "",
    "",
  ].join("\n");
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(terminal, { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(failed), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  vi.stubGlobal("fetch", fetchMock);
  const events: string[] = [];

  await expect(streamReviewSession("review-1", (event) => events.push(event.event)))
    .resolves.toMatchObject({ status: "failed", recheck_attempt_status: "timed_out" });
  expect(events).toEqual(["error"]);
  expect(fetchMock).toHaveBeenCalledTimes(2);
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

it("retries a transient fetch failure without creating another review", async () => {
  vi.useFakeTimers();
  const completed = {
    review_id: "review-1", mode: "paste", status: "completed",
    created_at: "2026-08-03T00:00:00+00:00", expires_at: "2026-08-04T00:00:00+00:00",
    files: [], findings: [], coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "完成" },
  };
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new TypeError("network reset"))
    .mockResolvedValueOnce(new Response('id: 2\nevent: complete\ndata: {"review_id":"review-1"}\n\n'))
    .mockResolvedValueOnce(new Response(JSON.stringify(completed), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const events: Array<Record<string, unknown>> = [];

  const result = streamReviewSession("review-1", (event) => events.push(event.data));
  await vi.runAllTimersAsync();

  await expect(result).resolves.toMatchObject({ status: "completed" });
  expect(events).toContainEqual(expect.objectContaining({ stage: "connection_reconnecting", attempt: 1 }));
  vi.useRealTimers();
});

it("keeps Last-Event-ID when reader.read fails", async () => {
  vi.useFakeTimers();
  const encoder = new TextEncoder();
  let pullCount = 0;
  const interrupted = new ReadableStream({
    pull(controller) {
      if (pullCount++ === 0) controller.enqueue(encoder.encode('id: 7\nevent: progress\ndata: {"completed":1}\n\n'));
      else controller.error(new TypeError("connection lost"));
    },
  });
  const completed = {
    review_id: "review-1", mode: "paste", status: "completed",
    created_at: "2026-08-03T00:00:00+00:00", expires_at: "2026-08-04T00:00:00+00:00",
    files: [], findings: [], coverage: [],
    summary: { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0, text: "完成" },
  };
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(interrupted))
    .mockResolvedValueOnce(new Response('id: 8\nevent: complete\ndata: {}\n\n'))
    .mockResolvedValueOnce(new Response(JSON.stringify(completed), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);

  const result = streamReviewSession("review-1", () => undefined);
  await vi.runAllTimersAsync();

  await expect(result).resolves.toMatchObject({ status: "completed" });
  expect(fetchMock).toHaveBeenNthCalledWith(
    2,
    "/v1/reviews/review-1/events",
    expect.objectContaining({ headers: expect.objectContaining({ "Last-Event-ID": "7" }) }),
  );
  vi.useRealTimers();
});

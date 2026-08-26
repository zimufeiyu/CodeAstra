import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  decideReviewFinding,
  getInstanceHealth,
  reviewCode,
  reviewCodeStream,
} from "./client";

describe("review API client", () => {
  beforeEach(() => {
    vi.stubGlobal("crypto", { randomUUID: () => "review-request-1" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts code review requests to the same-origin review endpoint", async () => {
    const responseBody = {
      summary: "ok",
      findings: [],
      uncovered: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await reviewCode("print('hello')");

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/review",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
      }),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const payload = JSON.parse(String(request.body));
    expect(payload).toMatchObject({
      request_id: "review-request-1",
      model: "qwen3-8b",
      max_output_tokens: 32768,
      temperature: 0,
    });
    expect(payload.messages[0]).toMatchObject({ role: "system" });
    expect(payload.messages[0].content).toContain("中文");
    expect(payload.messages[1]).toEqual({
      role: "user",
      content: "print('hello')",
    });
  });

  it("loads gateway instance health", async () => {
    const responseBody = {
      instances: [
        {
          endpoint_id: "ppu-0",
          inflight_requests: 0,
          inflight_tokens: 0,
          circuit_open: false,
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getInstanceHealth()).resolves.toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith("/health/instances", { method: "GET" });
  });

  it("raises a sanitized Chinese error for unavailable gateway responses", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("internal stack secret", { status: 503 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(reviewCode("x = 1")).rejects.toMatchObject({
      status: 503,
      message: "模型服务暂时不可用，请稍后重试。",
    });

    await expect(reviewCode("x = 1")).rejects.toBeInstanceOf(ApiError);
    await expect(reviewCode("x = 1")).rejects.not.toThrow("internal stack secret");
  });

  it("raises a specific Chinese error when model output is incomplete", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "raw backend detail" }), { status: 502 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(reviewCode("x = 1")).rejects.toMatchObject({
      status: 502,
      message: "模型未能生成完整的审查结果，请缩短输入或稍后重试。",
    });
  });

  it("shows the backend Chinese detail for invalid input", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "无法确定是 Python 还是 C++ 代码" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(reviewCode("ambiguous code")).rejects.toMatchObject({
      status: 400,
      message: "无法确定是 Python 还是 C++ 代码",
    });
  });

  it("shows the backend repair detail for finding decision failures", async () => {
    const detail = "模型返回的修复内容不完整，原代码和问题已保留，请重试。";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      decideReviewFinding("review-1", "finding-1", "accepted_risk"),
    ).rejects.toMatchObject({ status: 502, message: detail });
  });

});


it("parses SSE review events and returns the final validated result", async () => {
  const body = [
    'event: status\ndata: {"stage":"generating"}\n\n',
    'event: delta\ndata: {"content":"{\\"summary\\":\\"旧"}\n\n',
    'event: reset\ndata: {"message":"正在精简"}\n\n',
    'event: delta\ndata: {"content":"{\\"summary\\":\\"ok\\"}"}\n\n',
    'event: final\ndata: {"summary":"ok","findings":[],"uncovered":[]}\n\n',
  ].join("");
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const events: string[] = [];
  const controller = new AbortController();

  await expect(
    reviewCodeStream("print(1)", (event) => events.push(event.event), controller.signal),
  ).resolves.toEqual({ summary: "ok", findings: [], uncovered: [] });
  expect(events).toEqual(["status", "delta", "reset", "delta", "final"]);
  const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
  expect(request.signal).toBe(controller.signal);
});

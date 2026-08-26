import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.resetModules();
});

it("dispatches one auth-expired event for concurrent protected 401 responses", async () => {
  const nativeFetch = vi.fn().mockResolvedValue(new Response("{}", { status: 401 }));
  vi.stubGlobal("fetch", nativeFetch);
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const { AUTH_EXPIRED_EVENT, installAuthenticatedFetch } = await import("./authClient");
  installAuthenticatedFetch("csrf");

  await Promise.all([
    fetch("/v1/reviews/one"),
    fetch("/v1/reviews/two"),
  ]);

  expect(dispatch.mock.calls.filter(([event]) => event.type === AUTH_EXPIRED_EVENT)).toHaveLength(1);
});

it("does not turn an invalid login 401 into an auth-expired event", async () => {
  const nativeFetch = vi.fn().mockResolvedValue(new Response("{}", { status: 401 }));
  vi.stubGlobal("fetch", nativeFetch);
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const { AUTH_EXPIRED_EVENT, installAuthenticatedFetch } = await import("./authClient");
  installAuthenticatedFetch("csrf");

  await fetch("/v1/auth/login", { method: "POST" });

  expect(dispatch.mock.calls.filter(([event]) => event.type === AUTH_EXPIRED_EVENT)).toHaveLength(0);
});

it("preserves the structured Chinese session-expired error contract", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: {
      error_code: "session_expired",
      message: "账号已在其他设备登录或会话已过期，请重新登录。",
    },
  }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  })));
  const { getCurrentUser } = await import("./authClient");

  await expect(getCurrentUser()).rejects.toMatchObject({
    status: 401,
    code: "session_expired",
    detail: "账号已在其他设备登录或会话已过期，请重新登录。",
  });
});

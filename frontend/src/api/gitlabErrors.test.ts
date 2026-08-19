import { afterEach, expect, it, vi } from "vitest";

import { ApiError, verifyGitLabAccount } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("shows the sanitized GitLab timeout detail instead of a model-service error", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ detail: "连接 GitLab 超时，请稍后重试。" }), {
      status: 504,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    verifyGitLabAccount("https://gitlab.cigai.cn:1443/", "private-token"),
  ).rejects.toMatchObject({
    status: 504,
    message: "连接 GitLab 超时，请稍后重试。",
  });
  await expect(
    verifyGitLabAccount("https://gitlab.cigai.cn:1443/", "private-token"),
  ).rejects.toBeInstanceOf(ApiError);
});

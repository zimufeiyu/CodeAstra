import { afterEach, expect, it, vi } from "vitest";

import { downloadArtifact, safeArtifactFilename } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("parses RFC filenames and removes path or platform-unsafe content", () => {
  expect(safeArtifactFilename("attachment; filename*=UTF-8''%E4%BF%AE%E5%A4%8D.patch", "fallback.patch"))
    .toBe("修复.patch");
  expect(safeArtifactFilename('attachment; filename="../../bad:name.patch"', "fallback.patch"))
    .toBe("bad-name.patch");
  expect(safeArtifactFilename('attachment; filename="CON"', "fallback.patch"))
    .toBe("download-fallback.patch");
});

it("downloads through fetch once and always revokes the object URL", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("patch", {
    status: 200,
    headers: { "Content-Disposition": "attachment; filename*=UTF-8''review.patch" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const createObjectURL = vi.fn().mockReturnValue("blob:artifact");
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

  await expect(downloadArtifact("/v1/reviews/one/fixes.patch", "one.patch"))
    .resolves.toBe("review.patch");

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(click).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:artifact");
  expect(document.querySelector('a[href="blob:artifact"]')).toBeNull();
});

it.each([
  [401, "会话"],
  [404, "不存在"],
  [409, "修订"],
])("surfaces a recoverable %s download error", async (status, expected) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status })));
  await expect(downloadArtifact("/artifact", "artifact.zip")).rejects.toMatchObject({
    status,
    message: expect.stringContaining(expected),
  });
});

it("keeps network interruption retryable without creating an object URL", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
  const createObjectURL = vi.fn();
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });

  await expect(downloadArtifact("/artifact", "artifact.zip")).rejects.toMatchObject({
    code: "download_interrupted",
    message: expect.stringContaining("下载连接中断"),
  });
  expect(createObjectURL).not.toHaveBeenCalled();
});

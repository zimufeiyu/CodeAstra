import { beforeEach, describe, expect, it } from "vitest";
import { defaultDeepSeekSettings, loadDeepSeekSettings, persistDeepSeekSettings } from "./deepseekSettings";

describe("DeepSeek browser settings", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("starts with automatic selection and no key", () => {
    expect(loadDeepSeekSettings()).toEqual(defaultDeepSeekSettings);
  });

  it("keeps settings in the account-isolated browser namespace", () => {
    persistDeepSeekSettings({
      apiKey: "sk-user-only",
      selectionMode: "manual",
      manualModel: "deepseek-v4-pro",
      preferredProfileId: "deepseek-api",
    }, "user-1");
    expect(window.localStorage.getItem("code-review.user.user-1.deepseek-preferences.v3")).not.toContain("sk-user-only");
    expect(window.localStorage.getItem("code-review.user.user-1.deepseek-api-key.v1")).toBe("sk-user-only");
    expect(loadDeepSeekSettings("user-1")).toEqual({
      apiKey: "sk-user-only",
      selectionMode: "manual",
      manualModel: "deepseek-v4-pro",
      preferredProfileId: "deepseek-api",
    });
    expect(loadDeepSeekSettings("user-2")).toEqual(defaultDeepSeekSettings);
  });

  it("removes a legacy record containing a key when settings are next saved", () => {
    window.localStorage.setItem("code-review.deepseek-settings.v1", JSON.stringify({ apiKey: "old" }));
    persistDeepSeekSettings(defaultDeepSeekSettings);
    expect(window.localStorage.getItem("code-review.deepseek-settings.v1")).toBeNull();
  });
});

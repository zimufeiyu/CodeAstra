import { beforeEach, describe, expect, it } from "vitest";
import {
  defaultDeepSeekSettings,
  isPersistentDeepSeekStorageKey,
  loadDeepSeekSettings,
  persistDeepSeekSettings,
} from "./deepseekSettings";

describe("DeepSeek browser settings", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("starts with the local profile and no key", () => {
    expect(loadDeepSeekSettings("alice")).toEqual(defaultDeepSeekSettings);
  });

  it("keeps a validated binding and preferred profile isolated by user", () => {
    persistDeepSeekSettings({
      apiKey: "sk-alice-only",
      selectionMode: "manual",
      manualModel: "deepseek-v4-pro",
      preferredProfileId: "deepseek-api",
    }, "alice-id");

    expect(loadDeepSeekSettings("alice-id")).toEqual({
      apiKey: "sk-alice-only",
      selectionMode: "manual",
      manualModel: "deepseek-v4-pro",
      preferredProfileId: "deepseek-api",
    });
    expect(loadDeepSeekSettings("bob-id")).toEqual(defaultDeepSeekSettings);
    expect(window.localStorage.getItem("code-review.user.alice-id.deepseek-api-key.v1"))
      .toBe("sk-alice-only");
    expect(window.localStorage.getItem("code-review.deepseek-preferences.v2")).toBeNull();
  });

  it("recognizes only the account-scoped DeepSeek records as persistent bindings", () => {
    expect(isPersistentDeepSeekStorageKey("code-review.user.alice.deepseek-api-key.v1")).toBe(true);
    expect(isPersistentDeepSeekStorageKey("code-review.user.alice.deepseek-preferences.v3")).toBe(true);
    expect(isPersistentDeepSeekStorageKey("code-review.gitlab-accounts.v1")).toBe(false);
  });

  it("migrates and removes legacy browser settings when saved", () => {
    window.localStorage.setItem("code-review.deepseek-preferences.v2", JSON.stringify({
      selectionMode: "manual",
      manualModel: "deepseek-v3",
    }));
    window.sessionStorage.setItem("code-review.deepseek-api-key.session", "old-session-key");
    expect(loadDeepSeekSettings("alice").apiKey).toBe("old-session-key");

    persistDeepSeekSettings({
      apiKey: "new-key",
      selectionMode: "auto",
      manualModel: "",
      preferredProfileId: "deepseek-api",
    }, "alice");
    expect(window.localStorage.getItem("code-review.deepseek-preferences.v2")).toBeNull();
    expect(window.sessionStorage.getItem("code-review.deepseek-api-key.session")).toBeNull();
  });
});
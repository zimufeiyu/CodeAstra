export type DeepSeekSelectionMode = "auto" | "manual";

export type DeepSeekSettings = {
  apiKey: string;
  selectionMode: DeepSeekSelectionMode;
  manualModel: string;
  preferredProfileId: string;
};

const LEGACY_STORAGE_KEY = "code-review.deepseek-settings.v1";
const LEGACY_PREFERENCES_KEY = "code-review.deepseek-preferences.v2";
const LEGACY_API_KEY_SESSION_KEY = "code-review.deepseek-api-key.session";

function storageNamespace(userId?: string): string {
  return `code-review.user.${encodeURIComponent(userId || "anonymous")}`;
}

function preferencesKey(userId?: string): string {
  return `${storageNamespace(userId)}.deepseek-preferences.v3`;
}

function apiKeyStorageKey(userId?: string): string {
  return `${storageNamespace(userId)}.deepseek-api-key.v1`;
}

export function isPersistentDeepSeekStorageKey(key: string): boolean {
  return key.startsWith("code-review.user.")
    && (key.endsWith(".deepseek-preferences.v3") || key.endsWith(".deepseek-api-key.v1"));
}

export const defaultDeepSeekSettings: DeepSeekSettings = {
  apiKey: "",
  selectionMode: "auto",
  manualModel: "",
  preferredProfileId: "local-qwen3-8b",
};

export function loadDeepSeekSettings(userId?: string): DeepSeekSettings {
  try {
    const raw = window.localStorage.getItem(preferencesKey(userId))
      ?? window.localStorage.getItem(LEGACY_PREFERENCES_KEY);
    const value = raw ? (JSON.parse(raw) as Partial<DeepSeekSettings>) : {};
    return {
      apiKey: window.localStorage.getItem(apiKeyStorageKey(userId))
        ?? window.sessionStorage.getItem(LEGACY_API_KEY_SESSION_KEY)
        ?? "",
      selectionMode: value.selectionMode === "manual" ? "manual" : "auto",
      manualModel: typeof value.manualModel === "string" ? value.manualModel : "",
      preferredProfileId: typeof value.preferredProfileId === "string"
        ? value.preferredProfileId
        : defaultDeepSeekSettings.preferredProfileId,
    };
  } catch {
    return { ...defaultDeepSeekSettings };
  }
}

export function persistDeepSeekSettings(settings: DeepSeekSettings, userId?: string): void {
  if (settings.apiKey) window.localStorage.setItem(apiKeyStorageKey(userId), settings.apiKey);
  else window.localStorage.removeItem(apiKeyStorageKey(userId));
  window.localStorage.setItem(preferencesKey(userId), JSON.stringify({
    selectionMode: settings.selectionMode,
    manualModel: settings.manualModel,
    preferredProfileId: settings.preferredProfileId,
  }));
  window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  window.localStorage.removeItem(LEGACY_PREFERENCES_KEY);
  window.sessionStorage.removeItem(LEGACY_API_KEY_SESSION_KEY);
}
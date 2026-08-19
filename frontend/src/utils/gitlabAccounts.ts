import type { GitLabAccountProfile } from "../api/client";

export type SavedGitLabAccount = GitLabAccountProfile & {
  account_id: string;
  private_token: string;
  saved_at: string;
};

const ACCOUNTS_KEY = "code-review.gitlab-accounts.v1";
const ACTIVE_KEY = "code-review.gitlab-active-account.v1";

function isSavedAccount(value: unknown): value is SavedGitLabAccount {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<SavedGitLabAccount>;
  return Boolean(
    item.account_id
      && item.gitlab_host
      && item.username
      && item.private_token
      && typeof item.user_id === "number",
  );
}

export function loadGitLabAccounts(): SavedGitLabAccount[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(ACCOUNTS_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter(isSavedAccount) : [];
  } catch {
    return [];
  }
}

export function persistGitLabAccounts(accounts: SavedGitLabAccount[]): void {
  window.localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts));
}

export function loadActiveGitLabAccountId(): string | null {
  return window.localStorage.getItem(ACTIVE_KEY);
}

export function persistActiveGitLabAccountId(accountId: string | null): void {
  if (accountId) window.localStorage.setItem(ACTIVE_KEY, accountId);
  else window.localStorage.removeItem(ACTIVE_KEY);
}

export function buildSavedGitLabAccount(
  profile: GitLabAccountProfile,
  privateToken: string,
  existing?: SavedGitLabAccount,
): SavedGitLabAccount {
  return {
    ...profile,
    account_id: existing?.account_id ?? (
      globalThis.crypto?.randomUUID?.()
      ?? `gitlab-${profile.user_id}-${Date.now()}`
    ),
    private_token: privateToken,
    saved_at: new Date().toISOString(),
  };
}

import { beforeEach, expect, test } from "vitest";

import {
  buildSavedGitLabAccount,
  loadActiveGitLabAccountId,
  loadGitLabAccounts,
  persistActiveGitLabAccountId,
  persistGitLabAccounts,
} from "./gitlabAccounts";

beforeEach(() => window.localStorage.clear());

test("persists saved GitLab accounts and the active connection", () => {
  const account = buildSavedGitLabAccount(
    {
      gitlab_host: "https://gitlab.example.com",
      user_id: 42,
      username: "reviewer",
      name: "Code Reviewer",
    },
    "token",
  );
  persistGitLabAccounts([account]);
  persistActiveGitLabAccountId(account.account_id);

  expect(loadGitLabAccounts()).toEqual([account]);
  expect(loadActiveGitLabAccountId()).toBe(account.account_id);
});

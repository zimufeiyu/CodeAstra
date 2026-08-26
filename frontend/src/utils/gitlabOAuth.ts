import type { GitLabFileChange, GitLabMergeRequestPreview } from "../api/client";

const DEFAULT_HOST = "https://gitlab.cigai.cn:1443";
const clientId = (import.meta.env.VITE_GITLAB_OAUTH_CLIENT_ID ?? "").trim();
export const GITLAB_HOST = (import.meta.env.VITE_GITLAB_BASE_URL ?? DEFAULT_HOST).replace(/\/$/, "");
export const GITLAB_REDIRECT_URIS = (import.meta.env.VITE_GITLAB_OAUTH_REDIRECT_URIS ?? "http://172.25.9.106:8081/oauth/callback,http://127.0.0.1:8081/oauth/callback")
  .split(",").map((item: string) => item.trim()).filter(Boolean);

type PendingOAuth = { state: string; verifier: string; redirectUri: string };
let pendingOAuth: PendingOAuth | null = null;
const PENDING_KEY = "codeastra.gitlab.oauth.pending.v1";
let accessToken: string | null = null;

export function gitLabOAuthConfigured(): boolean { return Boolean(clientId && GITLAB_REDIRECT_URIS.length); }
export function defaultGitLabRedirectUri(): string {
  const local = `${window.location.origin}/oauth/callback`;
  return GITLAB_REDIRECT_URIS.includes(local) ? local : GITLAB_REDIRECT_URIS[0];
}

function randomUrlSafe(bytes = 32): string {
  const data = new Uint8Array(bytes);
  const cryptoApi = requirePkceCrypto();
  cryptoApi.getRandomValues(data);
  return btoa(String.fromCharCode(...data)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function requirePkceCrypto(): Crypto {
  const cryptoApi = globalThis.crypto;
  if (globalThis.isSecureContext === false || !cryptoApi?.getRandomValues || !cryptoApi.subtle?.digest) {
    throw new Error("当前地址无法安全生成 GitLab PKCE 验证信息。请通过 http://127.0.0.1:8081 的 SSH 隧道访问，或请管理员启用 HTTPS；系统不会降级为弱哈希或明文验证。");
  }
  return cryptoApi;
}

export async function createGitLabPkceChallenge(value: string): Promise<string> {
  const cryptoApi = requirePkceCrypto();
  const digest = await cryptoApi.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function beginGitLabOAuth(redirectUri = GITLAB_REDIRECT_URIS[0]): Promise<void> {
  if (!gitLabOAuthConfigured()) throw new Error("GitLab OAuth 尚未配置 Client ID。 ");
  if (!GITLAB_REDIRECT_URIS.includes(redirectUri)) throw new Error("GitLab OAuth 回调地址不在允许列表中。");
  requirePkceCrypto();
  const verifier = randomUrlSafe(48);
  const state = randomUrlSafe(32);
  pendingOAuth = { state, verifier, redirectUri };
  sessionStorage.setItem(PENDING_KEY, JSON.stringify(pendingOAuth));
  const challenge = await createGitLabPkceChallenge(verifier);
  const params = new URLSearchParams({ client_id: clientId, redirect_uri: redirectUri, response_type: "code", state, scope: "read_user read_api read_repository", code_challenge: challenge, code_challenge_method: "S256" });
  window.location.assign(`${GITLAB_HOST}/oauth/authorize?${params.toString()}`);
}

export async function consumeGitLabOAuthCallback(locationLike: Location = window.location): Promise<string | null> {
  const query = new URLSearchParams(locationLike.search);
  const code = query.get("code");
  const returnedState = query.get("state");
  if (!code && !returnedState) return null;
  if (!pendingOAuth) {
    try { pendingOAuth = JSON.parse(sessionStorage.getItem(PENDING_KEY) ?? "null") as PendingOAuth | null; } catch { pendingOAuth = null; }
  }
  if (!code || !returnedState || !pendingOAuth || returnedState !== pendingOAuth.state) throw new Error("GitLab OAuth 安全校验失败，请重新连接。");
  const pending = pendingOAuth;
  pendingOAuth = null;
  sessionStorage.removeItem(PENDING_KEY);
  const response = await fetch(`${GITLAB_HOST}/oauth/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ client_id: clientId, code, grant_type: "authorization_code", redirect_uri: pending.redirectUri, code_verifier: pending.verifier }) });
  if (!response.ok) throw new Error(response.status === 400 ? "GitLab 授权码已失效，请重新连接。" : "GitLab 授权失败，请检查网络或应用权限。");
  const payload = await response.json() as { access_token?: string };
  if (!payload.access_token) throw new Error("GitLab 未返回可用授权令牌。");
  accessToken = payload.access_token;
  return accessToken;
}

export function clearGitLabOAuth(): void { accessToken = null; pendingOAuth = null; sessionStorage.removeItem(PENDING_KEY); }
export function hasGitLabOAuthToken(): boolean { return Boolean(accessToken); }

export type GitLabProject = { id: number; path_with_namespace: string; name: string; default_branch: string | null; web_url: string };
export type GitLabBranch = { name: string; web_url: string; commit?: { id?: string; short_id?: string } };
export type GitLabMergeRequestListItem = { iid: number; title: string; state: string; source_branch: string; target_branch: string; web_url: string; updated_at: string };

async function jsonApi<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await gitlabFetch(path, signal);
  return response.json() as Promise<T>;
}

export function hasGitLabTokenForTests(): boolean { return Boolean(accessToken); }
export async function getGitLabCurrentUser(signal?: AbortSignal): Promise<{ id: number; username: string; name: string; web_url?: string }> {
  return jsonApi("/user", signal);
}
export async function listGitLabProjects(page = 1, search = "", signal?: AbortSignal): Promise<GitLabProject[]> {
  const params = new URLSearchParams({ page: String(page), per_page: "50", order_by: "last_activity_at", sort: "desc" });
  if (search.trim()) params.set("search", search.trim().slice(0, 80));
  return jsonApi(`/projects?membership=true&${params.toString()}`, signal);
}
export async function listGitLabBranches(projectId: number, page = 1, search = "", signal?: AbortSignal): Promise<GitLabBranch[]> {
  const params = new URLSearchParams({ page: String(page), per_page: "50" });
  if (search.trim()) params.set("search", search.trim().slice(0, 80));
  return jsonApi(`/projects/${projectId}/repository/branches?${params.toString()}`, signal);
}
export async function listGitLabMergeRequests(projectId: number, page = 1, search = "", state = "opened", signal?: AbortSignal): Promise<GitLabMergeRequestListItem[]> {
  const params = new URLSearchParams({ page: String(page), per_page: "50", state });
  if (search.trim()) params.set("search", search.trim().slice(0, 80));
  return jsonApi(`/projects/${projectId}/merge_requests?${params.toString()}`, signal);
}
export async function previewGitLabProjectMergeRequest(projectId: number, iid: number, signal?: AbortSignal): Promise<GitLabMergeRequestPreview> {
    const project = await jsonApi<{ path_with_namespace?: string }>(`/projects/${projectId}`, signal);
    const mr = await jsonApi<{ title: string; web_url: string; diff_refs?: { base_sha?: string; head_sha?: string }; changes?: Array<{ old_path: string; new_path: string; new_file: boolean; deleted_file: boolean; renamed_file: boolean; diff: string }> }>(`/projects/${projectId}/merge_requests/${iid}/changes`, signal);
    const baseSha = mr.diff_refs?.base_sha; const headSha = mr.diff_refs?.head_sha;
    if (!baseSha || !headSha) throw new Error("GitLab 未提供稳定的 base/head SHA，无法安全导入。");
    const changes = mr.changes ?? [];
    const files: GitLabFileChange[] = [];
    for (const change of changes.slice(0, 200)) {
      const path = change.new_path || change.old_path; const language = languageFor(path); let oldContent: string | null = null; let newContent: string | null = null; let unavailable: string | null = null;
      if (!language) unavailable = "仅支持 Python/C++ 静态审查。";
      else if (change.diff.length > 512 * 1024) unavailable = "文件差异过大，暂不导入。";
      else try {
        if (!change.deleted_file) { const response = await gitlabFetch(`/projects/${projectId}/repository/files/${encodeURIComponent(path)}/raw?ref=${encodeURIComponent(headSha)}`, signal); const text = await response.text(); if (text.length > 2 * 1024 * 1024) throw new Error("文件超过 2 MB 限制。"); newContent = text; }
        if (!change.new_file) { const response = await gitlabFetch(`/projects/${projectId}/repository/files/${encodeURIComponent(change.old_path)}/raw?ref=${encodeURIComponent(baseSha)}`, signal); const text = await response.text(); if (text.length > 2 * 1024 * 1024) throw new Error("文件超过 2 MB 限制。"); oldContent = text; }
      } catch (error) { unavailable = error instanceof Error ? error.message : "无法读取文件内容。"; }
      files.push({ old_path: change.old_path, new_path: change.new_path, change_type: change.new_file ? "added" : change.deleted_file ? "deleted" : change.renamed_file ? "renamed" : "modified", language, old_content: oldContent, new_content: newContent, diff: change.diff, changed_ranges: [], diff_truncated: change.diff.length > 512 * 1024, selectable: !unavailable && Boolean(newContent), unavailable_reason: unavailable });
    }
    return { gitlab_host: GITLAB_HOST, project_id: projectId, project_path: project.path_with_namespace ?? `project-${projectId}`, merge_request_iid: iid, title: mr.title, web_url: mr.web_url, base_sha: baseSha, head_sha: headSha, files };
}

function apiError(status: number): Error {
  if (status === 401) return new Error("GitLab 授权已过期，请重新连接。");
  if (status === 403) return new Error("GitLab 当前账户没有读取权限。");
  if (status === 429) return new Error("GitLab 请求过于频繁，请稍后重试。");
  return new Error("GitLab 暂时无法读取，请检查网络后重试。");
}

async function gitlabFetch(path: string, signal?: AbortSignal): Promise<Response> {
  if (!accessToken) throw new Error("请先连接 GitLab。");
  const response = await fetch(`${GITLAB_HOST}/api/v4${path}`, { headers: { Authorization: `Bearer ${accessToken}` }, signal });
  if (!response.ok) throw apiError(response.status);
  return response;
}

export function assertNotGitLabBranchTreeUrl(value: string): void {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return;
  }
  if (/\/\-\/tree\//.test(url.pathname)) {
    throw new Error("这是 GitLab 分支树地址，不是 Merge Request 地址。请从项目浏览器查看该分支，再选择能确定 base/head 的 Merge Request；当前不支持仅凭单个分支直接导入审查。");
  }
}

function parseMrUrl(value: string): { projectPath: string; iid: number } {
  const url = new URL(value);
  if (url.origin !== GITLAB_HOST) throw new Error("MR 地址必须来自已连接的 GitLab 服务。");
  assertNotGitLabBranchTreeUrl(value);
  const match = url.pathname.match(/^\/(.+)\/\-\/merge_requests\/(\d+)\/?$/);
  if (!match) throw new Error("请输入完整的 GitLab Merge Request 地址。");
  return { projectPath: match[1], iid: Number(match[2]) };
}

function languageFor(path: string): "python" | "cpp" | null {
  if (/\.(py|pyw)$/i.test(path)) return "python";
  if (/\.(c|cc|cpp|cxx|h|hh|hpp|hxx)$/i.test(path)) return "cpp";
  return null;
}

export async function previewGitLabMergeRequestBrowser(url: string, signal?: AbortSignal): Promise<GitLabMergeRequestPreview> {
  const { projectPath, iid } = parseMrUrl(url.trim());
  const encodedProject = encodeURIComponent(projectPath);
  const changesResponse = await gitlabFetch(`/projects/${encodedProject}/merge_requests/${iid}/changes`, signal);
  const mr = await changesResponse.json() as { id: number; title: string; web_url: string; diff_refs?: { base_sha?: string; head_sha?: string }; changes?: Array<{ old_path: string; new_path: string; new_file: boolean; deleted_file: boolean; renamed_file: boolean; diff: string }> };
  const baseSha = mr.diff_refs?.base_sha; const headSha = mr.diff_refs?.head_sha;
  if (!baseSha || !headSha) throw new Error("GitLab 未提供稳定的 base/head SHA，无法安全导入。");
  const changes = mr.changes ?? [];
  const files: GitLabFileChange[] = [];
  for (const change of changes.slice(0, 200)) {
    const path = change.new_path || change.old_path;
    const language = languageFor(path);
    let oldContent: string | null = null; let newContent: string | null = null; let unavailable: string | null = null;
    if (!language) unavailable = "仅支持 Python/C++ 静态审查。";
    else if (change.diff.length > 512 * 1024) unavailable = "文件差异过大，暂不导入。";
    else {
      try {
        if (!change.deleted_file) {
          const response = await gitlabFetch(`/projects/${encodedProject}/repository/files/${encodeURIComponent(path)}/raw?ref=${encodeURIComponent(headSha)}`, signal);
          const text = await response.text(); if (text.length > 2 * 1024 * 1024) throw new Error("文件超过 2 MB 限制。"); newContent = text;
        }
        if (!change.new_file) {
          const response = await gitlabFetch(`/projects/${encodedProject}/repository/files/${encodeURIComponent(change.old_path)}/raw?ref=${encodeURIComponent(baseSha)}`, signal);
          const text = await response.text(); if (text.length > 2 * 1024 * 1024) throw new Error("文件超过 2 MB 限制。"); oldContent = text;
        }
      } catch (error) { unavailable = error instanceof Error ? error.message : "无法读取文件内容。"; }
    }
    files.push({ old_path: change.old_path, new_path: change.new_path, change_type: change.new_file ? "added" : change.deleted_file ? "deleted" : change.renamed_file ? "renamed" : "modified", language, old_content: oldContent, new_content: newContent, diff: change.diff, changed_ranges: [], diff_truncated: change.diff.length > 512 * 1024, selectable: !unavailable && Boolean(newContent), unavailable_reason: unavailable });
  }
  return { gitlab_host: GITLAB_HOST, project_id: mr.id, project_path: projectPath, merge_request_iid: iid, title: mr.title, web_url: mr.web_url, base_sha: baseSha, head_sha: headSha, files };
}

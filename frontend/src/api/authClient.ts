export type AuthUser = { user_id: string; username: string; role: "admin" | "user"; is_active: boolean; must_change_password: boolean; password_changed_at: string; csrf_token?: string };
export type AuthSessionInfo = { session_id: string; current: boolean; created_at: string; last_seen_at: string; expires_at: string };
export type AuthSessionPage = { total: number; items: AuthSessionInfo[] };export type AdminUser = AuthUser;
export type UserPage = { items: AdminUser[]; total: number; page: number; page_size: number };
export type UserStats = { total: number; active: number; disabled: number };
class AuthApiError extends Error {
  status: number;
  detail: string;
  code?: string;

  constructor(status: number, detail: string, code?: string) {
    super(detail);
    this.name = "AuthApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}
async function json<T>(request: Promise<Response>): Promise<T> {
  const response = await request; const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const fallback: Record<number, string> = {
      401: "账号已在其他设备登录或会话已过期，请重新登录。",
      403: "当前账号没有权限，或页面安全令牌已过期。",
      409: "当前内容已发生变化，请刷新后重试。",
      413: "请求内容过大，请缩小后重试。",
      422: "输入内容无法处理，请根据提示修改。",
      429: "请求过于频繁，请稍后重试。",
      502: "模型服务暂时失败，请稍后重试。",
    };
    const rawDetail = body.detail;
    const detail = typeof rawDetail === "object" && rawDetail
      ? rawDetail.message
      : typeof rawDetail === "string"
        ? rawDetail
        : fallback[response.status] || "请求失败";
    const translated: Record<string, string> = {
      "authentication required": fallback[401],
      "invalid CSRF token": "页面安全令牌已过期，请刷新后重试。",
    };
    throw new AuthApiError(
      response.status,
      translated[detail] ?? detail,
      typeof rawDetail === "object" && rawDetail ? rawDetail.error_code : undefined,
    );
  }
  return body as T;
}
const headers = (csrf: string, content = false) => ({ ...(content ? { "Content-Type": "application/json" } : {}), "X-CSRF-Token": csrf });
export const getCurrentUser = () => json<AuthUser>(fetch("/v1/auth/me", { credentials: "same-origin" }));
export const login = (username: string, password: string) => json<AuthUser>(fetch("/v1/auth/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) }));
export const logout = (csrf: string) => json<{ ok: boolean }>(fetch("/v1/auth/logout", { method: "POST", credentials: "same-origin", headers: headers(csrf) }));
export const logoutAll = (csrf: string) => json<{ ok: boolean }>(fetch("/v1/auth/logout-all", { method: "POST", credentials: "same-origin", headers: headers(csrf) }));
export const listAuthSessions = () => json<AuthSessionPage>(fetch("/v1/auth/sessions", { credentials: "same-origin" }));
export const changePassword = (currentPassword: string, newPassword: string, csrf: string) => json<{ ok: boolean }>(fetch("/v1/auth/change-password", { method: "POST", credentials: "same-origin", headers: headers(csrf, true), body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) }));
export const adminApi = {
  listUsers: (params: { q: string; status: string; page: number; page_size: number }, csrf: string) => { const query = new URLSearchParams({ status: params.status, page: String(params.page), page_size: String(params.page_size) }); if (params.q) query.set("q", params.q); return json<UserPage>(fetch(`/v1/admin/users?${query}`, { credentials: "same-origin", headers: headers(csrf) })); },
  stats: (csrf: string) => json<UserStats>(fetch("/v1/admin/users/stats", { credentials: "same-origin", headers: headers(csrf) })),
  createUser: (input: { username: string }, csrf: string) => json<AdminUser & { temporary_password: string }>(fetch("/v1/admin/users", { method: "POST", credentials: "same-origin", headers: headers(csrf, true), body: JSON.stringify(input) })),
  disableUser: (id: string, csrf: string) => json<AdminUser>(fetch(`/v1/admin/users/${id}/disable`, { method: "POST", credentials: "same-origin", headers: headers(csrf) })),
  enableUser: (id: string, csrf: string) => json<AdminUser>(fetch(`/v1/admin/users/${id}/enable`, { method: "POST", credentials: "same-origin", headers: headers(csrf) })),
  resetPassword: (id: string, csrf: string) => json<{ temporary_password: string }>(fetch(`/v1/admin/users/${id}/reset-password`, { method: "POST", credentials: "same-origin", headers: headers(csrf) })),
  deleteUser: (id: string, csrf: string) => json<{ ok: boolean }>(fetch(`/v1/admin/users/${id}`, { method: "DELETE", credentials: "same-origin", headers: headers(csrf) })),
  batchStatus: (ids: string[], active: boolean, csrf: string) => json<{ results: Array<{ user_id: string; ok: boolean; error?: string }> }>(fetch("/v1/admin/users/batch-status", { method: "POST", credentials: "same-origin", headers: headers(csrf, true), body: JSON.stringify({ user_ids: ids, active }) })),
};
let originalFetch: typeof window.fetch | null = null;
let authenticatedCsrfToken = "";
let authExpiredNotified = false;

export const AUTH_EXPIRED_EVENT = "codeastra:auth-expired";

export function getAuthenticatedCsrfToken(): string {
  return authenticatedCsrfToken;
}

export function installAuthenticatedFetch(csrf: string): void {
  authenticatedCsrfToken = csrf;
  authExpiredNotified = false;
  if (!originalFetch) originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    let requestInit = init;
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      const requestHeaders = new Headers(init.headers);
      if (!requestHeaders.has("X-CSRF-Token")) requestHeaders.set("X-CSRF-Token", csrf);
      requestInit = { ...init, headers: requestHeaders, credentials: init.credentials || "same-origin" };
    }
    const response = await originalFetch!(input, requestInit);
    const rawUrl = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = new URL(rawUrl, window.location.href).pathname;
    const protectedApi = path.startsWith("/v1/") && path !== "/v1/auth/login";
    if (response.status === 401 && protectedApi && !authExpiredNotified) {
      authExpiredNotified = true;
      window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT));
    }
    return response;
  };
}

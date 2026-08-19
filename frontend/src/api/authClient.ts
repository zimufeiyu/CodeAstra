export type AuthUser = { user_id: string; username: string; role: "admin" | "user"; is_active: boolean; must_change_password: boolean; password_changed_at: string; csrf_token?: string };
export type AuthSessionInfo = { session_id: string; current: boolean; created_at: string; last_seen_at: string; expires_at: string };
export type AuthSessionPage = { total: number; items: AuthSessionInfo[] };export type AdminUser = AuthUser;
export type UserPage = { items: AdminUser[]; total: number; page: number; page_size: number };
export type UserStats = { total: number; active: number; disabled: number };
async function json<T>(request: Promise<Response>): Promise<T> {
  const response = await request; const body = await response.json().catch(() => ({}));
  if (!response.ok) throw { status: response.status, detail: body.detail || "请求失败" };
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

export function getAuthenticatedCsrfToken(): string {
  return authenticatedCsrfToken;
}

export function installAuthenticatedFetch(csrf: string): void {
  authenticatedCsrfToken = csrf;
  if (!originalFetch) originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    if (["GET", "HEAD", "OPTIONS"].includes(method)) return originalFetch!(input, init);
    const requestHeaders = new Headers(init.headers); if (!requestHeaders.has("X-CSRF-Token")) requestHeaders.set("X-CSRF-Token", csrf);
    return originalFetch!(input, { ...init, headers: requestHeaders, credentials: init.credentials || "same-origin" });
  };
}



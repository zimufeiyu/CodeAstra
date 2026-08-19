import { FormEvent, ReactNode, useEffect, useState } from "react";
import {
  AuthSessionPage,
  AuthUser,
  changePassword as changePasswordRequest,
  listAuthSessions,
  logout as logoutRequest,
  logoutAll as logoutAllRequest,
} from "../api/authClient";
import { SettingsShell } from "./SettingsShell";
import { isPersistentDeepSeekStorageKey } from "../utils/deepseekSettings";

type SecurityApi = {
  changePassword: typeof changePasswordRequest;
  logout: typeof logoutRequest;
  logoutAll: typeof logoutAllRequest;
  listSessions: typeof listAuthSessions;
};
const defaultApi: SecurityApi = {
  changePassword: changePasswordRequest,
  logout: logoutRequest,
  logoutAll: logoutAllRequest,
  listSessions: listAuthSessions,
};

export function clearUserScopedBrowserState(): void {
  for (const storage of [window.sessionStorage, window.localStorage]) {
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index));
    for (const key of keys) {
      if (key?.startsWith("code-review.") && !isPersistentDeepSeekStorageKey(key)) {
        storage.removeItem(key);
      }
    }
  }
}

export function PasswordChangeForm({ csrfToken, onComplete, api = defaultApi }: { csrfToken: string; onComplete: () => void; api?: Partial<SecurityApi> }) {
  const resolvedApi = { ...defaultApi, ...api };
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  function clearPasswords() { setCurrentPassword(""); setNewPassword(""); setConfirmation(""); }
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (newPassword !== confirmation) { setError("两次输入的新密码不一致"); return; }
    setBusy(true);
    try {
      await resolvedApi.changePassword(currentPassword, newPassword, csrfToken);
      clearPasswords(); clearUserScopedBrowserState(); onComplete();
    } catch (reason: any) {
      clearPasswords(); setError(reason?.detail || "密码修改失败，请重新输入");
    } finally { setBusy(false); }
  }
  return <form className="auth-password-form" onSubmit={submit}>
    <label>当前密码<input aria-label="当前密码" type="password" autoComplete="current-password" value={currentPassword} onChange={event => setCurrentPassword(event.target.value)} required /></label>
    <label>新密码<input aria-label="新密码" type="password" autoComplete="new-password" minLength={8} value={newPassword} onChange={event => setNewPassword(event.target.value)} required /></label>
    <label>确认新密码<input aria-label="确认新密码" type="password" autoComplete="new-password" minLength={8} value={confirmation} onChange={event => setConfirmation(event.target.value)} required /></label>
    {error && <p className="auth-error" role="alert">{error}</p>}
    <button type="submit" disabled={busy}>保存并重新登录</button>
  </form>;
}

export function AccountSecurityPanel({ user, onSignedOut, onClose, forced = false, api, gitLabConnections, initialSection = "profile" }: { user: AuthUser; onSignedOut: () => void; onClose?: () => void; forced?: boolean; api?: Partial<SecurityApi>; gitLabConnections?: ReactNode; initialSection?: "profile" | "security" | "gitlab" | "devices" }) {
  const resolvedApi = { ...defaultApi, ...api };
  const [section, setSection] = useState(initialSection);
  const [deviceError, setDeviceError] = useState("");
  const [deviceBusy, setDeviceBusy] = useState(false);
  const [sessions, setSessions] = useState<AuthSessionPage | null>(null);
  const csrf = user.csrf_token || "";

  useEffect(() => {
    if (forced || section !== "devices") return;
    let ignore = false;
    setDeviceBusy(true); setDeviceError("");
    void resolvedApi.listSessions()
      .then((page) => { if (!ignore) setSessions(page); })
      .catch((reason: any) => { if (!ignore) setDeviceError(reason?.detail || "登录设备加载失败，请重试"); })
      .finally(() => { if (!ignore) setDeviceBusy(false); });
    return () => { ignore = true; };
  }, [forced, section]);

  async function signOut(allDevices: boolean) {
    setDeviceError(""); setDeviceBusy(true);
    try {
      await (allDevices ? resolvedApi.logoutAll(csrf) : resolvedApi.logout(csrf));
      clearUserScopedBrowserState(); onSignedOut();
    } catch (reason: any) { setDeviceError(reason?.detail || "退出失败，请重试"); }
    finally { setDeviceBusy(false); }
  }

  const summary = <dl className="auth-account-summary"><div><dt>用户名</dt><dd>{user.username}</dd></div><div><dt>角色</dt><dd>{user.role === "admin" ? "管理员" : "普通用户"}</dd></div><div><dt>状态</dt><dd>{user.is_active ? "正常" : "已停用"}</dd></div><div><dt>密码更新时间</dt><dd>{user.password_changed_at ? new Date(user.password_changed_at).toLocaleString() : "暂无记录"}</dd></div></dl>;
  const security = <><h2>修改密码</h2><PasswordChangeForm csrfToken={csrf} onComplete={onSignedOut} api={resolvedApi} /></>;
  const gitlab = gitLabConnections ? <section className="auth-gitlab-connections" aria-labelledby="auth-gitlab-title"><div><span className="auth-kicker">GitLab connections</span><h2 id="auth-gitlab-title">GitLab 连接</h2><p>管理当前浏览器中用于导入合并请求的 GitLab 账户。</p></div>{gitLabConnections}</section> : <p>尚未连接 GitLab 账户。</p>;
  const devices = <section className="auth-session-section" aria-labelledby="auth-session-title">
    <div><span className="auth-kicker">Active session</span><h2 id="auth-session-title">登录设备</h2><p>{deviceBusy && !sessions ? "正在读取登录状态…" : `当前账号共有 ${sessions?.total ?? 0} 个活动会话。新设备登录后，旧设备会自动退出。`}</p></div>
    {deviceError && <p className="auth-error" role="alert">{deviceError}</p>}
    <div className="auth-session-list">
      {sessions?.items.map((item) => <article key={item.session_id} className="auth-session-item"><div><strong>{item.current ? "当前设备" : "其他设备"}</strong><span>{item.current ? "正在使用" : "活动会话"}</span></div><dl><div><dt>登录时间</dt><dd>{new Date(item.created_at).toLocaleString()}</dd></div><div><dt>最近活动</dt><dd>{new Date(item.last_seen_at).toLocaleString()}</dd></div><div><dt>自动过期</dt><dd>{new Date(item.expires_at).toLocaleString()}</dd></div></dl></article>)}
    </div>
    <div className="auth-device-actions"><button type="button" className="auth-secondary" disabled={deviceBusy} onClick={() => void signOut(false)}>退出当前设备</button><button type="button" className="auth-danger" disabled={deviceBusy} onClick={() => void signOut(true)}>退出所有设备</button></div>
  </section>;
  const content = forced ? <><p>临时密码只能使用一次。修改后需要重新登录。</p>{summary}{security}</> : section === "profile" ? summary : section === "security" ? security : section === "gitlab" ? gitlab : devices;
  const card = <section className="auth-card auth-security" aria-label="账户安全"><div className="auth-security-heading"><div><span className="auth-kicker">CodeAstra · 账户安全</span><h1>{forced ? "首次登录，请修改密码" : "账户安全"}<span className="sr-only">{forced ? " Change password" : " Account security"}</span></h1></div>{!forced && onClose && <button type="button" className="auth-link" onClick={onClose}>返回审查</button>}</div>{content}</section>;
  if (forced) return card;
  return <SettingsShell ariaLabel="账户设置导航" activeId={section} onSelect={(id) => setSection(id as typeof section)} items={[{ id: "profile", label: "个人资料" }, { id: "security", label: "密码与安全" }, { id: "gitlab", label: "GitLab 连接" }, { id: "devices", label: "登录设备" }]}>{card}</SettingsShell>;
}
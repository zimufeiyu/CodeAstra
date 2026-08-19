import { FormEvent, ReactNode, useEffect, useState } from "react";

import { getCurrentUser, installAuthenticatedFetch, login, logout, AuthUser } from "../api/authClient";
import { AccountSecurityPanel, clearUserScopedBrowserState } from "./AccountSecurityPanel";
import { AdminUsersPage } from "./AdminUsersPage";
import { AuthSessionProvider } from "./AuthSessionContext";
import { ProductBrand } from "../components/ProductBrand";

export function AuthGate({ app }: { app: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [state, setState] = useState<"loading" | "login" | "ready" | "error">("loading");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [adminReview, setAdminReview] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.resolve()
      .then(() => getCurrentUser())
      .then(value => {
        if (!active) return;
        if (!value.csrf_token) {
          setUser(null);
          setState("error");
          return;
        }
        setUser(value);
        if (typeof installAuthenticatedFetch === "function") installAuthenticatedFetch(value.csrf_token);
        setState("ready");
      })
      .catch((reason: any) => {
        if (active) setState(reason?.status === 401 ? "login" : "error");
      });
    return () => { active = false; };
  }, [app]);

  function returnToLogin(message = "") {
    clearUserScopedBrowserState();
    setUser(null); setPassword(""); setAccountOpen(false); setState("login"); setError(message);
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    try {
      const value = await login(username, password);
      if (!value.csrf_token) {
        setUser(null); setPassword(""); setState("error");
        return;
      }
      setUser(value); setPassword("");
      if (typeof installAuthenticatedFetch === "function") installAuthenticatedFetch(value.csrf_token);
      setState("ready");
    } catch { setPassword(""); setError("用户名或密码错误"); }
  }
  async function signOut() { await logout(user?.csrf_token || "").catch(() => undefined); returnToLogin(); }

  if (state === "loading") return <div className="auth-center"><p>正在确认登录状态…</p></div>;
  if (state === "error") return <div className="auth-center"><div className="auth-card" role="alert"><h1>暂时无法进入</h1><p>认证服务没有正常响应，请稍后刷新页面。</p></div></div>;
  if (state === "login") return <div className="auth-center"><form className="auth-card" onSubmit={submit}><ProductBrand subtitle="星鉴" /><h1>登录 <span className="sr-only">Sign in</span></h1><p>使用管理员分配的账号继续。</p><label>用户名<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} autoFocus required /></label><label>密码<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /></label>{error && <p className="auth-error" role="alert">{error}</p>}<button type="submit">登录</button></form></div>;
  if (user?.must_change_password) return <div className="auth-center"><AccountSecurityPanel user={user} forced onSignedOut={() => returnToLogin("密码已修改，请重新登录")} /></div>;
  if (user?.role === "admin" && !adminReview) return <div className="auth-session"><button className="auth-logout" onClick={() => void signOut()}>退出登录</button><AdminUsersPage csrfToken={user.csrf_token || ""} onReview={() => setAdminReview(true)} /></div>;
  if (!user) return null;
  return <AuthSessionProvider value={{ user, accountSettingsOpen: accountOpen, openAccountSettings: () => setAccountOpen(true), closeAccountSettings: () => setAccountOpen(false), openAdminManagement: () => { setAccountOpen(false); setAdminReview(false); }, signOut: () => void signOut() }}><div className="auth-session">{app}</div></AuthSessionProvider>;
}



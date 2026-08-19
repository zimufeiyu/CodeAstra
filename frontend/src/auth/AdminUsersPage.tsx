import { FormEvent, useEffect, useRef, useState } from "react";
import { adminApi as defaultApi, AdminUser, UserPage, UserStats } from "../api/authClient";
import { SettingsShell } from "./SettingsShell";

type ListParams = { q: string; status: "all" | "active" | "disabled"; page: number; page_size: number };
type Api = {
  listUsers: (params: ListParams, csrf: string) => Promise<UserPage>;
  stats: (csrf: string) => Promise<UserStats>;
  createUser: (input: { username: string }, csrf: string) => Promise<AdminUser & { temporary_password: string }>;
  disableUser: (id: string, csrf: string) => Promise<AdminUser>;
  enableUser: (id: string, csrf: string) => Promise<AdminUser>;
  resetPassword: (id: string, csrf: string) => Promise<{ temporary_password: string }>;
  deleteUser: (id: string, csrf: string) => Promise<{ ok: boolean }>;
  batchStatus: (ids: string[], active: boolean, csrf: string) => Promise<{ results: Array<{ user_id: string; ok: boolean; error?: string }> }>;
};

export function AdminUsersPage({ csrfToken, api = defaultApi, onReview }: { csrfToken: string; api?: Api; onReview?: () => void }) {
  const [activeSection, setActiveSection] = useState("users");
  const [pageData, setPageData] = useState<UserPage>({ items: [], total: 0, page: 1, page_size: 20 });
  const [stats, setStats] = useState<UserStats>({ total: 0, active: 0, disabled: 0 });
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<ListParams["status"]>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [username, setUsername] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState<AdminUser | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [deletingBusy, setDeletingBusy] = useState(false);
  const refreshSequence = useRef(0);
  const totalPages = Math.max(1, Math.ceil(pageData.total / pageSize));

  async function refresh() {
    const sequence = ++refreshSequence.current;
    const [users, counts] = await Promise.all([
      api.listUsers({ q: query, status, page, page_size: pageSize }, csrfToken),
      api.stats(csrfToken),
    ]);
    if (sequence !== refreshSequence.current) return;
    setPageData(users); setStats(counts);
    setSelected(current => current.filter(id => users.items.some(user => user.user_id === id)));
  }
  useEffect(() => { void refresh().catch(reason => setError(reason?.detail || "无法载入用户列表")); }, [csrfToken, query, status, page, pageSize]);

  async function create(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice("");
    try {
      const created = await api.createUser({ username }, csrfToken);
      setNotice(`账号 ${created.username} 已创建，临时密码：${created.temporary_password}。首次登录必须修改密码。`);
      setUsername(""); setPage(1); await refresh();
    } catch (reason: any) { setError(reason?.detail || "创建用户失败"); }
  }
  async function act(user: AdminUser, action: "enable" | "disable" | "reset") {
    setError(""); setNotice("");
    try {
      if (action === "reset") {
        const result = await api.resetPassword(user.user_id, csrfToken);
        setNotice(`${user.username} 的密码已重置为 ${result.temporary_password}，下次登录必须修改。`);
      } else {
        await (action === "enable" ? api.enableUser : api.disableUser)(user.user_id, csrfToken);
        await refresh();
      }
    } catch (reason: any) { setError(reason?.detail || "操作失败"); }
  }
  async function batch(active: boolean) {
    if (!selected.length) return;
    setError(""); setNotice("");
    try {
      const result = await api.batchStatus(selected, active, csrfToken);
      const failed = result.results.filter(item => !item.ok).length;
      setNotice(`\u6279\u91cf\u64cd\u4f5c\u5b8c\u6210\uff1a\u6210\u529f ${result.results.length - failed}\uff0c\u5931\u8d25 ${failed}\u3002`);
      setSelected([]); await refresh();
    } catch (reason: any) { setError(reason?.detail || "\u6279\u91cf\u64cd\u4f5c\u5931\u8d25"); }
  }
  async function confirmDelete() {
    if (!deleting || confirmation !== deleting.username || deletingBusy) return;
    setDialogError(""); setDeletingBusy(true);
    try {
      await api.deleteUser(deleting.user_id, csrfToken);
      setNotice(`\u8d26\u53f7 ${deleting.username} \u53ca\u5176\u5168\u90e8\u5ba1\u67e5\u6570\u636e\u5df2\u6c38\u4e45\u5220\u9664\u3002`);
      setDeleting(null); setConfirmation(""); await refresh();
    } catch (reason: any) { setDialogError(reason?.detail || "\u5220\u9664\u7528\u6237\u5931\u8d25"); }
    finally { setDeletingBusy(false); }
  }

  return <SettingsShell ariaLabel="账号管理导航" activeId={activeSection} onSelect={setActiveSection} items={[{ id: "overview", label: "概览" }, { id: "users", label: "用户管理" }, { id: "create", label: "创建用户" }]}><main className="auth-admin">
    <header className="auth-admin-header"><div><span className="auth-kicker">CodeAstra · 星鉴</span><h1>账号管理 <span className="sr-only">Account management</span></h1><p>创建、查找和维护可使用代码审查工具的账号。</p></div>{onReview && <button className="auth-secondary" onClick={onReview}>进入代码审查</button>}</header>
    {notice && <p className="auth-notice" role="status">{notice}</p>}{error && <p className="auth-error" role="alert">{error}</p>}
    {activeSection === "overview" && <section className="auth-stats" aria-label="账号统计"><article><strong>{stats.total}</strong><span>全部账号</span></article><article><strong>{stats.active}</strong><span>正常账号</span></article><article><strong>{stats.disabled}</strong><span>已停用</span></article></section>}
    {activeSection === "create" && <section className="auth-panel"><h2>创建普通账号</h2><p>新账号默认密码固定为 <code>12345678</code>，首次登录后会强制修改。</p><form className="auth-create" onSubmit={create}><label>用户名<input value={username} onChange={e => setUsername(e.target.value)} required /></label><button type="submit">创建用户 <span className="sr-only">Create user</span></button></form></section>}
    {activeSection === "users" && <section className="auth-panel auth-user-management"><div className="auth-panel-title"><h2>现有账号</h2><span>共 {pageData.total} 个</span></div>
      <div className="auth-user-tools"><label>搜索用户<input aria-label="搜索用户" value={query} onChange={e => { setQuery(e.target.value); setPage(1); }} placeholder="输入用户名" /></label><label>账号状态<select aria-label="账号状态" value={status} onChange={e => { setStatus(e.target.value as ListParams["status"]); setPage(1); }}><option value="all">全部</option><option value="active">正常</option><option value="disabled">已停用</option></select></label><label>每页<select aria-label="每页数量" value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}><option value="20">20</option><option value="50">50</option></select></label></div>
      <div className="auth-batch"><button disabled={!selected.length} onClick={() => void batch(true)}>批量启用</button><button disabled={!selected.length} onClick={() => void batch(false)}>批量停用</button><span>已选择 {selected.length} 个</span></div>
      <div className="auth-user-table-scroll" data-testid="user-table-scroll"><div className="auth-user-table" role="table"><div className="auth-user-row auth-user-head" role="row"><span>选择</span><span>用户</span><span>状态</span><span>操作</span></div>{pageData.items.map(user => <div className="auth-user-row" role="row" key={user.user_id}><span>{user.role === "user" ? <input type="checkbox" aria-label={`选择 ${user.username}`} checked={selected.includes(user.user_id)} onChange={e => setSelected(current => e.target.checked ? [...current, user.user_id] : current.filter(id => id !== user.user_id))} /> : "—"}</span><span><strong>{user.username}</strong><small>{user.role === "admin" ? "唯一管理员" : user.must_change_password ? "待修改密码" : "普通用户"}</small></span><span><i className={user.is_active ? "status-active" : "status-disabled"}>{user.is_active ? "正常" : "已停用"}</i></span><span className="auth-row-actions">{user.role === "user" ? <><button onClick={() => void act(user, user.is_active ? "disable" : "enable")}>{user.is_active ? "停用" : "启用"}</button><button onClick={() => void act(user, "reset")}>重置密码</button><button className="auth-danger" aria-label={`删除 ${user.username}`} onClick={() => { setDeleting(user); setConfirmation(""); setDialogError(""); }}>删除</button></> : <span>受保护</span>}</span></div>)}</div></div>
      <nav className="auth-pagination" aria-label="用户分页"><button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><span>第 {pageData.page} / {totalPages} 页</span><button disabled={page >= totalPages} onClick={() => setPage(value => value + 1)}>下一页</button></nav>
    </section>}
    {deleting && <div className="auth-dialog-backdrop"><section className="auth-delete-dialog" role="dialog" aria-modal="true" aria-label="永久删除用户"><h2>永久删除 {deleting.username}</h2><p>此操作会删除该账号、登录会话、上传文件、审查记录、事件、报告和跟进内容，无法恢复。</p><label>输入用户名确认<input aria-label="输入用户名确认" value={confirmation} onChange={e => setConfirmation(e.target.value)} autoFocus /></label>{dialogError && <p className="auth-error" role="alert">{dialogError}</p>}<div><button className="auth-secondary" disabled={deletingBusy} onClick={() => setDeleting(null)}>取消</button><button className="auth-danger" disabled={confirmation !== deleting.username || deletingBusy} onClick={() => void confirmDelete()}>确认永久删除</button></div></section></div>}
  </main></SettingsShell>;
}

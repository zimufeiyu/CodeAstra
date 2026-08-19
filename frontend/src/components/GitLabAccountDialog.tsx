import { Check, GitBranch, LoaderCircle, Trash2, X } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

import { verifyGitLabAccount } from "../api/client";
import type { SavedGitLabAccount } from "../utils/gitlabAccounts";
import { buildSavedGitLabAccount } from "../utils/gitlabAccounts";
import "./GitLabTokenHelp.css";

type Props = {
  open: boolean;
  accounts: SavedGitLabAccount[];
  activeAccountId: string | null;
  onClose: () => void;
  onSave: (account: SavedGitLabAccount) => void;
  onActivate: (accountId: string) => void;
  onDelete: (accountId: string) => void;
};
type ManagementProps = Omit<Props, "open" | "onClose">;
export function GitLabAccountManagement({ accounts, activeAccountId, onSave, onActivate, onDelete }: ManagementProps) {
  const [host, setHost] = useState("https://gitlab.com"); const [token, setToken] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function connect(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { const profile = await verifyGitLabAccount(host.trim(), token); const existing = accounts.find((item) => item.gitlab_host === profile.gitlab_host && item.user_id === profile.user_id); onSave(buildSavedGitLabAccount(profile, token, existing)); setToken(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法验证 GitLab 账户。"); } finally { setBusy(false); }
  }
  return <div className="gitlab-account-body">{accounts.length ? <section className="saved-gitlab-accounts" aria-label="已保存的 GitLab 账户"><p className="gitlab-account-section-title">已保存连接</p>{accounts.map((account) => <article className={account.account_id === activeAccountId ? "saved-gitlab-account active" : "saved-gitlab-account"} key={account.account_id}><button type="button" className="saved-gitlab-account-main" onClick={() => onActivate(account.account_id)}><span className="gitlab-account-avatar">{account.avatar_url ? <img src={account.avatar_url} alt="" /> : <GitBranch size={18} />}</span><span><strong>{account.name}</strong><small>@{account.username} · {account.gitlab_host}</small></span>{account.account_id === activeAccountId ? <Check size={16} aria-label="当前使用" /> : null}</button><button type="button" className="gitlab-account-delete" aria-label={`删除 GitLab 账户 ${account.username}`} onClick={() => { if (window.confirm(`删除已保存的 GitLab 账户“${account.username}”？`)) onDelete(account.account_id); }}><Trash2 size={15} /></button></article>)}</section> : null}<form className="gitlab-account-form" onSubmit={connect}><p className="gitlab-account-section-title">{accounts.length ? "添加其他连接" : "连接 GitLab"}</p><label>GitLab 地址<input type="url" required value={host} onChange={(event) => setHost(event.target.value)} placeholder="https://gitlab.example.com" /></label><label>个人访问令牌<input type="password" required value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" placeholder="用于验证并读取项目" /></label><section className="gitlab-token-help" aria-labelledby="gitlab-token-help-title"><h3 id="gitlab-token-help-title">如何获取个人访问令牌</h3><ol><li>登录 GitLab，点击右上角头像，然后选择“编辑个人资料”。</li><li>在左侧选择“访问”→“个人访问令牌”。</li><li>选择“生成令牌”→“旧版令牌（Legacy token）”。</li><li>填写名称和有效期，只勾选 <code>read_api</code> 与 <code>read_repository</code>。</li><li>点击“生成令牌”，立即复制令牌并粘贴到上方；离开或刷新 GitLab 页面后将无法再次查看。</li></ol><p className="gitlab-token-warning">只需读取权限；不要勾选 api、write_repository 或管理员权限。</p><a href="https://docs.gitlab.com/user/profile/personal_access_tokens/" target="_blank" rel="noreferrer">查看 GitLab 官方说明</a></section><p className="gitlab-note">验证成功后保存在当前浏览器，可在导入 MR 时直接选择。不会写入审查历史。</p>{error ? <p className="gitlab-error" role="alert">{error}</p> : null}<button type="submit" className="gitlab-primary" disabled={busy || !host.trim() || !token}>{busy ? <LoaderCircle className="spin" size={17} /> : <GitBranch size={17} />}{busy ? "正在验证…" : "验证并保存"}</button></form></div>;
}

export function GitLabAccountDialog({
  open,
  accounts,
  activeAccountId,
  onClose,
  onSave,
  onActivate,
  onDelete,
}: Props) {
  useEffect(() => {
  }, [open]);

  if (!open) return null;

  return (
    <div className="gitlab-account-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="gitlab-account-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gitlab-account-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="gitlab-account-header">
          <div><p className="eyebrow">GitLab connections</p><h2 id="gitlab-account-title">GitLab 账户</h2></div>
          <button type="button" className="dialog-close" aria-label="关闭 GitLab 账户" onClick={onClose}><X size={18} /></button>
        </header>
        <GitLabAccountManagement accounts={accounts} activeAccountId={activeAccountId} onSave={onSave} onActivate={onActivate} onDelete={onDelete} />
      </section>
    </div>
  );
}

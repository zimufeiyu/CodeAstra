import { GitMerge, LoaderCircle, X } from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  GitLabFileChange,
  GitLabMergeRequestPreview,
  previewGitLabMergeRequest,
} from "../api/client";
import type { SavedGitLabAccount } from "../utils/gitlabAccounts";

type Props = {
  open: boolean;
  accounts?: SavedGitLabAccount[];
  activeAccountId?: string | null;
  onClose: () => void;
  onConnectAccount?: () => void;
  onImport: (preview: GitLabMergeRequestPreview, files: GitLabFileChange[]) => void;
};

const EMPTY_ACCOUNTS: SavedGitLabAccount[] = [];

const changeLabels: Record<GitLabFileChange["change_type"], string> = {
  added: "新增",
  modified: "修改",
  deleted: "删除",
  renamed: "重命名",
};

export function GitLabImportDialog({
  open,
  accounts = EMPTY_ACCOUNTS,
  activeAccountId = null,
  onClose,
  onConnectAccount,
  onImport,
}: Props) {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [accountId, setAccountId] = useState(activeAccountId ?? "");
  const [preview, setPreview] = useState<GitLabMergeRequestPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [activePath, setActivePath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) {
      requestControllerRef.current?.abort();
      requestControllerRef.current = null;
      setToken("");
      setBusy(false);
      return;
    }
    setPreview(null);
    setSelected(new Set());
    setActivePath("");
    setAccountId(
      activeAccountId && accounts.some((item) => item.account_id === activeAccountId)
        ? activeAccountId
        : "",
    );
    setError("");
    return () => {
      requestControllerRef.current?.abort();
      requestControllerRef.current = null;
    };
  }, [open, activeAccountId, accounts]);

  const selectedAccount = accounts.find((item) => item.account_id === accountId) ?? null;

  const activeFile = useMemo(
    () => preview?.files.find((file) => file.new_path === activePath || file.old_path === activePath) ?? preview?.files[0],
    [activePath, preview],
  );

  if (!open) return null;

  async function loadPreview(event: FormEvent) {
    event.preventDefault();
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    setBusy(true);
    setError("");
    if (selectedAccount) {
      try {
        if (new URL(url.trim()).origin !== selectedAccount.gitlab_host) {
          setError("MR 地址与所选 GitLab 账户不属于同一个服务。");
          setBusy(false);
          return;
        }
      } catch {
        setError("请输入完整的 GitLab Merge Request 地址。");
        setBusy(false);
        return;
      }
    }
    try {
      const request = previewGitLabMergeRequest(
        url.trim(),
        selectedAccount?.private_token ?? token,
        controller.signal,
      );
      if (!selectedAccount) setToken("");
      const result = await request;
      const available = result.files.filter((file) => file.selectable);
      setPreview(result);
      setSelected(new Set(available.map((file) => file.new_path)));
      setActivePath((available[0] ?? result.files[0])?.new_path ?? "");
      if (!selectedAccount) setToken("");
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") return;
      setError(requestError instanceof Error ? requestError.message : "无法读取 GitLab 合并请求。");
    } finally {
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
        setBusy(false);
      }
    }
  }

  function toggle(path: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  const chosenFiles = preview?.files.filter((file) => file.selectable && selected.has(file.new_path)) ?? [];

  return (
    <div className="gitlab-dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="gitlab-dialog" role="dialog" aria-modal="true" aria-labelledby="gitlab-dialog-title">
        <header className="gitlab-dialog-header">
          <div><p className="eyebrow">GitLab Merge Request</p><h2 id="gitlab-dialog-title">从 GitLab 导入代码</h2></div>
          <button type="button" className="dialog-close" aria-label="关闭" onClick={onClose}><X size={18} /></button>
        </header>

        {!preview ? (
          <form className="gitlab-connect-form" onSubmit={loadPreview}>
            {accounts.length ? (
              <label>使用 GitLab 账户
                <select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
                  <option value="">不使用已保存账户</option>
                  {accounts.map((account) => (
                    <option value={account.account_id} key={account.account_id}>
                      {account.name} (@{account.username}) · {account.gitlab_host}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <label>合并请求地址<input type="url" required value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://gitlab.example.com/group/project/-/merge_requests/12" /></label>
            {selectedAccount ? (
              <p className="gitlab-selected-account">使用 @{selectedAccount.username} 读取 {selectedAccount.gitlab_host}</p>
            ) : (
              <>
                {onConnectAccount ? (
                  <>
                    <button type="button" className="gitlab-secondary" onClick={onConnectAccount}>
                      连接 GitLab 并继续
                    </button>
                    <p className="gitlab-note">也可以使用下方一次性令牌，不会保存账户。</p>
                  </>
                ) : null}
                <label>访问令牌（私有项目需要）<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" placeholder="仅用于本次读取，不会保存" /></label>
              </>
            )}
            <p className="gitlab-note">使用固定的 base/head 提交读取新旧版本，避免审查期间代码发生漂移。</p>
            {error ? <p className="gitlab-error" role="alert">{error}</p> : null}
            <button className="gitlab-primary" type="submit" disabled={busy || !url.trim()}>
              {busy ? <LoaderCircle className="spin" size={17} /> : <GitMerge size={17} />}
              {busy ? "正在读取…" : "读取合并请求"}
            </button>
          </form>
        ) : (
          <>
            <div className="gitlab-mr-summary">
              <div><strong>{preview.title}</strong><span>{preview.project_path} · !{preview.merge_request_iid}</span></div>
              <button type="button" className="gitlab-link-button" onClick={() => setPreview(null)}>更换地址</button>
            </div>
            <div className="gitlab-browser">
              <aside className="gitlab-files" aria-label="变更文件">
                {preview.files.map((file) => (
                  <div className={"gitlab-file-row" + (activeFile === file ? " active" : "")} key={file.old_path + file.new_path}>
                    <button type="button" className="gitlab-file-open" onClick={() => setActivePath(file.new_path || file.old_path)}>
                      <strong>{file.new_path || file.old_path}</strong>
                      <span>{changeLabels[file.change_type]}{file.language ? " · " + (file.language === "python" ? "Python" : "C/C++") : ""}</span>
                    </button>
                    <input type="checkbox" aria-label={"选择 " + file.new_path} checked={selected.has(file.new_path)} disabled={!file.selectable} onChange={() => toggle(file.new_path)} />
                    {!file.selectable && file.unavailable_reason ? <small>{file.unavailable_reason}</small> : null}
                  </div>
                ))}
              </aside>
              <section className="gitlab-diff-preview">
                {activeFile ? (
                  <>
                    <div className="gitlab-diff-heading"><strong>{activeFile.new_path || activeFile.old_path}</strong>{activeFile.diff_truncated ? <span>差异过大，预览已截断</span> : null}</div>
                    <div className="gitlab-code-columns">
                      <div><span>修改前 · {preview.base_sha.slice(0, 8)}</span><pre>{activeFile.old_content ?? "（无旧版本）"}</pre></div>
                      <div><span>修改后 · {preview.head_sha.slice(0, 8)}</span><pre>{activeFile.new_content ?? "（无新版本）"}</pre></div>
                    </div>
                  </>
                ) : <p className="gitlab-empty">此合并请求没有可预览的文件。</p>}
              </section>
            </div>
            <footer className="gitlab-dialog-footer">
              <span>已选择 {chosenFiles.length} 个可审查文件</span>
              <div><button type="button" className="gitlab-secondary" onClick={onClose}>取消</button><button type="button" className="gitlab-primary" disabled={!chosenFiles.length} onClick={() => onImport(preview, chosenFiles)}>添加到审查</button></div>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}

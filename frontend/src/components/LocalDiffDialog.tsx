import { FileDiff, LoaderCircle, X } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";

import {
  LocalDiffPreview,
  previewLocalDiff,
} from "../api/client";

type Props = {
  open: boolean;
  onClose: () => void;
  onImport: (preview: LocalDiffPreview, oldContent: string) => void;
};

const ACCEPTED = ".py,.pyw,.c,.cc,.cpp,.cxx,.h,.hh,.hpp,.hxx";

function readFileText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("无法读取本地文件。"));
    reader.readAsText(file);
  });
}

export function LocalDiffDialog({ open, onClose, onImport }: Props) {
  const [oldFile, setOldFile] = useState<File | null>(null);
  const [newFile, setNewFile] = useState<File | null>(null);
  const [oldContent, setOldContent] = useState("");
  const [newContent, setNewContent] = useState("");
  const [preview, setPreview] = useState<LocalDiffPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (open) return;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setOldFile(null);
    setNewFile(null);
    setOldContent("");
    setNewContent("");
    setPreview(null);
    setBusy(false);
    setError("");
  }, [open]);

  if (!open) return null;

  async function chooseFile(file: File | undefined, side: "old" | "new") {
    if (!file) return;
    const content = await readFileText(file);
    if (side === "old") {
      setOldFile(file);
      setOldContent(content);
    } else {
      setNewFile(file);
      setNewContent(content);
    }
    setPreview(null);
    setError("");
  }

  async function compare(event: FormEvent) {
    event.preventDefault();
    if (!oldFile || !newFile) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError("");
    try {
      setPreview(await previewLocalDiff(
        { filename: oldFile.name, content: oldContent },
        { filename: newFile.name, content: newContent },
        controller.signal,
      ));
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") return;
      setError(requestError instanceof Error ? requestError.message : "无法生成本地版本对比。");
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setBusy(false);
      }
    }
  }

  const change = preview?.files[0];

  return (
    <div className="gitlab-dialog-backdrop" role="presentation">
      <section className="gitlab-dialog local-diff-dialog" role="dialog" aria-modal="true" aria-labelledby="local-diff-title">
        <header className="gitlab-dialog-header">
          <div><p className="eyebrow">Local diff</p><h2 id="local-diff-title">本地版本对比</h2></div>
          <button type="button" className="gitlab-close" aria-label="关闭" onClick={onClose}><X size={18} /></button>
        </header>
        {!preview ? (
          <form className="gitlab-connect-form local-diff-form" onSubmit={compare}>
            <div className="local-diff-pickers">
              <label className="local-diff-picker">
                <span>修改前文件</span>
                <input type="file" aria-label="修改前文件" accept={ACCEPTED} onChange={(event) => void chooseFile(event.target.files?.[0], "old")} />
                <strong>{oldFile?.name ?? "选择旧版本"}</strong>
              </label>
              <label className="local-diff-picker">
                <span>修改后文件</span>
                <input type="file" aria-label="修改后文件" accept={ACCEPTED} onChange={(event) => void chooseFile(event.target.files?.[0], "new")} />
                <strong>{newFile?.name ?? "选择新版本"}</strong>
              </label>
            </div>
            <p className="gitlab-note">只把修改后的文件加入审查；后端会重新计算并校验变更范围。</p>
            {error ? <p className="gitlab-error" role="alert">{error}</p> : null}
            <button className="gitlab-primary" type="submit" disabled={busy || !oldFile || !newFile}>
              {busy ? <LoaderCircle className="spin" size={17} /> : <FileDiff size={17} />}
              {busy ? "正在比较…" : "生成对比"}
            </button>
          </form>
        ) : (
          <>
            <div className="gitlab-mr-summary">
              <div><strong>{preview.old_label} → {preview.new_label}</strong><span>{change?.changed_ranges.length ?? 0} 个变更范围</span></div>
              <button type="button" className="gitlab-link-button" onClick={() => setPreview(null)}>重新选择</button>
            </div>
            <section className="gitlab-diff-preview local-diff-preview">
              {change ? (
                <>
                  <div className="gitlab-diff-heading">
                    <strong>{change.new_path}</strong>
                    {change.diff_truncated ? <span>差异预览已截断，审查范围仍按完整内容计算</span> : null}
                  </div>
                  <div className="gitlab-code-columns">
                    <div><span>修改前 · {preview.old_label}</span><pre>{change.old_content}</pre></div>
                    <div><span>修改后 · {preview.new_label}</span><pre>{change.new_content}</pre></div>
                  </div>
                  {!change.selectable ? <p className="gitlab-error" role="alert">{change.unavailable_reason}</p> : null}
                </>
              ) : null}
            </section>
            <footer className="gitlab-dialog-footer">
              <span>{change?.selectable ? "已准备 1 个本地变更文件" : "当前版本不可加入审查"}</span>
              <div>
                <button type="button" className="gitlab-secondary" onClick={onClose}>取消</button>
                <button type="button" className="gitlab-primary" disabled={!change?.selectable} onClick={() => onImport(preview, oldContent)}>添加到审查</button>
              </div>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}

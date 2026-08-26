import { Check, X } from "lucide-react";

import type { FixCandidate } from "../api/client";
import { UnifiedDiff } from "./UnifiedDiff";

type Props = {
  candidate: FixCandidate | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
};

export function FixPreviewDialog({ candidate, busy, onCancel, onConfirm }: Props) {
  if (!candidate) return null;
  return (
    <div className="fix-preview-backdrop" role="presentation">
      <section className="fix-preview-dialog" role="dialog" aria-modal="true" aria-labelledby="fix-preview-title">
        <header>
          <div><p className="eyebrow">Fix preview</p><h2 id="fix-preview-title">确认应用候选修复</h2></div>
          <button type="button" aria-label="取消候选修复" disabled={busy} onClick={onCancel}><X size={18} /></button>
        </header>
        <p>尚未写入审查会话。请先核对修改理由、统一 Diff 和验证结果。</p>
        <dl>
          <dt>文件</dt><dd>{candidate.relative_path}</dd>
          <dt>修改理由</dt><dd>{candidate.explanation}</dd>
          <dt>修改前 SHA-256</dt><dd><code>{candidate.base_sha256}</code></dd>
          <dt>修改后 SHA-256</dt><dd><code>{candidate.after_sha256}</code></dd>
          <dt>验证</dt><dd><ul>{candidate.validation.map(item => <li key={item}>{item}</li>)}</ul></dd>
        </dl>
        <UnifiedDiff diff={candidate.diff} label="候选修复统一 Diff" />
        <footer>
          <button type="button" disabled={busy} onClick={onCancel}><X size={16} />取消，不改变会话</button>
          <button type="button" className="decision-action-primary" disabled={busy} onClick={onConfirm}><Check size={16} />{busy ? "正在应用并准备复查…" : "确认应用"}</button>
        </footer>
      </section>
    </div>
  );
}

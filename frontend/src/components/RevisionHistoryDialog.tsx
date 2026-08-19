import { RotateCcw, X } from "lucide-react";

import type { ReviewRevision } from "../api/client";

type RevisionHistoryDialogProps = {
  open: boolean;
  items: ReviewRevision[];
  busyRevisionId: string | null;
  onClose: () => void;
  onUndo: (revisionId: string) => void;
};

export function RevisionHistoryDialog({
  open,
  items,
  busyRevisionId,
  onClose,
  onUndo,
}: RevisionHistoryDialogProps) {
  if (!open) return null;
  const latestActive = items.find((item) => !item.undone_at)?.revision_id ?? null;
  return (
    <div className="revision-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="revision-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="revision-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="revision-dialog-header">
          <div>
            <p className="eyebrow">Code changes</p>
            <h2 id="revision-dialog-title">修改历史</h2>
          </div>
          <button type="button" className="dialog-close" aria-label="关闭修改历史" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <div className="revision-dialog-body">
          {items.length ? items.map((item) => (
            <article className={item.undone_at ? "revision-card revision-undone" : "revision-card"} key={item.revision_id}>
              <div className="revision-card-heading">
                <div>
                  <strong>{item.relative_path}</strong>
                  <span>{new Date(item.created_at).toLocaleString("zh-CN")}</span>
                </div>
                {item.undone_at ? <span className="revision-status">已撤销</span> : null}
              </div>
              {item.explanation ? <p>{item.explanation}</p> : null}
              <pre aria-label={`${item.relative_path} 修改差异`}>{item.diff || "未生成文本差异"}</pre>
              {!item.undone_at ? (
                <button
                  type="button"
                  className="revision-undo"
                  disabled={latestActive !== item.revision_id || busyRevisionId !== null}
                  onClick={() => onUndo(item.revision_id)}
                >
                  <RotateCcw size={15} />
                  {busyRevisionId === item.revision_id ? "正在撤销…" : latestActive === item.revision_id ? "撤销这次修改" : "请先撤销较新的修改"}
                </button>
              ) : null}
            </article>
          )) : <p className="revision-empty">还没有已应用的代码修改。</p>}
        </div>
      </section>
    </div>
  );
}

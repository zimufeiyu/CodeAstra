import { MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";
import { KeyboardEvent, useMemo, useState } from "react";

import type { ReviewHistoryItem } from "../api/client";

type HistorySidebarProps = {
  items: ReviewHistoryItem[];
  activeReviewId?: string | null;
  onNewReview: () => void;
  onOpen: (reviewId: string) => void | Promise<void>;
  onRename: (reviewId: string, title: string) => void | Promise<void>;
  onDelete: (reviewId: string) => void | Promise<void>;
};

type HistoryGroup = {
  label: "今天" | "过去 7 天" | "更早";
  items: ReviewHistoryItem[];
};

function groupHistory(items: ReviewHistoryItem[]): HistoryGroup[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const groups: Record<HistoryGroup["label"], ReviewHistoryItem[]> = {
    今天: [],
    "过去 7 天": [],
    更早: [],
  };
  for (const item of items) {
    const created = new Date(item.created_at);
    created.setHours(0, 0, 0, 0);
    const days = Math.floor((today.getTime() - created.getTime()) / 86_400_000);
    const label = days <= 0 ? "今天" : days <= 6 ? "过去 7 天" : "更早";
    groups[label].push(item);
  }
  return (["今天", "过去 7 天", "更早"] as const)
    .map((label) => ({ label, items: groups[label] }))
    .filter((group) => group.items.length > 0);
}

export function HistorySidebar({
  items,
  activeReviewId,
  onNewReview,
  onOpen,
  onRename,
  onDelete,
}: HistorySidebarProps) {
  const groups = useMemo(() => groupHistory(items), [items]);
  const [menuId, setMenuId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  function beginRename(item: ReviewHistoryItem) {
    setMenuId(null);
    setEditingId(item.review_id);
    setDraftTitle(item.title);
  }

  async function saveRename(item: ReviewHistoryItem) {
    const normalized = draftTitle.trim();
    if (!normalized || normalized.length > 100) return;
    setBusyId(item.review_id);
    try {
      await onRename(item.review_id, normalized);
      setEditingId(null);
    } finally {
      setBusyId(null);
    }
  }

  function handleEditorKey(event: KeyboardEvent<HTMLInputElement>, item: ReviewHistoryItem) {
    if (event.key === "Enter") {
      event.preventDefault();
      void saveRename(item);
    } else if (event.key === "Escape") {
      setEditingId(null);
      setDraftTitle("");
    }
  }

  async function confirmDelete(item: ReviewHistoryItem) {
    setMenuId(null);
    const confirmed = window.confirm(
      `永久删除“${item.title}”？\n删除后无法恢复该审查、事件和追问记录。`,
    );
    if (!confirmed) return;
    setBusyId(item.review_id);
    try {
      await onDelete(item.review_id);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <nav className="history-sidebar" aria-label="审查历史">
      <button type="button" className="new-review-button" onClick={onNewReview}>
        <Plus size={16} aria-hidden="true" />新建审查
      </button>
      <div className="history-scroll">
        {groups.map((group) => (
          <section className="history-group" key={group.label} aria-label={group.label}>
            <p className="history-group-label">{group.label}</p>
            {group.items.map((item) => (
              <div
                className={item.review_id === activeReviewId ? "history-row active" : "history-row"}
                key={item.review_id}
              >
                {editingId === item.review_id ? (
                  <input
                    className="history-rename-input"
                    aria-label={`重命名 ${item.title}`}
                    autoFocus
                    maxLength={100}
                    value={draftTitle}
                    disabled={busyId === item.review_id}
                    onChange={(event) => setDraftTitle(event.target.value)}
                    onKeyDown={(event) => handleEditorKey(event, item)}
                    onBlur={() => {
                      if (busyId !== item.review_id) setEditingId(null);
                    }}
                  />
                ) : (
                  <>
                    <button
                      type="button"
                      className="history-title-button"
                      aria-current={item.review_id === activeReviewId ? "page" : undefined}
                      title={item.title}
                      onClick={() => void onOpen(item.review_id)}
                    >
                      {item.title}
                    </button>
                    <button
                      type="button"
                      className="history-menu-trigger"
                      aria-label={`更多操作 ${item.title}`}
                      aria-expanded={menuId === item.review_id}
                      onClick={() => setMenuId((current) => current === item.review_id ? null : item.review_id)}
                    >
                      <MoreHorizontal size={16} aria-hidden="true" />
                    </button>
                    {menuId === item.review_id ? (
                      <div className="history-menu" role="menu">
                        <button type="button" role="menuitem" onClick={() => beginRename(item)}>
                          <Pencil size={14} aria-hidden="true" />重命名
                        </button>
                        <button type="button" role="menuitem" className="danger" onClick={() => void confirmDelete(item)}>
                          <Trash2 size={14} aria-hidden="true" />删除
                        </button>
                      </div>
                    ) : null}
                  </>
                )}
              </div>
            ))}
          </section>
        ))}
        {!items.length ? <p className="history-empty">暂无审查记录</p> : null}
      </div>
    </nav>
  );
}


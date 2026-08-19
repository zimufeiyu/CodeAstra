import { FileDiff, Files, GitMerge, Plus } from "lucide-react";
import { KeyboardEvent, useEffect, useRef, useState } from "react";

type Props = {
  disabled?: boolean;
  onSelectLocalFiles: () => void;
  onSelectLocalDiff: () => void;
  onSelectGitLab: () => void;
};

export function AttachmentMenu({
  disabled = false,
  onSelectLocalFiles,
  onSelectLocalDiff,
  onSelectGitLab,
}: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (!open) return;
    function closeOutside(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    }
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  function select(action: () => void) {
    setOpen(false);
    action();
  }

  function navigate(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = itemRefs.current.filter((item): item is HTMLButtonElement => item !== null);
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : event.key === "ArrowDown"
          ? (current + 1 + items.length) % items.length
          : (current - 1 + items.length) % items.length;
    items[next]?.focus();
  }

  return (
    <div className="attachment-menu-root" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="composer-plus"
        aria-label="添加内容"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
      >
        <Plus size={20} aria-hidden="true" />
      </button>
      {open ? (
        <div className="attachment-menu" role="menu" aria-label="添加内容" onKeyDown={navigate}>
          <button ref={(node) => { itemRefs.current[0] = node; }} type="button" role="menuitem" onClick={() => select(onSelectLocalFiles)}>
            <Files size={18} aria-hidden="true" />
            <span><strong>选择本地文件</strong><small>上传 Python、C/C++ 文件</small></span>
          </button>
          <button ref={(node) => { itemRefs.current[1] = node; }} type="button" role="menuitem" onClick={() => select(onSelectLocalDiff)}>
            <FileDiff size={18} aria-hidden="true" />
            <span><strong>本地版本对比</strong><small>比较修改前和修改后的代码</small></span>
          </button>
          <button ref={(node) => { itemRefs.current[2] = node; }} type="button" role="menuitem" onClick={() => select(onSelectGitLab)}>
            <GitMerge size={18} aria-hidden="true" />
            <span><strong>从 GitLab 导入</strong><small>读取 Merge Request 变更</small></span>
          </button>
          <p>自动识别 Python / C++</p>
        </div>
      ) : null}
    </div>
  );
}

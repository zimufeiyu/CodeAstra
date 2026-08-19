import { Check, Copy, MessageSquareText } from "lucide-react";
import { KeyboardEvent, MouseEvent, useEffect, useRef, useState } from "react";

import { ReviewSourceFile, SessionFinding } from "../api/client";

type CodeViewerProps = {
  file: ReviewSourceFile;
  findings: SessionFinding[];
  selectedFindingId?: string | null;
  onTextSelection?: (selection: CodeSelection) => void;
  onFindingClick: (findingId: string) => void;
};

export type CodeSelection = {
  text: string;
  start_line?: number;
  end_line?: number;
};

type PendingSelection = CodeSelection & {
  left: number;
  top: number;
};

async function copyText(text: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall back to document.execCommand for non-secure deployments.
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    return document.execCommand?.("copy") ?? false;
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}

function rangeForLine(finding: SessionFinding, lineNumber: number, line: string) {
  if (lineNumber < finding.start_line || lineNumber > finding.end_line) return null;
  const start = lineNumber === finding.start_line ? Math.max(0, finding.start_column - 1) : 0;
  const end = lineNumber === finding.end_line ? Math.max(start + 1, finding.end_column - 1) : line.length;
  return { start: Math.min(start, line.length), end: Math.min(Math.max(end, start + 1), line.length) };
}

export function CodeViewer({ file, findings, selectedFindingId, onFindingClick, onTextSelection }: CodeViewerProps) {
  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null);
  const [copyAllStatus, setCopyAllStatus] = useState<"idle" | "copied" | "failed">("idle");
  const [selectionCopyFailed, setSelectionCopyFailed] = useState(false);
  const selectionActionsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setCopyAllStatus("idle");
    setPendingSelection(null);
    setSelectionCopyFailed(false);
  }, [file.file_id]);

  useEffect(() => {
    if (!pendingSelection) return;
    function closeSelectionActions(event: globalThis.MouseEvent) {
      if (selectionActionsRef.current?.contains(event.target as Node)) return;
      if ((event.target as Element | null)?.closest?.(".code-lines")) return;
      setPendingSelection(null);
    }
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") setPendingSelection(null);
    }
    document.addEventListener("mousedown", closeSelectionActions);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeSelectionActions);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [pendingSelection]);
  function activate(event: KeyboardEvent<HTMLSpanElement>, findingId: string) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onFindingClick(findingId);
    }
  }


  function selectText(event: MouseEvent<HTMLPreElement>) {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) {
      setPendingSelection(null);
      return;
    }
    const range = selection.getRangeAt(0);
    if (!event.currentTarget.contains(range.commonAncestorContainer)) {
      setPendingSelection(null);
      return;
    }
    const text = selection.toString();
    if (!text.trim()) {
      setPendingSelection(null);
      return;
    }
    setSelectionCopyFailed(false);
    const lineForNode = (node: Node) => {
      const element = node instanceof Element ? node : node.parentElement;
      const value = element?.closest<HTMLElement>(".code-line")?.dataset.lineNumber;
      return value ? Number(value) : undefined;
    };
    const startLine = lineForNode(range.startContainer);
    const endLine = lineForNode(range.endContainer);
    const rect = typeof range.getBoundingClientRect === "function" ? range.getBoundingClientRect() : null;
    const center = rect && rect.width ? rect.left + rect.width / 2 : event.clientX;
    setPendingSelection({
      text,
      start_line: startLine && endLine ? Math.min(startLine, endLine) : startLine,
      end_line: startLine && endLine ? Math.max(startLine, endLine) : endLine,
      left: Math.min(window.innerWidth - 132, Math.max(132, center)),
      top: Math.max(12, (rect?.top || event.clientY) - 48),
    });
  }

  async function copyAllCode() {
    const copied = await copyText(file.content);
    setCopyAllStatus(copied ? "copied" : "failed");
  }

  async function copySelectedCode() {
    if (!pendingSelection) return;
    const copied = await copyText(pendingSelection.text);
    if (!copied) {
      setSelectionCopyFailed(true);
      return;
    }
    window.getSelection()?.removeAllRanges();
    setPendingSelection(null);
  }

  function askAboutSelection() {
    if (!pendingSelection) return;
    const { text, start_line, end_line } = pendingSelection;
    onTextSelection?.({ text, start_line, end_line });
    setPendingSelection(null);
  }

  return (
    <section className="code-viewer" aria-label={`完整代码 ${file.relative_path}`}>
      <header className="code-viewer-header">
        <div className="code-viewer-title">
          <strong>{file.relative_path}</strong>
          <span>{file.language}</span>
        </div>
        <button type="button" className="code-copy-button" onClick={copyAllCode} aria-label={"复制完整代码 " + file.relative_path}>
          {copyAllStatus === "copied" ? <Check size={15} /> : <Copy size={15} />}
          {copyAllStatus === "copied" ? "已复制" : copyAllStatus === "failed" ? "复制失败" : "复制完整代码"}
        </button>
      </header>
      <pre className="code-lines" onMouseUp={selectText}>
        {file.content.split("\n").map((line, index) => {
          const lineNumber = index + 1;
          const lineFindings = findings.filter((item) => rangeForLine(item, lineNumber, line));
          const finding = lineFindings.find((item) => item.finding_id === selectedFindingId) ?? lineFindings[0];
          const range = finding ? rangeForLine(finding, lineNumber, line) : null;
          const startingFindings = findings.filter((item) => item.start_line === lineNumber);
          return (
            <div
              className="code-line"
              data-line-number={lineNumber}
              id={`code-line-${file.file_id}-${lineNumber}`}
              key={lineNumber}
            >
              <span className="line-number" aria-hidden="true">{lineNumber}</span>
              <code>
                {finding && range ? (
                  <>
                    {line.slice(0, range.start)}
                    <span
                      className={`code-highlight severity-${finding.severity}${selectedFindingId === finding.finding_id ? " selected" : ""}`}
                      role="button"
                      tabIndex={0}
                      title={finding.hover_summary}
                      aria-label={`${finding.title}，第 ${lineNumber} 行`}
                      onClick={() => onFindingClick(finding.finding_id)}
                      onKeyDown={(event) => activate(event, finding.finding_id)}
                    >
                      {line.slice(range.start, range.end) || " "}
                    </span>
                    {line.slice(range.end)}
                  </>
                ) : (
                  line || " "
                )}
              </code>
              {startingFindings.length > 1 ? (
                <span className="code-line-finding-stack" aria-label={`第 ${lineNumber} 行有 ${startingFindings.length} 个问题`}>
                  {startingFindings.map((item, findingIndex) => (
                    <button
                      type="button"
                      className={item.finding_id === selectedFindingId ? "active" : ""}
                      key={item.finding_id}
                      title={item.title}
                      aria-label={`问题 ${findingIndex + 1}：${item.title}`}
                      onClick={() => onFindingClick(item.finding_id)}
                    >
                      {findingIndex + 1}
                    </button>
                  ))}
                </span>
              ) : null}
            </div>
          );
        })}
      </pre>
      {pendingSelection ? (
        <div ref={selectionActionsRef} className="code-selection-actions" role="toolbar" aria-label="代码选区操作" style={{ left: pendingSelection.left, top: pendingSelection.top }}>
          <button type="button" onClick={copySelectedCode} aria-label="复制选中代码"><Copy size={15} />{selectionCopyFailed ? "复制失败" : "复制"}</button>
          <button type="button" onClick={askAboutSelection} aria-label="针对选区追问"><MessageSquareText size={15} />追问</button>
        </div>
      ) : null}
    </section>
  );
}

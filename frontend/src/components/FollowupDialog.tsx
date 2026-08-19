import { Send, X } from "lucide-react";
import { FormEvent, useState } from "react";

import type { FollowupMessage } from "../api/client";

export type FollowupContext = {
  kind: "finding" | "selection";
  title: string;
  detail: string;
  fileId: string;
  findingId?: string;
  startLine?: number;
  endLine?: number;
  selectedCode?: string;
};

type FollowupDialogProps = {
  context: FollowupContext;
  messages: FollowupMessage[];
  assistantName: string;
  error?: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (question: string) => boolean | void | Promise<boolean | void>;
};

export function FollowupDialog({ context, messages, assistantName, error, busy, onClose, onSubmit }: FollowupDialogProps) {
  const [question, setQuestion] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized || busy) return;
    try {
      const succeeded = await onSubmit(normalized);
      if (succeeded !== false) setQuestion("");
    } catch {
      // The owning screen renders the request error and keeps the draft for retry.
    }
  }

  return (
    <div className="followup-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="followup-dialog" role="dialog" aria-modal="true" aria-label="针对代码的二次追问" onMouseDown={(event) => event.stopPropagation()}>
        <header className="followup-dialog-header">
          <div><p className="eyebrow">{context.kind === "finding" ? "针对审查问题" : "针对所选代码"}</p><h2>{context.title}</h2></div>
          <button type="button" className="dialog-close" aria-label="关闭二次追问" onClick={onClose}><X size={18} aria-hidden="true" /></button>
        </header>
        <div className="followup-context-card">
          <div className="followup-context-heading"><span>已自动附加代码上下文</span><span>{context.kind === "finding" ? "审查问题" : "代码选区"}</span></div>
          <pre className="followup-context">{context.detail}</pre>
        </div>
        <div className="followup-dialog-messages" aria-live="polite">
          {messages.map((message) => <article className={`followup-message followup-${message.role}`} key={message.message_id}><span>{message.role === "user" ? "你" : assistantName}</span><p>{message.content}</p></article>)}
          {!messages.length ? <p className="muted">可以询问原因、影响、修改方式或替代方案。</p> : null}
        </div>
        {error ? <p className="auth-error" role="alert">{error}</p> : null}
        <form className="followup-dialog-form" onSubmit={submit}>
          <label className="sr-only" htmlFor="context-followup-question">针对当前代码追问</label>
          <textarea id="context-followup-question" aria-label="针对当前代码追问" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="针对当前问题或代码输入追问" maxLength={4000} autoFocus disabled={busy} />
          <button type="submit" className="composer-send" aria-label={busy ? "正在回答" : "发送追问"} title={busy ? "正在回答" : "发送追问"} disabled={busy || !question.trim()}><Send size={16} aria-hidden="true" /><span className="sr-only">{busy ? "正在回答" : "发送追问"}</span></button>
        </form>
      </section>
    </div>
  );
}
import { Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RepairIntentOption, UseDefEvidence } from "../api/client";

type Props = {
  evidence: UseDefEvidence;
  busy: boolean;
  error?: string;
  onCancel: () => void;
  onSubmit: (option: RepairIntentOption, value?: string) => void;
};

function findBestRecommendation(evidence: UseDefEvidence): RepairIntentOption | undefined {
  if (evidence.outcome !== "safe_plan") return undefined;
  const deterministic = evidence.options.filter(
    (option) => option.kind !== "custom_behavior" && option.kind !== "defer",
  );
  const confidentRename = deterministic.find(
    (option) => option.kind === "rename_existing"
      && evidence.similar_candidates?.some(
        (candidate) => candidate.name === option.symbol && candidate.confidence >= 0.85,
      ),
  );
  if (confidentRename) return confidentRename;
  const imports = deterministic.filter((option) => option.kind === "import_symbol");
  if (imports.length === 1 && evidence.cross_file_exports?.length === 1) return imports[0];
  return deterministic.length === 1 ? deterministic[0] : undefined;
}

export function RepairIntentDialog({ evidence, busy, error, onCancel, onSubmit }: Props) {
  const recommendation = useMemo(() => findBestRecommendation(evidence), [evidence]);
  const customOption = evidence.options.find((option) => option.kind === "custom_behavior");
  const [recommendationSelected, setRecommendationSelected] = useState(false);
  const [recommendationValue, setRecommendationValue] = useState("");
  const [customGoal, setCustomGoal] = useState("");
  const recommendationButtonRef = useRef<HTMLButtonElement>(null);
  const customGoalRef = useRef<HTMLTextAreaElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const recommendationNeedsInput = recommendation?.requires_input !== "none";
  const recommendationReady = Boolean(
    recommendationSelected
      && recommendation
      && (!recommendationNeedsInput || recommendationValue.trim()),
  );
  const customReady = Boolean(customOption && customGoal.trim());
  const canGenerate = !busy && (recommendationReady || customReady);

  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    (recommendation ? recommendationButtonRef.current : customGoalRef.current)?.focus();
    return () => previousFocusRef.current?.focus();
  }, [recommendation]);

  const generate = () => {
    if (!canGenerate) return;
    if (recommendationSelected && recommendation) {
      onSubmit(recommendation, recommendationValue.trim() || undefined);
      return;
    }
    if (customOption) onSubmit(customOption, customGoal.trim());
  };

  return (
    <div className="fix-preview-backdrop repair-intent-backdrop" role="presentation">
      <section
        className="fix-preview-dialog repair-intent-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="repair-intent-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) {
            event.preventDefault();
            onCancel();
          } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            generate();
          }
        }}
      >
        <header>
          <div>
            <p className="eyebrow">CodeAstra</p>
            <h2 id="repair-intent-title">确认修改目标</h2>
          </div>
          <button type="button" onClick={onCancel} disabled={busy} aria-label="关闭确认修改目标">
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="repair-intent-body">
          <p className="repair-intent-intro">
            已定位 <strong>{evidence.unresolved_name}</strong> 所在语句。请确认推荐修改，或直接告诉 CodeAstra 你希望如何调整行为。
          </p>
          <code className="repair-intent-statement">{evidence.statement_text}</code>

          {recommendation ? (
            <button
              ref={recommendationButtonRef}
              type="button"
              className={`repair-intent-recommendation${recommendationSelected ? " selected" : ""}`}
              aria-pressed={recommendationSelected}
              onClick={() => setRecommendationSelected(true)}
              disabled={busy}
            >
              <Sparkles size={16} aria-hidden="true" />
              <span><strong>推荐修改</strong>{recommendation.label}</span>
            </button>
          ) : (
            <p className="repair-intent-unavailable" role="status">
              当前没有可安全推断的唯一修改，请描述你的业务目标。
            </p>
          )}

          {recommendationSelected && recommendationNeedsInput ? (
            <label className="repair-intent-specialized-input">
              {recommendation?.input_label
                ?? (recommendation?.requires_input === "module" ? "导入来源" : "初始化表达式")}
              <input
                value={recommendationValue}
                onChange={(event) => setRecommendationValue(event.target.value)}
                disabled={busy}
                autoFocus
              />
            </label>
          ) : null}

          <label className="repair-intent-custom">
            <span>告诉 CodeAstra 你希望如何修改</span>
            <textarea
              ref={customGoalRef}
              aria-label="告诉 CodeAstra 你希望如何修改"
              value={customGoal}
              onChange={(event) => {
                setCustomGoal(event.target.value);
                if (event.target.value) setRecommendationSelected(false);
              }}
              disabled={busy}
              maxLength={2000}
              placeholder={`例如：${evidence.unresolved_name} 缺失时提前返回空结果，并保持现有调用方行为不变`}
            />
            <small>文字只用于生成受限候选；检查 Diff 并再次确认后才会修改代码。</small>
          </label>

          {error ? <p className="repair-intent-error" role="alert">{error}</p> : null}
        </div>

        <footer className="repair-intent-footer">
          <button type="button" onClick={onCancel} disabled={busy}>取消</button>
          <button
            type="button"
            className="decision-action-primary"
            onClick={generate}
            disabled={!canGenerate}
          >
            {busy ? "正在生成…" : "生成修改预览"}
          </button>
        </footer>
      </section>
    </div>
  );
}

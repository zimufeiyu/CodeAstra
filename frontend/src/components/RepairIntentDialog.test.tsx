import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { UseDefEvidence } from "../api/client";
import { RepairIntentDialog } from "./RepairIntentDialog";

const evidence: UseDefEvidence = {
  unresolved_name: "valux",
  scope_kind: "function",
  statement_kind: "Assign",
  statement_start_line: 8,
  statement_end_line: 8,
  statement_text: "result = valux",
  explanation: "找到唯一高置信拼写候选。",
  outcome: "safe_plan",
  similar_candidates: [
    { name: "value", kind: "parameter", confidence: 0.94, rationale: "同一作用域唯一近似参数" },
  ],
  options: [
    { option_id: "rename:value", kind: "rename_existing", label: "将 valux 改为 value", symbol: "value", requires_input: "none" },
    { option_id: "declare_local", kind: "declare_local", label: "声明局部变量", requires_input: "initializer", input_label: "初始化表达式" },
    { option_id: "custom_behavior", kind: "custom_behavior", label: "描述期望的业务行为", requires_input: "behavior", input_label: "期望行为" },
  ],
};

describe("RepairIntentDialog", () => {
  it("renders one compact recommendation and one primary custom goal", () => {
    const submit = vi.fn();
    render(<RepairIntentDialog evidence={evidence} busy={false} onCancel={vi.fn()} onSubmit={submit} />);

    expect(screen.getByRole("dialog", { name: "确认修改目标" })).toBeInTheDocument();
    const recommendation = screen.getByRole("button", { name: /推荐修改.*将 valux 改为 value/ });
    expect(recommendation).toHaveFocus();
    expect(screen.getAllByText("推荐修改")).toHaveLength(1);
    expect(screen.queryByText("声明局部变量")).not.toBeInTheDocument();
    expect(screen.queryByText("请输入必要信息")).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    const generate = screen.getByRole("button", { name: "生成修改预览" });
    expect(generate).toBeDisabled();
    fireEvent.click(recommendation);
    expect(generate).toBeEnabled();
    fireEvent.click(generate);
    expect(submit).toHaveBeenCalledWith(evidence.options[0], undefined);
  });

  it("focuses the textarea without a recommendation and submits with Ctrl+Enter", () => {
    const submit = vi.fn();
    const noRecommendation = {
      ...evidence,
      outcome: "needs_intent" as const,
      similar_candidates: [],
      options: [evidence.options[2]],
    };
    render(
      <RepairIntentDialog
        evidence={noRecommendation}
        busy={false}
        onCancel={vi.fn()}
        onSubmit={submit}
      />,
    );
    const input = screen.getByRole("textbox", { name: "告诉 CodeAstra 你希望如何修改" });
    expect(input).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("没有可安全推断的唯一修改");
    fireEvent.change(input, { target: { value: "缩写的目的：缺失时提前返回空结果" } });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(submit).toHaveBeenCalledWith(
      evidence.options[2],
      "缩写的目的：缺失时提前返回空结果",
    );
  });

  it("shows one specialized field only after selecting a recommendation that needs it", () => {
    const submit = vi.fn();
    const declareEvidence: UseDefEvidence = {
      ...evidence,
      similar_candidates: [],
      options: [evidence.options[1], evidence.options[2]],
    };
    render(<RepairIntentDialog evidence={declareEvidence} busy={false} onCancel={vi.fn()} onSubmit={submit} />);
    expect(screen.queryByLabelText("初始化表达式")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /推荐修改.*声明局部变量/ }));
    const initializer = screen.getByLabelText("初始化表达式");
    expect(screen.getByRole("button", { name: "生成修改预览" })).toBeDisabled();
    fireEvent.change(initializer, { target: { value: "{}" } });
    fireEvent.click(screen.getByRole("button", { name: "生成修改预览" }));
    expect(submit).toHaveBeenCalledWith(declareEvidence.options[0], "{}");
  });

  it("keeps errors inside the dialog and Escape cancels and restores focus", () => {
    const previous = document.createElement("button");
    previous.textContent = "应用修复";
    document.body.append(previous);
    previous.focus();
    const cancel = vi.fn();
    const view = render(
      <RepairIntentDialog
        evidence={evidence}
        busy={false}
        error="候选完整文件第 8 行仍有语法错误"
        onCancel={cancel}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("第 8 行仍有语法错误");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(cancel).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(previous).toHaveFocus();
    previous.remove();
  });
});

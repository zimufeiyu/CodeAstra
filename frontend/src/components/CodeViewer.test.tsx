import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CodeViewer } from "./CodeViewer";

describe("CodeViewer", () => {
  it("renders complete source and highlights an exact finding range", async () => {
    const onFindingClick = vi.fn();
    render(
      <CodeViewer
        file={{
          file_id: "file-1",
          relative_path: "snippet.py",
          language: "python",
          content: "user_input = input()\neval(user_input)\n",
          sha256: "a".repeat(64),
          line_offsets: [0, 22, 39],
        }}
        findings={[
          {
            finding_id: "finding-1",
            source: "static",
            analyzer: "python-ast",
            rule_id: "python.dangerous-eval",
            category: "security",
            severity: "high",
            confidence: 1,
            file_id: "file-1",
            start_line: 2,
            start_column: 1,
            end_line: 2,
            end_column: 17,
            title: "危险的 eval 调用",
            hover_summary: "不可信输入进入 eval。",
            detail: "详细说明",
            evidence: "eval(user_input)",
            impact: "影响",
            suggestion: "建议",
            verification: {
              range_valid: true,
              evidence_matched: true,
              static_confirmed: true,
              cross_file_checked: false,
              deduplicated: false,
            },
          },
          {
            finding_id: "finding-2",
            source: "llm",
            analyzer: "qwen3",
            rule_id: "python.eval-dataflow",
            category: "security",
            severity: "medium",
            confidence: 0.9,
            file_id: "file-1",
            start_line: 2,
            start_column: 6,
            end_line: 2,
            end_column: 16,
            title: "eval 参数未校验",
            hover_summary: "eval 参数需要校验。",
            detail: "第二个问题",
            evidence: "user_input",
            impact: "影响",
            suggestion: "建议",
            verification: {
              range_valid: true,
              evidence_matched: true,
              static_confirmed: false,
              cross_file_checked: false,
              deduplicated: false,
            },
          },
        ]}
        selectedFindingId="finding-1"
        onFindingClick={onFindingClick}
      />,
    );

    expect(screen.getByText("user_input = input()")).toBeInTheDocument();
    const highlight = screen.getByRole("button", { name: "危险的 eval 调用，第 2 行" });
    expect(highlight).toHaveAttribute("title", "不可信输入进入 eval。");
    await userEvent.click(highlight);
    expect(onFindingClick).toHaveBeenCalledWith("finding-1");

    const secondFinding = screen.getByRole("button", { name: "问题 2：eval 参数未校验" });
    await userEvent.click(secondFinding);
    expect(onFindingClick).toHaveBeenCalledWith("finding-2");
  });

  it("preserves indentation and waits for an explicit copy or follow-up choice", async () => {
    const onTextSelection = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const { container } = render(
      <CodeViewer
        file={{
          file_id: "file-copy",
          relative_path: "indented.py",
          language: "python",
          content: "def run():\n    return value\n",
          sha256: "b".repeat(64),
          line_offsets: [0, 11, 28],
        }}
        findings={[]}
        onFindingClick={vi.fn()}
        onTextSelection={onTextSelection}
      />,
    );
    const selectedNode = container.querySelector('[data-line-number="2"] code')?.firstChild as Node;
    const getSelection = vi.spyOn(window, "getSelection").mockReturnValue({
      isCollapsed: false,
      rangeCount: 1,
      toString: () => "    return value",
      getRangeAt: () => ({
        commonAncestorContainer: selectedNode,
        startContainer: selectedNode,
        endContainer: selectedNode,
        getBoundingClientRect: () => ({ left: 120, top: 120, width: 80 }),
      }),
      removeAllRanges: vi.fn(),
    } as unknown as Selection);

    fireEvent.mouseUp(container.querySelector(".code-lines") as HTMLElement, { clientX: 160, clientY: 120 });
    expect(onTextSelection).not.toHaveBeenCalled();
    expect(screen.getByRole("toolbar", { name: "代码选区操作" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "针对选区追问" }));
    expect(onTextSelection).toHaveBeenCalledWith({
      text: "    return value",
      start_line: 2,
      end_line: 2,
    });

    await userEvent.click(screen.getByRole("button", { name: "复制完整代码 indented.py" }));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("def run():\n    return value\n"));
    getSelection.mockRestore();
  });
});

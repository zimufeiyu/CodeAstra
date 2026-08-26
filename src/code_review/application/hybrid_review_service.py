from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import re
import textwrap
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from code_review.application.analyzer_adapters import LibCSTRenameAdapter
from code_review.application.chunk_prompt import ChunkPromptBuilder
from code_review.application.chunk_review_service import ChunkReviewService
from code_review.application.context_budget import ContextBudgeter
from code_review.application.finding_verifier import FindingVerifier
from code_review.application.inference_service import FixCandidateError, ReviewOutputError
from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.python_analysis_cache import (
    parse_python_content,
    python_parse_cache_info,
)
from code_review.application.recheck_context import current_recheck_attempt
from code_review.application.review_aggregator import ReviewAggregator
from code_review.application.review_planner import ReviewPlanner, significant_lines
from code_review.application.review_policy import ReviewPolicyEngine
from code_review.application.semantic_judge import SemanticJudgePort
from code_review.application.static_analysis import StaticAnalyzer
from code_review.config.settings import GatewaySettings
from code_review.domain.model_protocol import (
    ChatMessage,
    FixProposal,
    InferenceRequest,
    ModelSelection,
)
from code_review.domain.review_chunks import ChunkStatus, ReviewChunk, ReviewPlanningError
from code_review.domain.review_models import (
    Finding,
    FindingDecisionAudit,
    FixCandidate,
    FollowupMessage,
    RepairIntentSelection,
    ReviewEvent,
    ReviewMode,
    ReviewOrigin,
    ReviewRevision,
    ReviewSession,
    ReviewSummary,
    SourceFile,
    SymbolRepairPlan,
)
from code_review.domain.review_ports import ReviewInferencePort, ReviewStorePort

_MAX_FULL_FILE_SYNTAX_FIX_CHARS = 6000
_MAX_FULL_FILE_SYNTAX_FIX_LINES = 120
_MAX_LOCAL_SYNTAX_BLOCK_CHARS = 6000
_MAX_LOCAL_SYNTAX_BLOCK_LINES = 160
_SYNTAX_FIX_PADDING_LINES = 8
_MR_CHANGED_LINE_MARGIN = 2
_MAX_REVISIONS = 20
_TOP_LEVEL_DEFINITION_RE = re.compile(r"^(?:async\s+def|def|class)\s+")
_TOP_LEVEL_BOUNDARY_RE = re.compile(r"^(?:@|async\s+def|def|class)\s*")
_FIX_CANDIDATE_TTL_MINUTES = 15
_MAX_FIX_CANDIDATES_PER_OWNER = 20
_MAX_FIX_CANDIDATES_PER_REVIEW = 8
_REVALIDATION_BATCH_TIMEOUT_SECONDS = 60
_REVALIDATION_CANCEL_GRACE_SECONDS = 2


class RecheckCleanupInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepairScope:
    file_sha: str
    node_kind: str
    start_line: int
    end_line: int
    expected_symbol: str | None
    replacement_mode: Literal[
        "full_file", "expression", "statement", "statement_block", "definition", "replace_span"
    ]


@dataclass(frozen=True)
class ReplacementPlan:
    scope: RepairScope
    replacement_sha: str


@dataclass(frozen=True)
class _PreparedFix:
    owner_id: str
    candidate: FixCandidate
    revised_content: str
    plan: ReplacementPlan
    fix_start: int
    fix_end: int
    replacement_line_count: int


@dataclass(frozen=True)
class _PythonEditTarget:
    mode: Literal["expression", "statement", "statement_block", "definition"]
    node_kind: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    indent: str
    expected_symbol: str | None
    expected_decorators: tuple[str, ...]
    containing_function: tuple[int, int, str, type[ast.stmt]] | None


def _node_lines(node: ast.stmt) -> tuple[int, int]:
    start = min([node.lineno, *[item.lineno for item in getattr(node, "decorator_list", [])]])
    return start, node.end_lineno or node.lineno


def _byte_column_to_character(line: str, column: int) -> int:
    return len(line.encode("utf-8")[:column].decode("utf-8", errors="ignore"))


def _node_position(
    node: ast.AST, source_lines: list[str]
) -> tuple[tuple[int, int], tuple[int, int]]:
    start_line = getattr(node, "lineno", 1)
    start_column = getattr(node, "col_offset", 0)
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.decorator_list
    ):
        first_decorator = min(node.decorator_list, key=lambda item: (item.lineno, item.col_offset))
        start_line = first_decorator.lineno
        start_column = max(0, first_decorator.col_offset - 1)
    end_line = getattr(node, "end_lineno", start_line) or start_line
    end_column = getattr(node, "end_col_offset", None)
    start_text = source_lines[start_line - 1] if 1 <= start_line <= len(source_lines) else ""
    end_text = source_lines[end_line - 1] if 1 <= end_line <= len(source_lines) else ""
    return (
        (start_line, _byte_column_to_character(start_text, start_column)),
        (
            end_line,
            _byte_column_to_character(end_text, end_column)
            if isinstance(end_column, int)
            else len(end_text.rstrip("\r\n")),
        ),
    )


def _normalized_code(value: str) -> str:
    return " ".join(value.casefold().split())


def _derive_python_edit_target(content: str, finding: Finding) -> _PythonEditTarget:
    """Derive a stable edit category and character boundary from the base revision AST."""

    tree = parse_python_content(content)
    source_lines = content.splitlines(keepends=True)
    if not source_lines:
        raise FixCandidateError("scope_mismatch", "系统无法安全定位空文件中的替换边界。")
    start_line = min(finding.start_line, len(source_lines))
    end_line = min(finding.end_line, len(source_lines))
    start_text = source_lines[start_line - 1].rstrip("\r\n")
    end_text = source_lines[end_line - 1].rstrip("\r\n")
    finding_start = (start_line, min(max(finding.start_column - 1, 0), len(start_text)))
    finding_end = (end_line, min(max(finding.end_column - 1, 0), len(end_text)))
    precise_columns = finding.end_column - 1 <= len(end_text)

    expression_candidates: list[tuple[int, int, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr) or not hasattr(node, "end_lineno"):
            continue
        node_start, node_end = _node_position(node, source_lines)
        segment = ast.get_source_segment(content, node) or ""
        evidence_matches = (
            bool(segment)
            and node_start[0] <= start_line
            and node_end[0] >= end_line
            and _normalized_code(segment) == _normalized_code(finding.evidence)
        )
        location_matches = (
            precise_columns and node_start <= finding_start and node_end >= finding_end
        )
        if evidence_matches or location_matches:
            expression_candidates.append(
                ((node_end[0] - node_start[0]), max(0, node_end[1] - node_start[1]), node)
            )
    selected_node: ast.AST | None = None
    mode: Literal["expression", "statement", "statement_block", "definition"]
    block_nodes: list[ast.stmt] = []
    if expression_candidates:
        selected_node = min(expression_candidates, key=lambda item: (item[0], item[1]))[2]
        mode = "expression"
    else:
        block_candidates: list[tuple[int, int, list[ast.stmt]]] = []
        for parent in ast.walk(tree):
            for _, value in ast.iter_fields(parent):
                if not isinstance(value, list) or not value or not all(
                    isinstance(item, ast.stmt) for item in value
                ):
                    continue
                overlapping = [
                    item
                    for item in value
                    if _node_lines(item)[1] >= start_line and _node_lines(item)[0] <= end_line
                ]
                if not overlapping:
                    continue
                block_start = _node_lines(overlapping[0])[0]
                block_end = _node_lines(overlapping[-1])[1]
                if block_start <= start_line and block_end >= end_line:
                    block_candidates.append(
                        (block_end - block_start, len(overlapping), overlapping)
                    )
        if not block_candidates:
            raise FixCandidateError(
                "scope_mismatch", "系统无法安全定位替换边界；请将问题定位到具体表达式或完整语句。"
            )
        block_nodes = min(block_candidates, key=lambda item: (item[0], item[1]))[2]
        if len(block_nodes) > 1:
            selected_node = block_nodes[0]
            mode = "statement_block"
        else:
            selected_node = block_nodes[0]
            mode = (
                "definition"
                if isinstance(selected_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else "statement"
            )

    assert selected_node is not None
    if mode == "statement_block":
        node_start, _ = _node_position(block_nodes[0], source_lines)
        _, node_end = _node_position(block_nodes[-1], source_lines)
        node_kind = "statement_block"
        expected_symbol = None
    else:
        node_start, node_end = _node_position(selected_node, source_lines)
        node_kind = type(selected_node).__name__
        expected_symbol = (
            selected_node.name
            if isinstance(selected_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else None
        )
    target_line = source_lines[node_start[0] - 1]
    indent = target_line[: len(target_line) - len(target_line.lstrip(" \t"))]
    if " " in indent and "\t" in indent:
        raise FixCandidateError(
            "scope_mismatch", "系统无法安全定位替换边界：目标行混用了制表符和空格缩进。"
        )
    containing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _node_lines(node)[0] <= node_start[0]
        and _node_lines(node)[1] >= node_end[0]
    ]
    containing_function = None
    if containing:
        function = min(containing, key=lambda item: _node_lines(item)[1] - _node_lines(item)[0])
        function_start, function_end = _node_lines(function)
        containing_function = (function_start, function_end, function.name, type(function))
    return _PythonEditTarget(
        mode=mode,
        node_kind=node_kind,
        start_line=node_start[0],
        end_line=node_end[0],
        start_column=node_start[1],
        end_column=node_end[1],
        indent=indent,
        expected_symbol=expected_symbol,
        expected_decorators=(
            tuple(ast.unparse(item) for item in selected_node.decorator_list)
            if isinstance(selected_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else ()
        ),
        containing_function=containing_function,
    )


def _definition_edit_target(
    content: str, definition: tuple[int, int, str, type[ast.stmt]]
) -> _PythonEditTarget:
    tree = parse_python_content(content)
    source_lines = content.splitlines(keepends=True)
    matching = [
        node
        for node in ast.walk(tree)
        if type(node) is definition[3]
        and getattr(node, "name", None) == definition[2]
        and _node_lines(node) == definition[:2]
    ]
    if len(matching) != 1:
        raise FixCandidateError(
            "scope_mismatch", "系统无法安全定位包含当前问题的完整函数边界。"
        )
    node = matching[0]
    node_start, node_end = _node_position(node, source_lines)
    line = source_lines[node_start[0] - 1]
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    return _PythonEditTarget(
        mode="definition",
        node_kind=type(node).__name__,
        start_line=node_start[0],
        end_line=node_end[0],
        start_column=node_start[1],
        end_column=node_end[1],
        indent=indent,
        expected_symbol=definition[2],
        expected_decorators=tuple(ast.unparse(item) for item in node.decorator_list),
        containing_function=definition,
    )


def _logical_python_replacement(replacement: str, target: _PythonEditTarget) -> str:
    replacement = _strip_replacement_fence(replacement)
    prefixes = [
        re.match(r"^[ \t]*", line).group(0)
        for line in replacement.splitlines()
        if line.strip()
    ]
    if any(" " in prefix and "\t" in prefix for prefix in prefixes):
        raise FixCandidateError(
            "replacement_indent_invalid", "模型内容无效：候选混用了制表符和空格缩进。"
        )
    logical = textwrap.dedent(replacement).strip("\r\n")
    if not logical:
        if target.mode in {"statement", "statement_block"}:
            return ""
        raise FixCandidateError(
            "model_output_invalid", f"模型内容无效：{target.mode} replacement 不能为空。"
        )
    first = next((line for line in logical.splitlines() if line.strip()), "")
    if first[:1] in {" ", "\t"}:
        raise FixCandidateError(
            "replacement_indent_invalid", "模型内容无效：候选无法归一化到目标语法层级。"
        )
    logical_prefixes = [
        re.match(r"^[ \t]*", line).group(0)
        for line in logical.splitlines()
        if line.strip()
    ]
    if any("\t" in prefix for prefix in logical_prefixes) and " " in target.indent:
        raise FixCandidateError(
            "replacement_indent_invalid", "模型内容无效：候选缩进风格与目标文件不一致。"
        )
    if any(" " in prefix for prefix in logical_prefixes) and "\t" in target.indent:
        raise FixCandidateError(
            "replacement_indent_invalid", "模型内容无效：候选缩进风格与目标文件不一致。"
        )
    try:
        if target.mode == "expression":
            ast.parse(logical, mode="eval")
        elif target.mode == "definition":
            body = ast.parse(logical).body
            if len(body) != 1 or not isinstance(
                body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                raise SyntaxError("definition replacement must contain exactly one definition")
            if body[0].name != target.expected_symbol or type(body[0]).__name__ != target.node_kind:
                raise SyntaxError("definition identity does not match the server target")
            if (
                tuple(ast.unparse(item) for item in body[0].decorator_list)
                != target.expected_decorators
            ):
                raise SyntaxError("definition decorators do not match the server target")
        else:
            wrapper = "def __codeastra_scope__():\n" + textwrap.indent(logical, "    ") + "\n"
            body = ast.parse(wrapper).body[0].body
            if target.mode == "statement" and len(body) != 1:
                raise SyntaxError("statement replacement must contain exactly one statement")
            if target.mode == "statement_block" and not body:
                raise SyntaxError("statement block replacement must not be empty")
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for node in body
            ):
                raise SyntaxError("definition content is not valid for a statement target")
    except SyntaxError as error:
        raise FixCandidateError(
            "model_output_invalid",
            f"模型内容无效：replacement 不是有效的 {target.mode} 语法内容。",
        ) from error
    return logical


def _assemble_python_edit(
    content: str, target: _PythonEditTarget, replacement: str
) -> tuple[str, str]:
    logical = _logical_python_replacement(replacement, target)
    lines = content.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in content else "\n"
    if target.mode == "expression":
        start = sum(len(line) for line in lines[: target.start_line - 1]) + target.start_column
        end = sum(len(line) for line in lines[: target.end_line - 1]) + target.end_column
        return content[:start] + logical + content[end:], logical
    start = sum(len(line) for line in lines[: target.start_line - 1])
    end = sum(len(line) for line in lines[: target.end_line])
    rendered = "\n".join(
        target.indent + line if line else line for line in logical.splitlines()
    )
    if rendered and (end < len(content) or content[end - len(newline) : end] == newline):
        rendered += newline
    return content[:start] + rendered + content[end:], rendered.rstrip("\r\n")


def _syntax_error_caret(content: str, error: SyntaxError) -> str:
    line_number = error.lineno or 1
    lines = content.splitlines()
    line = lines[line_number - 1] if 1 <= line_number <= len(lines) else ""
    column = max(1, error.offset or 1)
    return f"{line_number}: {line}\n   {' ' * (column - 1)}^"


def _validate_assembled_python_target(content: str, target: _PythonEditTarget) -> None:
    tree = ast.parse(content)
    lines = content.splitlines(keepends=True)
    if target.mode == "definition":
        matching = [
            node
            for node in ast.walk(tree)
            if type(node).__name__ == target.node_kind
            and getattr(node, "name", None) == target.expected_symbol
            and _node_position(node, lines)[0][0] == target.start_line
        ]
    elif target.mode == "expression":
        matching = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.expr)
            and _node_position(node, lines)[0] <= (target.start_line, target.start_column)
            and _node_position(node, lines)[1] >= (target.start_line, target.start_column + 1)
        ]
    else:
        matching = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.stmt)
            and _node_position(node, lines)[0][0] == target.start_line
        ]
    if not matching:
        raise FixCandidateError(
            "model_output_invalid",
            f"模型内容无效：组装后目标位置没有形成预期的 {target.mode} AST 节点。",
        )


def _python_fix_scopes(
    content: str, finding: Finding
) -> tuple[tuple[int, int], tuple[int, int, str, type[ast.stmt]] | None]:
    tree = parse_python_content(content)
    statements = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and _node_lines(node)[0] <= finding.start_line
        and _node_lines(node)[1] >= finding.end_line
    ]
    if not statements:
        return (finding.start_line, finding.end_line), None
    smallest = min(
        statements,
        key=lambda node: (_node_lines(node)[1] - _node_lines(node)[0], -_node_lines(node)[0]),
    )
    definitions = [
        node
        for node in statements
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    enclosing = None
    if definitions:
        definition = min(definitions, key=lambda node: _node_lines(node)[1] - _node_lines(node)[0])
        start, end = _node_lines(definition)
        enclosing = (start, end, definition.name, type(definition))
    return _node_lines(smallest), enclosing


def _single_definition(
    replacement: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None:
    try:
        body = ast.parse(textwrap.dedent(replacement)).body
    except SyntaxError:
        return None
    if len(body) == 1 and isinstance(
        body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    ):
        return body[0]
    return None


def _strip_replacement_fence(replacement: str) -> str:
    """Remove one complete Markdown code fence without changing code indentation."""

    text = replacement.strip("\r\n")
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("```"):
        return text
    if len(lines) < 3 or not re.fullmatch(r"```\s*", lines[-1].strip()):
        raise FixCandidateError("model_output_invalid", "候选代码围栏未完整闭合。")
    if not re.fullmatch(r"```(?:python|py)?\s*", lines[0].strip(), re.IGNORECASE):
        raise FixCandidateError("model_output_invalid", "候选包含不支持的代码围栏标记。")
    if any(line.strip().startswith("```") for line in lines[1:-1]):
        raise FixCandidateError("model_output_invalid", "候选包含嵌套代码围栏。")
    return "\n".join(lines[1:-1]).strip("\r\n")


def _apply_symbol_repair(content: str, plan: SymbolRepairPlan) -> str:
    """Apply a bounded deterministic symbol edit without inventing intent."""
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != plan.base_sha:
        raise FixCandidateError("stale_revision", "符号修复计划对应的文件版本已经变化。")
    lines = content.splitlines(keepends=True)
    if plan.mode == "rename":
        if not plan.replacement_name:
            raise FixCandidateError("ambiguous_symbol", "没有选择唯一的替换符号。")
        line = lines[plan.use_line - 1]
        start = plan.use_column - 1
        if line[start : start + len(plan.unresolved_name)] != plan.unresolved_name:
            raise FixCandidateError("scope_mismatch", "未定义名称的位置与当前源码不一致。")
        lines[plan.use_line - 1] = (
            line[:start] + plan.replacement_name + line[start + len(plan.unresolved_name) :]
        )
        return "".join(lines)
    if plan.mode == "declare_local":
        if not plan.initializer:
            raise FixCandidateError("needs_intent", "请输入局部变量的初始化表达式。")
        try:
            ast.parse(plan.initializer, mode="eval")
        except SyntaxError as error:
            raise FixCandidateError(
                "model_output_invalid", "初始化表达式不是有效的 Python 表达式。"
            ) from error
        line = lines[plan.statement_start_line - 1]
        indent = line[: len(line) - len(line.lstrip())]
        newline = "\r\n" if "\r\n" in content else "\n"
        lines.insert(
            plan.statement_start_line - 1,
            f"{indent}{plan.unresolved_name} = {plan.initializer}{newline}",
        )
        return "".join(lines)
    tree = parse_python_content(content)
    if plan.mode == "import":
        if not plan.module or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", plan.module):
            raise FixCandidateError("ambiguous_symbol", "没有选择有效且唯一的导入模块。")
        insert_after = 0
        for index, node in enumerate(tree.body):
            if (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                or isinstance(node, (ast.Import, ast.ImportFrom))
            ):
                insert_after = node.end_lineno or node.lineno
            else:
                break
        newline = "\r\n" if "\r\n" in content else "\n"
        lines.insert(insert_after, f"from {plan.module} import {plan.unresolved_name}{newline}")
        return "".join(lines)
    if plan.mode == "declare_parameter":
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= plan.use_line <= (node.end_lineno or node.lineno)
        ]
        if not functions:
            raise FixCandidateError("scope_mismatch", "未定义名称不在可修改的函数参数作用域内。")
        target = min(functions, key=lambda node: (node.end_lineno or node.lineno) - node.lineno)
        body_line = target.body[0].lineno if target.body else target.lineno + 1
        header = "".join(lines[target.lineno - 1 : body_line - 1])
        close = header.rfind(")")
        if close < 0:
            raise FixCandidateError("scope_mismatch", "无法唯一定位函数参数列表。")
        separator = "" if header[:close].rstrip().endswith("(") else ", "
        revised = header[:close] + separator + plan.unresolved_name + header[close:]
        lines[target.lineno - 1 : body_line - 1] = revised.splitlines(keepends=True)
        return "".join(lines)
    raise FixCandidateError("finding_not_actionable", "不支持该符号修复方式。")


def _normalize_statement_replacement(replacement: str, indent: str) -> str:
    """Align a statement block to its AST span; full-file parsing is authoritative."""

    dedented = textwrap.dedent(replacement).strip("\r\n")
    if not dedented.strip():
        return ""
    return "\n".join(indent + line if line else line for line in dedented.splitlines())


def _python_repair_context(
    content: str,
    start_line: int,
    end_line: int,
    enclosing: tuple[int, int, str, type[ast.stmt]] | None,
) -> str:
    """Return exact target lines plus imports and the enclosing symbol signature."""
    tree = parse_python_content(content)
    lines = content.splitlines(keepends=True)
    selected = set(range(start_line, end_line + 1))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            selected.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    if enclosing is not None:
        selected.add(enclosing[0])
    return "".join(
        f"{line_number}: {lines[line_number - 1]}"
        for line_number in sorted(selected)
        if 1 <= line_number <= len(lines)
    )


def _python_enclosing_context(
    content: str,
    enclosing: tuple[int, int, str, type[ast.stmt]] | None,
    start_line: int,
    end_line: int,
) -> str:
    lines = content.splitlines(keepends=True)
    context_start, context_end = (
        (enclosing[0], enclosing[1]) if enclosing is not None else (start_line, end_line)
    )
    return "".join(
        f"{line_number}: {lines[line_number - 1]}"
        for line_number in range(context_start, context_end + 1)
        if 1 <= line_number <= len(lines)
    )


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _finding_in_review_scope(
    finding: Finding,
    session: ReviewSession,
) -> bool:
    origin = session.origin
    if origin is None:
        return True
    path = next(
        (source.relative_path for source in session.files if source.file_id == finding.file_id),
        None,
    )
    if path is None:
        return False
    if path not in origin.selected_paths:
        return True
    if origin.changed_ranges is None:
        return True
    ranges = origin.changed_ranges.get(path, [])
    if finding.rule_id == "python.syntax-error":
        return True
    if not ranges:
        return False
    return any(
        finding.start_line <= changed.end_line + _MR_CHANGED_LINE_MARGIN
        and finding.end_line >= max(1, changed.start_line - _MR_CHANGED_LINE_MARGIN)
        for changed in ranges
    )


def _python_syntax_fix_scope(
    source_lines: list[str],
    finding: Finding,
) -> tuple[int, int]:
    """Prefer a complete top-level definition over an arbitrary line window."""
    if not source_lines:
        return 1, 1

    error_index = min(max(finding.start_line - 1, 0), len(source_lines) - 1)
    definition_start: int | None = None
    for index in range(error_index, -1, -1):
        if _TOP_LEVEL_DEFINITION_RE.match(source_lines[index]):
            definition_start = index
            break

    if definition_start is not None:
        block_start = definition_start
        while block_start > 0 and source_lines[block_start - 1].startswith("@"):
            block_start -= 1

        block_end = len(source_lines)
        for index in range(definition_start + 1, len(source_lines)):
            if _TOP_LEVEL_BOUNDARY_RE.match(source_lines[index]):
                block_end = index
                break

        block = "".join(source_lines[block_start:block_end])
        if (
            block_end - block_start <= _MAX_LOCAL_SYNTAX_BLOCK_LINES
            and len(block) <= _MAX_LOCAL_SYNTAX_BLOCK_CHARS
        ):
            return block_start + 1, block_end

    return (
        max(1, finding.start_line - _SYNTAX_FIX_PADDING_LINES),
        min(len(source_lines), finding.end_line + _SYNTAX_FIX_PADDING_LINES),
    )


def finding_fingerprints(
    finding: Finding,
    files: list[SourceFile],
) -> set[str]:
    """Build stable issue identities from both evidence and the referenced source."""
    source = next((item for item in files if item.file_id == finding.file_id), None)
    path = source.relative_path if source is not None else finding.file_id
    anchors = {" ".join(finding.evidence.casefold().split())}
    if finding.rule_id == "python.syntax-error":
        anchors.add("file-level-python-syntax-error")
    if source is not None:
        lines = source.content.splitlines()
        start = max(0, finding.start_line - 1)
        end = min(len(lines), max(finding.end_line, finding.start_line))
        source_span = " ".join(" ".join(line.casefold().split()) for line in lines[start:end])
        if source_span:
            anchors.add(source_span)

    return {
        hashlib.sha256(
            "\x1f".join((path.casefold(), finding.rule_id.casefold(), anchor)).encode("utf-8")
        ).hexdigest()
        for anchor in anchors
        if anchor
    }


def _finding_summary(findings: list[Finding], *, completed: bool = False) -> ReviewSummary:
    counts = {level: 0 for level in ("critical", "high", "medium", "low", "info")}
    for finding in findings:
        counts[finding.severity] += 1
    if findings and completed:
        text = f"审查完成，发现 {len(findings)} 个需要处理的问题。"
    elif findings:
        text = f"剩余 {len(findings)} 个待处理问题。"
    elif completed:
        text = "审查完成，未发现新的明确问题。"
    else:
        text = "当前问题已全部处理，准备统一复查。"
    return ReviewSummary(
        total=len(findings),
        critical=counts["critical"],
        high=counts["high"],
        medium=counts["medium"],
        low=counts["low"],
        info=counts["info"],
        text=text,
    )


def _unified_source_diff(before: str, after: str, relative_path: str) -> str:
    """Build a diff with explicit markers when an edited line has no final newline."""
    parts: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative_path}",
        tofile=f"b/{relative_path}",
    ):
        parts.append(line)
        if not line.endswith(("\n", "\r")):
            parts.append("\n\\ No newline at end of file\n")
    return "".join(parts)


def _has_pending_revalidation(session: ReviewSession) -> bool:
    return any(
        state == "fixed_pending_revalidation"
        for state in session.finding_states.values()
    )


def _semantic_batch_timeout(session: ReviewSession, configured_timeout: int) -> int | None:
    if not _has_pending_revalidation(session):
        return None
    return min(configured_timeout, _REVALIDATION_BATCH_TIMEOUT_SECONDS)


def _followup_code_context(
    session: ReviewSession,
    context: dict[str, object] | None,
) -> tuple[str, str]:
    if not context:
        return "review", "未指定活动代码上下文。"
    file_id = context.get("file_id")
    if not isinstance(file_id, str):
        raise ValueError("未指定有效的活动文件")
    source = next((item for item in session.files if item.file_id == file_id), None)
    if source is None:
        raise ValueError("活动文件不属于当前审查")

    start_line = _positive_int(context.get("start_line"))
    end_line = _positive_int(context.get("end_line"))
    finding_id = context.get("finding_id")
    context_kind = context.get("kind")
    lines = source.content.splitlines()
    finding_section = ""
    if isinstance(finding_id, str):
        finding = next((item for item in session.findings if item.finding_id == finding_id), None)
        if finding is None or finding.file_id != file_id:
            raise ValueError("审查问题不属于当前文件")
        if (start_line is not None and start_line != finding.start_line) or (
            end_line is not None and end_line != finding.end_line
        ):
            raise ValueError("审查问题的代码范围不匹配")
        start_line, end_line = finding.start_line, finding.end_line
        context_key = f"finding:{finding.finding_id}"
        finding_section = (
            f"\n当前审查问题：[{finding.severity}] {finding.title}\n"
            f"证据：{finding.evidence}\n影响：{finding.impact}\n建议：{finding.suggestion}"
        )
    elif context_kind == "finding":
        raise ValueError("未指定有效的审查问题")
    else:
        if start_line is None or end_line is None or start_line > end_line:
            raise ValueError("所选代码范围无效")
        if end_line > len(lines):
            raise ValueError("所选代码范围超出文件内容")
        selected = context.get("selected_code")
        selected_code = selected.strip() if isinstance(selected, str) else ""
        range_code = "\n".join(lines[start_line - 1 : end_line])
        if selected_code and selected_code not in range_code:
            raise ValueError("所选代码不属于指定范围")
        context_key = "selection:{}:{}:{}:{}".format(
            source.file_id,
            start_line,
            end_line,
            hashlib.sha256(range_code.encode("utf-8")).hexdigest()[:16],
        )

    first_index = max(0, start_line - 21)
    last_index = min(len(lines), end_line + 20)
    excerpt = "\n".join(f"{index + 1}: {lines[index]}" for index in range(first_index, last_index))
    if len(excerpt) > 12000:
        excerpt = excerpt[:12000] + "\n…代码上下文已截断…"
    return context_key, (
        f"活动文件：{source.relative_path}（{source.language}）\n"
        f"活动范围：{start_line}-{end_line}\n"
        f"附近源码：\n{excerpt or '空文件'}"
        f"{finding_section}"
    )


class HybridReviewService:
    def __init__(
        self,
        inference_service: ReviewInferencePort,
        store: ReviewStorePort,
        analyzer: StaticAnalyzer | None = None,
        settings: GatewaySettings | None = None,
        model_profiles: dict[str, ModelSelection] | None = None,
        available_model_profile_ids: frozenset[str] | None = None,
        model_context_tokens: dict[str, int] | None = None,
        semantic_judge: SemanticJudgePort | None = None,
        review_policy: ReviewPolicyEngine | None = None,
    ) -> None:
        self._inference_service = inference_service
        self._store = store
        self._metrics = PipelineMetrics()
        self._analyzer = analyzer or StaticAnalyzer(metrics=self._metrics)
        self._finding_verifier = FindingVerifier(metrics=self._metrics)
        self._settings = settings or GatewaySettings()
        self._planner = ReviewPlanner(self._metrics)
        local_profile = ModelSelection(
            profile_id="local-qwen3-8b",
            provider="local",
            model=self._settings.model_name,
            display_name="\u672c\u5730 Qwen3-8B",
        )
        self._model_profiles = model_profiles or {local_profile.profile_id: local_profile}
        self._available_model_profile_ids = (
            available_model_profile_ids
            if available_model_profile_ids is not None
            else frozenset(self._model_profiles)
        )
        context_by_profile = model_context_tokens or {
            profile_id: self._settings.model_context_tokens for profile_id in self._model_profiles
        }
        self._executors = {
            profile_id: ChunkReviewService(
                inference_service=inference_service,
                store=store,
                planner=self._planner,
                prompt_builder=ChunkPromptBuilder(
                    ContextBudgeter(
                        context_tokens=context_by_profile.get(
                            profile_id,
                            self._settings.model_context_tokens,
                        ),
                        safety_tokens=self._settings.context_safety_tokens,
                        minimum_output_tokens=self._settings.minimum_output_tokens,
                        maximum_output_tokens=self._settings.max_output_tokens,
                    )
                ),
                max_split_depth=self._settings.max_chunk_split_depth,
                finding_verifier=self._finding_verifier,
                metrics=self._metrics,
                semantic_judge=semantic_judge,
            )
            for profile_id in self._model_profiles
        }
        default_executor = self._executors.get(self._settings.default_model_profile_id)
        if default_executor is None:
            default_executor = next(iter(self._executors.values()))
        self._executor = default_executor
        self._aggregator = ReviewAggregator(self._metrics, review_policy)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._orphaned_recheck_tasks: set[asyncio.Task[None]] = set()
        self._orphaned_rechecks: dict[str, asyncio.Task[None]] = {}
        self._fix_candidates: dict[str, _PreparedFix] = {}
        self._fix_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False

    def pipeline_counters(self) -> dict[str, int]:
        counters = self._metrics.snapshot()
        cache = python_parse_cache_info()
        counters.update(
            {
                "ast_cache_hits": cache["hits"],
                "ast_cache_misses": cache["misses"],
                "ast_cache_size": cache["size"],
            }
        )
        return counters

    def record_pipeline_counter(self, name: str) -> None:
        self._metrics.increment(name)

    async def create(
        self,
        mode: ReviewMode,
        files: list[SourceFile],
        *,
        owner_id: str,
        origin: ReviewOrigin | None = None,
        model_profile_id: str = "local-qwen3-8b",
        model: ModelSelection | None = None,
    ) -> ReviewSession:
        if not files:
            raise ValueError("至少需要一个待审查文件")
        if model is not None:
            if (
                model.profile_id != "deepseek-api"
                or model.provider != "deepseek"
                or model.selection_source not in {"auto", "manual"}
            ):
                raise ValueError("无效的动态模型配置")
            model_profile_id = model.profile_id
        else:
            model = self._model_profiles.get(model_profile_id)
        if model is None:
            raise ValueError("\u672a\u77e5\u7684\u6a21\u578b\u914d\u7f6e")
        if model_profile_id not in self._available_model_profile_ids:
            raise ValueError(
                f"{model.display_name} "
                "\u5c1a\u672a\u914d\u7f6e API Key\uff0c\u5f53\u524d\u4e0d\u53ef\u7528"
            )
        session = ReviewSession.create(
            review_id=f"review-{uuid.uuid4().hex}",
            owner_id=owner_id,
            mode=mode,
            files=files,
            origin=origin,
            model=model,
            retention_hours=self._settings.review_retention_hours,
        )
        await self._store.create(session)
        return session

    async def get(self, review_id: str, owner_id: str) -> ReviewSession | None:
        return await self._store.get(review_id, owner_id)

    async def list_sessions(self, owner_id: str, limit: int, offset: int) -> list[ReviewSession]:
        return await self._store.list_sessions(owner_id, limit, offset)

    async def rename(self, review_id: str, owner_id: str, title: str) -> ReviewSession:
        normalized = title.strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("标题长度必须为 1–100 个字符")
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        renamed = await self._store.update_title(review_id, owner_id, normalized)
        # Title is updated atomically so a running review cannot overwrite it.
        return renamed

    async def delete(self, review_id: str, owner_id: str) -> bool:
        if await self._store.get(review_id, owner_id) is None:
            return False
        await self.cancel(review_id, owner_id)
        self._tasks.pop(review_id, None)
        return await self._store.delete(review_id, owner_id)

    async def _restart_review_in_place(
        self,
        session: ReviewSession,
    ) -> ReviewSession:
        self._metrics.increment("re_review_count")
        restarted = session.model_copy(
            update={
                "status": "queued",
                "findings": [],
                "coverage": [],
                "summary": _finding_summary([]),
                "error": None,
                "error_code": None,
                "recheck_attempt_id": f"recheck-{uuid.uuid4().hex}",
                "recheck_attempt_status": "running",
                "recheck_deadline_at": datetime.now(tz=UTC)
                + timedelta(seconds=_REVALIDATION_BATCH_TIMEOUT_SECONDS),
            }
        )
        await self._store.reset_review(restarted)
        await self.start(restarted.review_id, restarted.owner_id)
        return restarted

    async def revisions(self, review_id: str, owner_id: str) -> list[ReviewRevision]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        current_by_file = {item.file_id: item.content for item in session.files}
        repaired: list[ReviewRevision] = []
        for index, revision in enumerate(session.revisions):
            later = next(
                (
                    item
                    for item in session.revisions[index + 1 :]
                    if item.file_id == revision.file_id
                ),
                None,
            )
            after_content = (
                later.before_content
                if later is not None
                else current_by_file.get(revision.file_id)
            )
            if revision.undone_at is None and after_content is not None:
                revision = revision.model_copy(
                    update={
                        "diff": _unified_source_diff(
                            revision.before_content,
                            after_content,
                            revision.relative_path,
                        )
                    }
                )
            repaired.append(revision)
        return list(reversed(repaired))

    async def undo_revision(
        self,
        review_id: str,
        owner_id: str,
        revision_id: str,
    ) -> ReviewSession:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status not in {"completed", "failed"}:
            raise ValueError("只有已结束的审查可以撤销修改")
        active_revisions = [item for item in session.revisions if item.undone_at is None]
        if not active_revisions or active_revisions[-1].revision_id != revision_id:
            raise ValueError("请从最新一次未撤销的修改开始撤销")
        revision = active_revisions[-1]
        source = next((item for item in session.files if item.file_id == revision.file_id), None)
        if source is None:
            raise ValueError("修改对应的文件已不存在")
        if source.sha256 != revision.after_sha256:
            raise ValueError("当前文件已发生后续变化，无法安全撤销该修改")
        restored_files = [
            SourceFile.from_content(
                file_id=item.file_id,
                relative_path=item.relative_path,
                language=item.language,
                content=(
                    revision.before_content if item.file_id == revision.file_id else item.content
                ),
            )
            for item in session.files
        ]
        restored_revisions = [
            item.model_copy(update={"undone_at": datetime.now(tz=UTC)})
            if item.revision_id == revision_id
            else item
            for item in session.revisions
        ]
        restored_origin = session.origin
        if (
            restored_origin is not None
            and restored_origin.changed_ranges is not None
            and revision.before_changed_ranges is not None
        ):
            changed_ranges = dict(restored_origin.changed_ranges)
            changed_ranges[revision.relative_path] = revision.before_changed_ranges
            restored_origin = restored_origin.model_copy(update={"changed_ranges": changed_ranges})
        decisions = dict(session.finding_decisions)
        decisions.pop(revision.finding_id, None)
        restored = session.model_copy(
            update={
                "files": restored_files,
                "origin": restored_origin,
                "revisions": restored_revisions,
                "finding_decisions": decisions,
            }
        )
        await self._store.save(restored)
        return await self._restart_review_in_place(restored)

    async def record_finding_decision(
        self,
        review_id: str,
        owner_id: str,
        finding_id: str,
        decision: Literal["accepted_risk", "deferred", "dismissed"],
    ) -> tuple[ReviewSession, ReviewSession | None]:
        lock = self._fix_locks.setdefault(review_id, asyncio.Lock())
        async with lock:
            return await self._record_finding_decision_locked(
                review_id, owner_id, finding_id, decision
            )

    async def _record_finding_decision_locked(
        self,
        review_id: str,
        owner_id: str,
        finding_id: str,
        decision: Literal["accepted_risk", "deferred", "dismissed"],
    ) -> tuple[ReviewSession, ReviewSession | None]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status not in {"completed", "failed"}:
            raise ValueError("只有已结束的审查可以处理修复建议")
        finding = next(
            (item for item in session.findings if item.finding_id == finding_id),
            session.decided_findings.get(finding_id),
        )
        if finding is None:
            raise LookupError(finding_id)
        decisions = dict(session.finding_decisions)
        decisions[finding_id] = decision
        states = dict(session.finding_states)
        states[finding_id] = decision
        decided_findings = dict(session.decided_findings)
        decided_findings[finding_id] = finding
        decision_reasons = {
            "accepted_risk": "用户明确接受该问题的风险。",
            "deferred": "用户将该问题保留为待处理。",
            "dismissed": "用户判定该问题不成立。",
        }
        decision_history = [
            *session.finding_decision_history,
            FindingDecisionAudit(
                finding_id=finding_id,
                action="decided",
                decision=decision,
                reason=decision_reasons[decision],
            ),
        ][-200:]
        ignored = set(session.ignored_finding_fingerprints)
        if decision in {"accepted_risk", "dismissed"}:
            ignored.update(finding_fingerprints(finding, session.files))
        else:
            ignored.difference_update(finding_fingerprints(finding, session.files))
        active_findings = [item for item in session.findings if item.finding_id != finding_id]
        if decision == "deferred":
            active_findings.append(finding)
        decided = session.model_copy(
            update={
                "finding_decisions": decisions,
                "finding_states": states,
                "decided_findings": decided_findings,
                "finding_decision_history": decision_history,
                "ignored_finding_fingerprints": sorted(ignored),
                "findings": active_findings,
                "summary": _finding_summary(active_findings, completed=True),
            }
        )
        await self._store.save(decided)
        if active_findings and not all(item.finding_id in decisions for item in active_findings):
            return decided, None
        if not any(
            state == "fixed_pending_revalidation"
            for state in decided.finding_states.values()
        ):
            return decided, None
        return decided, await self._restart_review_in_place(decided)

    async def reopen_finding(
        self,
        review_id: str,
        owner_id: str,
        finding_id: str,
    ) -> tuple[ReviewSession, bool, bool]:
        """Restore a decided finding to active navigation without semantic re-review."""
        lock = self._fix_locks.setdefault(review_id, asyncio.Lock())
        async with lock:
            session = await self._store.get(review_id, owner_id)
            if session is None:
                raise KeyError(review_id)
            if session.status not in {"completed", "failed"}:
                raise ValueError("只有已结束的审查可以重新打开问题")
            finding = session.decided_findings.get(finding_id)
            if finding is None:
                raise LookupError(finding_id)
            revision_retained = any(
                item.finding_id == finding_id and item.undone_at is None
                for item in session.revisions
            )
            if finding_id not in session.finding_decisions and any(
                item.finding_id == finding_id for item in session.findings
            ):
                return session, revision_retained, True

            decisions = dict(session.finding_decisions)
            previous_decision = decisions.pop(finding_id, None)
            if previous_decision is None:
                raise ValueError("该问题当前不是已处理状态")
            states = dict(session.finding_states)
            states[finding_id] = "reopened"
            ignored = set(session.ignored_finding_fingerprints)
            ignored.difference_update(finding_fingerprints(finding, session.files))
            active_findings = list(session.findings)
            if not any(item.finding_id == finding_id for item in active_findings):
                active_findings.append(finding)
            reason = (
                "用户重新检查该问题；已经应用的代码修订仍然保留。"
                if revision_retained
                else "用户将该问题重新打开并恢复为待处理。"
            )
            reopened = session.model_copy(
                update={
                    "finding_decisions": decisions,
                    "finding_states": states,
                    "ignored_finding_fingerprints": sorted(ignored),
                    "findings": active_findings,
                    "summary": _finding_summary(active_findings, completed=True),
                    "finding_decision_history": [
                        *session.finding_decision_history,
                        FindingDecisionAudit(
                            finding_id=finding_id,
                            action="reopened",
                            decision=previous_decision,
                            reason=reason,
                            revision_retained=revision_retained,
                        ),
                    ][-200:],
                }
            )
            await self._store.save(reopened)
            return reopened, revision_retained, False

    def _cleanup_fix_candidates(self) -> None:
        now = datetime.now(tz=UTC)
        expired = [
            key for key, value in self._fix_candidates.items() if value.candidate.expires_at <= now
        ]
        for key in expired:
            self._fix_candidates.pop(key, None)

    def _store_fix_candidate(self, prepared: _PreparedFix) -> None:
        self._cleanup_fix_candidates()
        owner_items = [
            item for item in self._fix_candidates.values() if item.owner_id == prepared.owner_id
        ]
        review_items = [
            item for item in owner_items if item.candidate.review_id == prepared.candidate.review_id
        ]
        for items, limit in (
            (review_items, _MAX_FIX_CANDIDATES_PER_REVIEW),
            (owner_items, _MAX_FIX_CANDIDATES_PER_OWNER),
        ):
            overflow = len(items) - limit + 1
            for item in sorted(items, key=lambda value: value.candidate.created_at)[
                : max(0, overflow)
            ]:
                self._fix_candidates.pop(item.candidate.candidate_id, None)
        self._fix_candidates[prepared.candidate.candidate_id] = prepared

    def _fix_output_token_budget(self, target_code: str) -> int:
        estimated = 1024 + max(0, len(target_code) // 2)
        return max(
            self._settings.minimum_output_tokens,
            min(self._settings.max_output_tokens, estimated),
        )

    async def preview_fix(
        self,
        review_id: str,
        owner_id: str,
        finding_id: str,
        intent: RepairIntentSelection | None = None,
        *,
        followup_instruction: str | None = None,
        expected_base_sha: str | None = None,
    ) -> FixCandidate:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status not in {"completed", "failed"}:
            raise ValueError("只有已结束的审查可以生成修复候选")
        finding = next(
            (item for item in session.findings if item.finding_id == finding_id),
            session.decided_findings.get(finding_id),
        )
        if finding is None:
            raise LookupError(finding_id)

        source = next(item for item in session.files if item.file_id == finding.file_id)
        if expected_base_sha is not None and expected_base_sha != source.sha256:
            raise FixCandidateError("stale_revision", "文件版本已经变化，请重新生成追问修复候选。")
        custom_user_intent: str | None = None
        if followup_instruction is not None:
            custom_user_intent = followup_instruction.strip()
            if not custom_user_intent or len(custom_user_intent) > 2000:
                raise FixCandidateError(
                    "root_cause_unverified", "追问修复指令必须为 1 至 2000 个字符。"
                )
        if intent is not None:
            if custom_user_intent is not None:
                raise FixCandidateError("scope_mismatch", "不能同时提交两种修复意图。")
            if intent.review_id != review_id or intent.finding_id != finding_id:
                raise FixCandidateError("scope_mismatch", "修复意图与当前审查或问题不匹配。")
            if intent.base_sha != source.sha256:
                raise FixCandidateError("stale_revision", "文件版本已经变化，请重新打开意图选择。")
        if finding.rule_id == "python.undefined-name" and finding.use_def_evidence is None:
            refreshed = await self._analyzer.analyze(session.files, external_standard=False)
            finding = next(
                (
                    item
                    for item in refreshed.findings
                    if item.file_id == finding.file_id
                    and item.rule_id == finding.rule_id
                    and item.start_line == finding.start_line
                    and item.evidence == finding.evidence
                ),
                finding,
            )
        symbol_plan = finding.symbol_repair_plan
        if finding.rule_id == "python.undefined-name" and intent is not None and symbol_plan:
            evidence = finding.use_def_evidence
            option = (
                next(
                    (item for item in evidence.options if item.option_id == intent.option_id),
                    None,
                )
                if evidence is not None
                else None
            )
            expected_kind = "rename_existing" if symbol_plan.mode == "rename" else "import_symbol"
            expected_value = (
                symbol_plan.replacement_name if symbol_plan.mode == "rename" else symbol_plan.module
            )
            supplied_value = (
                intent.selected_symbol if symbol_plan.mode == "rename" else intent.import_source
            )
            if (
                option is None
                or intent.intent_kind != expected_kind
                or option.kind != expected_kind
                or supplied_value != expected_value
            ):
                raise FixCandidateError(
                    "ambiguous_symbol", "所选修复不是服务端为当前版本签发的安全计划。"
                )
        if finding.rule_id == "python.undefined-name" and symbol_plan is None:
            evidence = finding.use_def_evidence
            if evidence is None:
                raise FixCandidateError(
                    "root_cause_unverified", "无法构建未定义名称的 use-def 证据。"
                )
            if intent is None and custom_user_intent is None:
                raise FixCandidateError(
                    "needs_intent",
                    "已定位症状，但无法唯一确定开发者意图。请选择一种明确处理方式。",
                    details={
                        "base_sha": source.sha256,
                        "use_def_evidence": evidence.model_dump(mode="json"),
                    },
                )
            if intent is not None:
                option = next(
                    (item for item in evidence.options if item.option_id == intent.option_id), None
                )
                if option is None:
                    raise FixCandidateError(
                        "ambiguous_symbol", "所选修复选项不是服务端为当前版本签发的选项。"
                    )
                if option.kind != intent.intent_kind:
                    raise FixCandidateError("ambiguous_symbol", "修复意图类型与服务端选项不一致。")
                if option.kind == "defer":
                    raise FixCandidateError(
                        "needs_intent", "请选择有效的修复意图；系统不会猜测默认值。"
                    )
                if option.kind == "rename_existing" and (
                    not option.symbol or intent.selected_symbol != option.symbol
                ):
                    raise FixCandidateError(
                        "ambiguous_symbol", "替换符号不是当前作用域中签发的候选。"
                    )
                if option.kind == "import_symbol" and (
                    not option.module
                    or intent.import_source != option.module
                    or intent.selected_symbol != evidence.unresolved_name
                ):
                    raise FixCandidateError(
                        "ambiguous_symbol", "导入来源或符号不是服务端签发的候选。"
                    )
                if option.kind == "declare_parameter" and intent.selected_symbol not in {
                    None,
                    evidence.unresolved_name,
                }:
                    raise FixCandidateError("ambiguous_symbol", "参数名称必须与未解析名称一致。")
                if option.kind != "declare_local" and intent.initializer is not None:
                    raise FixCandidateError("ambiguous_symbol", "该修复选项不接受初始化表达式。")
                if option.kind == "custom_behavior":
                    if (
                        intent.selected_symbol is not None
                        or intent.import_source is not None
                        or intent.initializer is not None
                        or intent.user_intent is None
                        or not intent.user_intent.strip()
                    ):
                        raise FixCandidateError(
                            "ambiguous_symbol", "自定义行为必须只携带有界的自然语言意图。"
                        )
                    custom_user_intent = intent.user_intent.strip()
                else:
                    if intent.user_intent is not None:
                        raise FixCandidateError(
                            "ambiguous_symbol", "服务端签发的确定性选项不接受自定义行为。"
                        )
                    mode = (
                        "rename"
                        if option.kind == "rename_existing"
                        else "import"
                        if option.kind == "import_symbol"
                        else option.kind
                    )
                    symbol_plan = SymbolRepairPlan(
                        mode=mode,
                        unresolved_name=evidence.unresolved_name,
                        replacement_name=(
                            option.symbol if option.kind == "rename_existing" else None
                        ),
                        module=option.module if option.kind == "import_symbol" else None,
                        initializer=intent.initializer if option.kind == "declare_local" else None,
                        definition_symbol=evidence.scope_symbol,
                        statement_start_line=evidence.statement_start_line,
                        statement_end_line=evidence.statement_end_line,
                        use_line=evidence.use_line,
                        use_column=evidence.use_column,
                        base_sha=source.sha256,
                        safety="requires_review",
                        user_selected=True,
                    )
        verified = self._finding_verifier.verify_existing(finding, session.files)
        if verified.action == "suppress":
            failure_code = (
                verified.code if verified.code != "verified" else "finding_not_actionable"
            )
            raise FixCandidateError(failure_code, verified.message)
        source_lines = source.content.splitlines(keepends=True)
        is_python_syntax_fix = (
            source.language == "python" and finding.rule_id == "python.syntax-error"
        )
        enclosing_definition: tuple[int, int, str, type[ast.stmt]] | None = None
        python_target: _PythonEditTarget | None = None
        parsed_python = False
        if source.language == "python" and not is_python_syntax_fix:
            try:
                python_target = _derive_python_edit_target(source.content, finding)
                fix_start, fix_end = python_target.start_line, python_target.end_line
                enclosing_definition = python_target.containing_function
                parsed_python = True
            except SyntaxError:
                pass
        replace_whole_file = (
            is_python_syntax_fix
            and len(source.content) <= _MAX_FULL_FILE_SYNTAX_FIX_CHARS
            and len(source_lines) <= _MAX_FULL_FILE_SYNTAX_FIX_LINES
        )
        if replace_whole_file:
            fix_start = 1
            fix_end = len(source_lines)
            excerpt_start = 0
            excerpt_end = len(source_lines)
        elif is_python_syntax_fix:
            fix_start, fix_end = _python_syntax_fix_scope(source_lines, finding)
            excerpt_start = max(0, fix_start - 11)
            excerpt_end = min(len(source_lines), fix_end + 10)
        elif not parsed_python:
            fix_start = finding.start_line
            fix_end = finding.end_line
            excerpt_start = max(0, finding.start_line - 21)
            excerpt_end = min(len(source_lines), finding.end_line + 20)
        else:
            excerpt_start = max(0, fix_start - 11)
            excerpt_end = min(len(source_lines), fix_end + 10)

        requested_start = fix_start
        requested_end = fix_end
        requested_mode: Literal[
            "full_file", "expression", "statement", "statement_block", "definition", "replace_span"
        ] = (
            "full_file"
            if replace_whole_file
            else python_target.mode
            if python_target is not None
            else "replace_span"
        )
        requested_symbol = (
            python_target.expected_symbol
            if python_target is not None and python_target.expected_symbol
            else "-"
        )
        requested_anchor = (
            f"sha256:{source.sha256}:lines:{requested_start}-{requested_end}:"
            f"columns:{python_target.start_column if python_target is not None else 0}-"
            f"{python_target.end_column if python_target is not None else 0}:"
            f"node:{python_target.node_kind if python_target is not None else requested_mode}:"
            f"symbol:{requested_symbol}"
        )

        output_token_budget = self._fix_output_token_budget(
            source.content if replace_whole_file else "".join(source_lines[fix_start - 1 : fix_end])
        )

        excerpt = (
            _python_repair_context(source.content, fix_start, fix_end, enclosing_definition)
            if parsed_python
            else "".join(
                f"{index + 1}: {source_lines[index]}" for index in range(excerpt_start, excerpt_end)
            )
        )
        parser_detail = ""
        if is_python_syntax_fix:
            try:
                ast.parse(source.content, filename=source.relative_path)
            except SyntaxError as original_error:
                parser_detail = (
                    f"\nPython 解析器报错：{original_error.msg}"
                    f"（第 {original_error.lineno or finding.start_line} 行，"
                    f"第 {original_error.offset or finding.start_column} 列）"
                )

        if replace_whole_file:
            scope_instruction = (
                "这是 Python 语法错误。replacement 必须是修复后的完整文件；"
            )
        elif python_target is not None:
            scope_instruction = (
                f"服务端已从 base_sha 的 AST 推导目标类别为 {python_target.mode}。"
                "你只负责输出该类别的逻辑代码内容，不决定缩进、边界或 splice；"
            )
        else:
            scope_instruction = (
                f"replacement 只包含替换第 {fix_start}-{fix_end} 行的完整代码，"
                "不得包含行号；"
            )

        async def request_proposal(correction: str = "") -> FixProposal:
            try:
                proposal = await self._inference_service.propose_fix(
                    InferenceRequest(
                        request_id=f"{review_id}:fix:{finding_id}:{uuid.uuid4().hex}",
                        messages=[
                            ChatMessage(
                                role="system",
                                content=(
                                    "你是代码修复助手。只修复指定问题，不改变无关行为。"
                                    f"{scope_instruction}"
                                    "FixProposal 的 target_file、base_sha、start_line、end_line、"
                                    "replacement_mode、anchor 必须原样复制请求中的结构化修复范围；"
                                    "\u53ea\u8fd4\u56de\u7b26\u5408 FixProposal schema \u7684 "
                                    "JSON \u5bf9\u8c61\uff0c"
                                    "\u4e0d\u8981\u4f7f\u7528 Markdown\uff1b"
                                    "explanation 使用简洁中文。"
                                ),
                            ),
                            ChatMessage(
                                role="user",
                                content=(
                                    f"文件：{source.relative_path}\n"
                                    f"基础 SHA：{source.sha256}\n"
                                    f"稳定锚点：{requested_anchor}\n"
                                    f"替换模式：{requested_mode}\n"
                                    f"问题：{finding.title}\n"
                                    f"修复范围：{requested_start}-{requested_end}\n"
                                    f"证据：{finding.evidence}\n"
                                    f"建议：{finding.suggestion}\n"
                                    f"用户确认的业务意图：{custom_user_intent or '无'}"
                                    f"{parser_detail}\n"
                                    f"上下文：\n{excerpt}{correction}"
                                ),
                            ),
                        ],
                        max_output_tokens=output_token_budget,
                        temperature=0.0,
                        response_format="fix",
                        model_profile_id=session.model.profile_id,
                    )
                )
                if proposal.target_file != source.relative_path:
                    raise FixCandidateError("scope_mismatch", "候选目标文件与修复范围不匹配。")
                if proposal.base_sha != source.sha256:
                    raise FixCandidateError("stale_revision", "候选基础 SHA 与当前文件不匹配。")
                if (
                    proposal.start_line != requested_start
                    or proposal.end_line != requested_end
                    or proposal.replacement_mode != requested_mode
                    or proposal.anchor != requested_anchor
                ):
                    raise FixCandidateError(
                        "scope_mismatch", "候选未保留服务端签发的稳定修复锚点。"
                    )
                if custom_user_intent is not None:
                    proposal = proposal.model_copy(
                        update={
                            "explanation": (
                                f"用户确认意图：{custom_user_intent}\n"
                                f"候选生成说明：{proposal.explanation}"
                            )
                        }
                    )
                return proposal
            except FixCandidateError:
                raise
            except ReviewOutputError as error:
                raise FixCandidateError(
                    "model_output_invalid",
                    f"模型输出不是有效的结构化修复：{error}",
                ) from error

        newline = "\r\n" if "\r\n" in source.content else "\n"

        def bound_proposal(replacement: str, explanation: str) -> FixProposal:
            return FixProposal(
                target_file=source.relative_path,
                base_sha=source.sha256,
                start_line=requested_start,
                end_line=requested_end,
                replacement_mode=requested_mode,
                anchor=requested_anchor,
                replacement=replacement,
                explanation=explanation,
            )

        rename_plan = verified.rename_plan or finding.rename_plan
        structured_renamed_content: str | None = None
        if symbol_plan is not None:
            structured_renamed_content = _apply_symbol_repair(source.content, symbol_plan)
            proposal = bound_proposal(
                replacement="".join(
                    structured_renamed_content.splitlines(keepends=True)[fix_start - 1 : fix_end]
                ).rstrip("\r\n"),
                explanation="按 use-def 证据和已确认意图执行结构化符号修复。",
            )
        elif rename_plan is not None:
            if not rename_plan.executable:
                reason = "；".join(rename_plan.unsafe_reasons) or "无法证明所有调用点安全。"
                raise FixCandidateError(
                    "finding_not_actionable",
                    f"该重命名需要人工确认：{reason}",
                )
            rename_adapter = LibCSTRenameAdapter()
            if not rename_adapter.capability.available:
                raise FixCandidateError("finding_not_actionable", rename_adapter.capability.message)
            try:
                renamed_content = rename_adapter.apply(
                    source.content,
                    rename_plan,
                    target_line=fix_start,
                    function_name=rename_plan.definition_symbol.rsplit(".", 1)[-1],
                )
                structured_renamed_content = renamed_content
            except ValueError as error:
                if str(error) == "scope_mismatch":
                    raise FixCandidateError(
                        "scope_mismatch", "结构化重命名未通过范围校验。"
                    ) from error
                if str(error) == "no_effective_diff":
                    raise FixCandidateError(
                        "no_effective_diff", "结构化重命名没有产生有效修改。"
                    ) from error
                raise FixCandidateError(
                    "model_output_invalid", "结构化重命名候选无法验证。"
                ) from error
            renamed_lines = renamed_content.splitlines(keepends=True)
            renamed_tree = ast.parse(renamed_content, filename=source.relative_path)
            matching = [
                node
                for node in ast.walk(renamed_tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == rename_plan.definition_symbol.rsplit(".", 1)[-1]
                and _node_lines(node)[0] == fix_start
            ]
            if len(matching) != 1:
                raise FixCandidateError("scope_mismatch", "重命名后无法唯一定位目标定义。")
            renamed_start, renamed_end = _node_lines(matching[0])
            proposal = bound_proposal(
                replacement=textwrap.dedent(
                    "".join(renamed_lines[renamed_start - 1 : renamed_end])
                ).rstrip("\r\n"),
                explanation="根据已验证的 override 契约执行结构化参数重命名。",
            )
        else:
            proposal = await request_proposal()
        phase = 0
        last_failure: FixCandidateError | SyntaxError | None = None
        revised_content = source.content
        replacement = ""
        while phase <= 2:
            last_failure = None
            try:
                if structured_renamed_content is not None:
                    replacement = proposal.replacement
                    revised_content = structured_renamed_content
                elif replace_whole_file:
                    replacement = _strip_replacement_fence(proposal.replacement)
                    if not replacement.strip():
                        raise FixCandidateError(
                            "model_output_invalid", "模型内容无效：返回了空文件。"
                        )
                    revised_content = replacement + (
                        newline if source.content.endswith(("\n", "\r")) else ""
                    )
                elif python_target is not None:
                    revised_content, replacement = _assemble_python_edit(
                        source.content, python_target, proposal.replacement
                    )
                    fix_start, fix_end = python_target.start_line, python_target.end_line
                else:
                    replacement = _strip_replacement_fence(proposal.replacement)
                    if source.language == "python" and parsed_python:
                        original_indent = source_lines[fix_start - 1][
                            : len(source_lines[fix_start - 1])
                            - len(source_lines[fix_start - 1].lstrip())
                        ]
                        replacement = _normalize_statement_replacement(replacement, original_indent)
                    replacement_for_file = replacement
                    if replacement_for_file and (
                        fix_end < len(source_lines) or source.content.endswith(("\n", "\r"))
                    ):
                        replacement_for_file += newline
                    revised_content = (
                        "".join(source_lines[: fix_start - 1])
                        + replacement_for_file
                        + "".join(source_lines[fix_end:])
                    )
                if revised_content == source.content:
                    raise FixCandidateError(
                        "no_effective_diff", "候选与原代码相同，没有可应用的有效修改。"
                    )
                if source.language == "python":
                    ast.parse(revised_content, filename=source.relative_path)
                    compile(revised_content, source.relative_path, "exec", dont_inherit=True)
                    if python_target is not None and replacement:
                        _validate_assembled_python_target(revised_content, python_target)
            except (FixCandidateError, SyntaxError) as error:
                last_failure = error
            if last_failure is None:
                break
            if structured_renamed_content is not None:
                raise FixCandidateError(
                    "syntax_invalid", "结构化重命名后的完整文件未通过语法校验。"
                ) from last_failure

            failed = proposal.replacement[:12000]
            if isinstance(last_failure, SyntaxError):
                failure_detail = (
                    f"完整文件语法错误：{last_failure.msg}"
                    f"（第 {last_failure.lineno or 1} 行，第 {last_failure.offset or 1} 列）"
                    f"\n真实完整文件 caret：\n{_syntax_error_caret(revised_content, last_failure)}"
                )
            else:
                failure_detail = f"候选结构验证失败：{last_failure}"
            enclosing_context = _python_enclosing_context(
                source.content,
                enclosing_definition,
                requested_start,
                requested_end,
            )
            if phase == 0:
                proposal = await request_proposal(
                    "\n第一次局部候选未通过确定性验证。请只重写 replacement；"
                    "不要决定缩进、mode、span 或锚点。"
                    f"\n服务端目标 AST 类别：{requested_mode}"
                    f"\n{failure_detail}"
                    f"\n原始安全替换范围：第 {requested_start}-{requested_end} 行"
                    f"\n目标节点最小完整上下文：\n{enclosing_context}"
                    f"\n原始失败候选：\n{failed}"
                )
                phase = 1
                continue
            if (
                phase == 1
                and python_target is not None
                and python_target.mode != "definition"
                and python_target.containing_function is not None
            ):
                python_target = _definition_edit_target(
                    source.content, python_target.containing_function
                )
                enclosing_definition = python_target.containing_function
                requested_start, requested_end = python_target.start_line, python_target.end_line
                requested_mode = "definition"
                requested_anchor = (
                    f"sha256:{source.sha256}:lines:{requested_start}-{requested_end}:"
                    f"columns:{python_target.start_column}-{python_target.end_column}:"
                    f"node:{python_target.node_kind}:symbol:{python_target.expected_symbol or '-'}"
                )
                scope_instruction = (
                    "局部候选及一次纠正均失败；服务端现将安全范围升级为包含该问题的完整函数。"
                    "replacement 必须只包含这个完整函数定义；模型不决定缩进、边界或 splice；"
                )
                excerpt = _python_enclosing_context(
                    source.content,
                    enclosing_definition,
                    requested_start,
                    requested_end,
                )
                output_token_budget = self._fix_output_token_budget(
                    "".join(source_lines[requested_start - 1 : requested_end])
                )
                proposal = await request_proposal(
                    "\n这是唯一一次函数级安全恢复。请返回完整包含函数，保持函数名称和类型不变。"
                    f"\n局部失败原因：{failure_detail}"
                    f"\n用户确认的业务意图仍为：{custom_user_intent or '无'}"
                )
                phase = 2
                continue

            if isinstance(last_failure, SyntaxError):
                file_line = last_failure.lineno or requested_start
                file_column = last_failure.offset or 1
                code = (
                    "replacement_indent_invalid"
                    if last_failure.msg == "unexpected indent"
                    else "syntax_invalid"
                )
                message = (
                    f"模型内容在有界纠正后仍无效：完整文件 {source.relative_path} "
                    f"第 {file_line} 行、第 {file_column} 列出现 {last_failure.msg}；原代码未改变。"
                )
                details = {
                    "file": source.relative_path,
                    "file_line": file_line,
                    "file_column": file_column,
                    "parser_message": last_failure.msg,
                    "scope_start_line": requested_start,
                    "scope_end_line": requested_end,
                    "target_mode": requested_mode,
                }
                raise FixCandidateError(code, message, details=details) from last_failure
            raise FixCandidateError(
                last_failure.code,
                f"模型内容在有界纠正后仍无效：{last_failure} 原代码未改变。",
                details={
                    **last_failure.details,
                    "scope_start_line": requested_start,
                    "scope_end_line": requested_end,
                    "target_mode": requested_mode,
                },
            ) from last_failure
        else:
            raise FixCandidateError("syntax_invalid", "候选未通过有界确定性验证。")

        proposal = proposal.model_copy(
            update={
                "explanation": (
                    f"{proposal.explanation}\n安全边界：模型仅提供语义候选内容；"
                    "AST 目标、缩进、替换范围、SHA、静态验证和最终确认均由系统确定性控制。"
                )
            }
        )

        candidate_source = SourceFile.from_content(
            file_id=source.file_id,
            relative_path=source.relative_path,
            language=source.language,
            content=revised_content,
        )
        baseline_analysis = await self._analyzer.analyze([source], external_standard=False)
        candidate_analysis = await self._analyzer.analyze(
            [candidate_source], external_standard=False
        )

        def static_identity(item: Finding) -> tuple[str, str]:
            return item.rule_id, " ".join(item.evidence.casefold().split())

        baseline_identities = {static_identity(item) for item in baseline_analysis.findings}
        introduced = [
            item
            for item in candidate_analysis.findings
            if static_identity(item) not in baseline_identities
            and item.rule_id != "python.syntax-error"
        ]
        if introduced:
            titles = "\uff1b".join(item.title for item in introduced[:3])
            raise FixCandidateError("model_output_invalid", f"候选引入了新的静态诊断：{titles}。")
        target_still_present = any(
            item.rule_id == finding.rule_id
            and (
                finding.rule_id == "python.syntax-error"
                or static_identity(item)[1] == static_identity(finding)[1]
            )
            for item in candidate_analysis.findings
        )
        if finding.source == "static" and target_still_present:
            raise FixCandidateError("no_effective_diff", "候选未消除目标确定性诊断。")

        unified_diff = _unified_source_diff(
            source.content,
            revised_content,
            source.relative_path,
        )
        validation = [
            "候选文件 SHA-256 已计算",
            "静态检查未引入新的诊断",
        ]
        if source.language == "python":
            validation.insert(1, "Python 语法解析通过")
        else:
            validation.insert(1, "C++ 静态/语法检查已完成（未运行用户程序）")
        now = datetime.now(tz=UTC)
        candidate = FixCandidate(
            candidate_id=f"fix-{uuid.uuid4().hex}",
            review_id=review_id,
            finding_id=finding_id,
            file_id=source.file_id,
            relative_path=source.relative_path,
            created_at=now,
            expires_at=now + timedelta(minutes=_FIX_CANDIDATE_TTL_MINUTES),
            base_sha256=source.sha256,
            after_sha256=candidate_source.sha256,
            diff=unified_diff,
            explanation=proposal.explanation,
            validation=validation,
            output_token_budget=output_token_budget,
            fix_safety=(symbol_plan.safety if symbol_plan is not None else "requires_review"),
        )
        node_kind = python_target.node_kind if python_target is not None else "stmt"
        expected_symbol = python_target.expected_symbol if python_target is not None else None
        scope = RepairScope(
            file_sha=source.sha256,
            node_kind=node_kind,
            start_line=fix_start,
            end_line=fix_end,
            expected_symbol=expected_symbol,
            replacement_mode=requested_mode,
        )
        self._store_fix_candidate(
            _PreparedFix(
                owner_id=owner_id,
                candidate=candidate,
                revised_content=revised_content,
                plan=ReplacementPlan(
                    scope=scope,
                    replacement_sha=hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
                ),
                fix_start=fix_start,
                fix_end=fix_end,
                replacement_line_count=len(replacement.rstrip("\r\n").splitlines()),
            )
        )
        return candidate

    async def cancel_fix(self, review_id: str, owner_id: str, candidate_id: str) -> None:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        self._cleanup_fix_candidates()
        prepared = self._fix_candidates.get(candidate_id)
        if (
            prepared is None
            or prepared.candidate.review_id != review_id
            or prepared.owner_id != owner_id
        ):
            raise LookupError(candidate_id)
        self._fix_candidates.pop(candidate_id, None)

    async def confirm_fix(
        self,
        review_id: str,
        owner_id: str,
        candidate_id: str,
    ) -> tuple[ReviewSession, ReviewSession | None]:
        lock = self._fix_locks.setdefault(review_id, asyncio.Lock())
        async with lock:
            return await self._confirm_fix_locked(review_id, owner_id, candidate_id)

    async def _confirm_fix_locked(
        self, review_id: str, owner_id: str, candidate_id: str
    ) -> tuple[ReviewSession, ReviewSession | None]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        self._cleanup_fix_candidates()
        prepared = self._fix_candidates.pop(candidate_id, None)
        if (
            prepared is None
            or prepared.candidate.review_id != review_id
            or prepared.owner_id != owner_id
        ):
            raise LookupError(candidate_id)
        candidate = prepared.candidate
        if candidate.expires_at <= datetime.now(tz=UTC):
            raise FixCandidateError("stale_revision", "修复候选已过期，请重新生成。")
        source = next((item for item in session.files if item.file_id == candidate.file_id), None)
        if source is None:
            raise FixCandidateError("stale_revision", "修复候选对应的文件已不存在。")
        if source.sha256 != candidate.base_sha256:
            raise FixCandidateError("stale_revision", "当前文件 SHA 已变化，请重新生成候选。")
        if source.sha256 != prepared.plan.scope.file_sha:
            raise FixCandidateError("stale_revision", "修复范围对应的文件 SHA 已变化。")
        if source.language == "python" and prepared.plan.scope.expected_symbol:
            tree = parse_python_content(source.content)
            matching = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == prepared.plan.scope.expected_symbol
                and _node_lines(node)
                == (prepared.plan.scope.start_line, prepared.plan.scope.end_line)
            ]
            if len(matching) != 1:
                raise FixCandidateError("stale_revision", "修复范围的 AST 节点身份已变化。")
        finding = next(
            (item for item in session.findings if item.finding_id == candidate.finding_id),
            session.decided_findings.get(candidate.finding_id),
        )
        if finding is None:
            raise FixCandidateError(
                "stale_revision", "目标问题已变化，修复候选未应用，请重新生成。"
            )

        revised_files = [
            SourceFile.from_content(
                file_id=item.file_id,
                relative_path=item.relative_path,
                language=item.language,
                content=prepared.revised_content
                if item.file_id == source.file_id
                else item.content,
            )
            for item in session.files
        ]
        revised_source = next(item for item in revised_files if item.file_id == source.file_id)
        baseline_post_apply = await self._analyzer.analyze([source])
        revised_post_apply = await self._analyzer.analyze([revised_source])

        def post_apply_identity(item: Finding) -> tuple[str, str]:
            return item.rule_id, " ".join(item.evidence.casefold().split())

        baseline_post_identities = {
            post_apply_identity(item) for item in baseline_post_apply.findings
        }
        introduced_post_apply = [
            item
            for item in revised_post_apply.findings
            if post_apply_identity(item) not in baseline_post_identities
        ]
        if introduced_post_apply:
            titles = "；".join(item.title for item in introduced_post_apply[:3])
            raise FixCandidateError(
                "model_output_invalid",
                f"确认前的受控静态复核发现新诊断：{titles}。",
            )
        post_apply_validation = [
            *candidate.validation,
            "确认前已复核当前 revision 并对受影响文件运行有界静态检查。",
        ]
        replacement_line_count = prepared.replacement_line_count
        fix_start, fix_end = prepared.fix_start, prepared.fix_end
        line_delta = replacement_line_count - (fix_end - fix_start + 1)
        remaining_findings: list[Finding] = []
        for item in session.findings:
            if item.finding_id == candidate.finding_id:
                continue
            if item.file_id != finding.file_id or item.end_line < fix_start:
                remaining_findings.append(item)
                continue
            if item.start_line > fix_end:
                remaining_findings.append(
                    item.model_copy(
                        update={
                            "start_line": item.start_line + line_delta,
                            "end_line": item.end_line + line_delta,
                        }
                    )
                )
            # Overlapping findings are stale and will be checked in the batch re-review.

        before_changed_ranges = None
        revised_origin = session.origin
        if (
            revised_origin is not None
            and revised_origin.changed_ranges is not None
            and source.relative_path in revised_origin.changed_ranges
        ):
            before_changed_ranges = list(revised_origin.changed_ranges[source.relative_path])
            adjusted_ranges = []
            replacement_end = fix_start + max(1, replacement_line_count) - 1
            for changed in before_changed_ranges:
                if changed.end_line < fix_start:
                    adjusted_ranges.append(changed)
                elif changed.start_line > fix_end:
                    adjusted_ranges.append(
                        changed.model_copy(
                            update={
                                "start_line": changed.start_line + line_delta,
                                "end_line": changed.end_line + line_delta,
                            }
                        )
                    )
                else:
                    adjusted_ranges.append(
                        changed.model_copy(
                            update={
                                "start_line": min(changed.start_line, fix_start),
                                "end_line": max(replacement_end, changed.end_line + line_delta),
                            }
                        )
                    )
            changed_ranges = dict(revised_origin.changed_ranges)
            changed_ranges[source.relative_path] = adjusted_ranges
            revised_origin = revised_origin.model_copy(update={"changed_ranges": changed_ranges})
        revision = ReviewRevision(
            revision_id=f"revision-{uuid.uuid4().hex}",
            finding_id=candidate.finding_id,
            file_id=source.file_id,
            relative_path=source.relative_path,
            created_at=datetime.now(tz=UTC),
            before_content=source.content,
            before_changed_ranges=before_changed_ranges,
            before_sha256=source.sha256,
            after_sha256=next(
                item.sha256 for item in revised_files if item.file_id == source.file_id
            ),
            diff=candidate.diff,
            explanation=candidate.explanation,
            validation=post_apply_validation,
        )
        decisions = dict(session.finding_decisions)
        decisions[candidate.finding_id] = "fixed"
        states = dict(session.finding_states)
        states[candidate.finding_id] = "fixed_pending_revalidation"
        ignored = set(session.ignored_finding_fingerprints)
        ignored.difference_update(finding_fingerprints(finding, session.files))
        decided_findings = dict(session.decided_findings)
        decided_findings[candidate.finding_id] = finding
        decision_history = [
            *session.finding_decision_history,
            FindingDecisionAudit(
                finding_id=candidate.finding_id,
                action="decided",
                decision="fixed",
                reason=candidate.explanation,
            ),
        ][-200:]
        decided = session.model_copy(
            update={
                "finding_decisions": decisions,
                "finding_states": states,
                "decided_findings": decided_findings,
                "finding_decision_history": decision_history,
                "ignored_finding_fingerprints": sorted(ignored),
                "files": revised_files,
                "origin": revised_origin,
                "findings": remaining_findings,
                "summary": _finding_summary(remaining_findings),
                "revisions": [*session.revisions, revision][-_MAX_REVISIONS:],
            }
        )
        await self._store.save(decided)
        if remaining_findings:
            return decided, None

        revised = await self._restart_review_in_place(decided)
        return decided, revised

    async def preview_followup_fix(
        self,
        review_id: str,
        owner_id: str,
        instruction: str,
        base_sha: str,
        context: dict[str, object],
    ) -> FixCandidate:
        if context.get("kind") != "finding":
            raise FixCandidateError(
                "scope_mismatch", "只有审查问题上下文可以生成受约束的修改候选。"
            )
        finding_id = context.get("finding_id")
        file_id = context.get("file_id")
        if not isinstance(finding_id, str) or not isinstance(file_id, str):
            raise FixCandidateError("scope_mismatch", "追问修复上下文缺少问题或文件标识。")
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        finding = next(
            (item for item in session.findings if item.finding_id == finding_id),
            session.decided_findings.get(finding_id),
        )
        if finding is None:
            raise LookupError(finding_id)
        if finding.file_id != file_id:
            raise FixCandidateError("scope_mismatch", "追问上下文与问题所在文件不匹配。")
        source = next((item for item in session.files if item.file_id == file_id), None)
        if source is None:
            raise FixCandidateError("scope_mismatch", "追问上下文中的文件不存在。")
        if source.sha256 != base_sha:
            raise FixCandidateError("stale_revision", "文件版本已经变化，请重新打开追问。")
        return await self.preview_fix(
            review_id,
            owner_id,
            finding_id,
            followup_instruction=instruction,
            expected_base_sha=base_sha,
        )

    async def followups(
        self,
        review_id: str,
        owner_id: str,
        context: dict[str, object] | None = None,
    ) -> list[FollowupMessage]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        context_key, _ = _followup_code_context(session, context)
        return await self._store.followups(review_id, owner_id, context_key)

    async def ask_followup(
        self,
        review_id: str,
        owner_id: str,
        question: str,
        context: dict[str, object] | None = None,
    ) -> list[FollowupMessage]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status != "completed":
            raise ValueError("只有已完成的审查可以继续追问")
        context_key, code_context = _followup_code_context(session, context)
        previous = await self._store.followups(review_id, owner_id, context_key)
        history_lines = [
            f"{'用户' if item.role == 'user' else '助手'}：{item.content}" for item in previous[-8:]
        ]
        previous_context = "\n".join(history_lines)
        if len(previous_context) > 6000:
            previous_context = "…较早内容已省略…\n" + previous_context[-6000:]
        prompt = (
            f"当前代码上下文：\n{code_context}\n"
            f"审查摘要：{session.summary.text}\n"
            f"当前上下文的此前追问：\n{previous_context or '无'}\n"
            f"本次问题：{question.strip()}"
        )
        request_id = f"{review_id}:followup:{uuid.uuid4().hex}"
        answer_text = await self._inference_service.answer_followup(
            InferenceRequest(
                request_id=request_id,
                messages=[
                    ChatMessage(
                        role="system",
                        content=(
                            "你是代码审查追问助手。仅根据当前代码上下文、审查摘要"
                            "和当前上下文的对话历史回答。不得虚构代码事实；使用简洁、可读的中文。"
                        ),
                    ),
                    ChatMessage(role="user", content=prompt),
                ],
                max_output_tokens=4096,
                temperature=0.1,
                response_format="text",
                model_profile_id=session.model.profile_id,
            )
        )
        created_at = datetime.now(tz=UTC)
        exchange_id = uuid.uuid4().hex
        user_message = FollowupMessage(
            message_id=f"followup-question-{exchange_id}",
            review_id=review_id,
            role="user",
            context_key=context_key,
            content=question.strip(),
            created_at=created_at,
        )
        assistant_message = FollowupMessage(
            message_id=f"followup-answer-{exchange_id}",
            review_id=review_id,
            role="assistant",
            context_key=context_key,
            content=answer_text,
            created_at=created_at + timedelta(microseconds=1),
        )
        await self._store.append_followup_exchange(
            user_message, assistant_message, owner_id, context_key
        )
        return [user_message, assistant_message]

    async def start(self, review_id: str, owner_id: str) -> None:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        existing = self._tasks.get(review_id)
        if existing is not None and not existing.done():
            return
        recheck_attempt_id: str | None = None
        if _has_pending_revalidation(session):
            recheck_attempt_id = session.recheck_attempt_id
            if (
                session.recheck_attempt_status != "running"
                or recheck_attempt_id is None
                or session.recheck_deadline_at is None
            ):
                recheck_attempt_id = f"recheck-{uuid.uuid4().hex}"
                session = session.model_copy(
                    update={
                        "recheck_attempt_id": recheck_attempt_id,
                        "recheck_attempt_status": "running",
                        "recheck_deadline_at": datetime.now(tz=UTC)
                        + timedelta(seconds=_REVALIDATION_BATCH_TIMEOUT_SECONDS),
                    }
                )
                await self._store.save(session)
            elif session.recheck_deadline_at <= datetime.now(tz=UTC):
                await self._commit_recheck_failure(
                    review_id,
                    owner_id,
                    recheck_attempt_id,
                    code="revalidation_timeout",
                    status="timed_out",
                    message="修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
                )
                return
        operation = (
            self._run_revalidation_with_deadline(
                review_id, owner_id, recheck_attempt_id
            )
            if recheck_attempt_id is not None
            else self.run(review_id, owner_id)
        )
        task = asyncio.create_task(operation)
        self._tasks[review_id] = task

        def cleanup(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(review_id) is completed:
                self._tasks.pop(review_id, None)

        task.add_done_callback(cleanup)

    async def _run_revalidation_with_deadline(
        self,
        review_id: str,
        owner_id: str,
        attempt_id: str,
    ) -> None:
        current = await self._store.get(review_id, owner_id)
        timeout_seconds = float(_REVALIDATION_BATCH_TIMEOUT_SECONDS)
        if current is not None and current.recheck_deadline_at is not None:
            timeout_seconds = max(
                0.0,
                (current.recheck_deadline_at - datetime.now(tz=UTC)).total_seconds(),
            )
        pipeline = asyncio.create_task(
            self.run(
                review_id,
                owner_id,
                recheck_attempt_id=attempt_id,
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(pipeline),
                timeout=timeout_seconds,
            )
            return
        except TimeoutError:
            await self._commit_recheck_failure(
                review_id,
                owner_id,
                attempt_id,
                code="revalidation_timeout",
                status="timed_out",
                message="修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
            )
            pipeline.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(pipeline),
                    timeout=_REVALIDATION_CANCEL_GRACE_SECONDS,
                )
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                self._orphaned_recheck_tasks.add(pipeline)
                self._orphaned_rechecks[review_id] = pipeline

                def cleanup_orphan(completed: asyncio.Task[None]) -> None:
                    self._orphaned_recheck_tasks.discard(completed)
                    if self._orphaned_rechecks.get(review_id) is completed:
                        self._orphaned_rechecks.pop(review_id, None)
                    with suppress(asyncio.CancelledError, Exception):
                        completed.result()

                pipeline.add_done_callback(cleanup_orphan)
            except Exception:
                pass
        except asyncio.CancelledError:
            await self._commit_recheck_failure(
                review_id,
                owner_id,
                attempt_id,
                code="revalidation_cancelled",
                status="failed",
                message="修复已应用并保留；统一复查已中断，可重新复查。",
            )
            if not pipeline.done():
                pipeline.cancel()
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(pipeline),
                        timeout=_REVALIDATION_CANCEL_GRACE_SECONDS,
                    )
            raise

    async def _commit_recheck_failure(
        self,
        review_id: str,
        owner_id: str,
        attempt_id: str,
        *,
        code: str,
        status: Literal["failed", "timed_out"],
        message: str,
    ) -> bool:
        current = await self._store.get(review_id, owner_id)
        if current is None:
            return False
        failed = current.model_copy(
            update={
                "status": "failed",
                "error": message,
                "error_code": code,
                "summary": current.summary.model_copy(update={"text": message}),
                "recheck_attempt_status": status,
            }
        )
        return await self._store.transition_review_if_recheck_attempt(
            failed,
            attempt_id,
            "error",
            {
                "code": code,
                "message": message,
                "retryable": True,
                "terminal": True,
                "recheck_attempt_id": attempt_id,
            },
        )

    async def _transition_run_review(
        self,
        session: ReviewSession,
        event: str,
        data: dict[str, object],
        attempt_id: str | None,
    ) -> None:
        if attempt_id is None:
            await self._store.transition_review(session, event, data)
            return
        if not await self._store.transition_review_if_recheck_attempt(
            session, attempt_id, event, data
        ):
            raise asyncio.CancelledError

    async def _save_run_review(
        self, session: ReviewSession, attempt_id: str | None
    ) -> None:
        if attempt_id is None:
            await self._store.save(session)
            return
        if not await self._store.save_review_if_recheck_attempt(session, attempt_id):
            raise asyncio.CancelledError

    async def run(
        self,
        review_id: str,
        owner_id: str,
        *,
        recheck_attempt_id: str | None = None,
    ) -> None:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        owner_id = session.owner_id
        if session.status in {"completed", "cancelled"}:
            return
        executor = self._executors.get(session.model.profile_id, self._executor)
        context_token = current_recheck_attempt.set(recheck_attempt_id)
        try:
            planning = session.model_copy(
                update={"status": "planning", "error": None, "error_code": None}
            )
            await self._transition_run_review(
                planning,
                "stage",
                {
                    "stage": "planning",
                    "progress": 5,
                    "message": "正在规划完整审查范围",
                },
                recheck_attempt_id,
            )
            static_result = await self._analyzer.analyze(planning.files)
            ignored = set(planning.ignored_finding_fingerprints)
            scoped_static_findings = [
                finding
                for finding in static_result.findings
                if _finding_in_review_scope(finding, planning)
            ]
            visible_static_findings = [
                finding
                for finding in scoped_static_findings
                if ignored.isdisjoint(finding_fingerprints(finding, planning.files))
            ]
            planning_states = dict(planning.finding_states)
            for finding in visible_static_findings:
                planning_states.setdefault(finding.finding_id, "active")
            planning = planning.model_copy(
                update={
                    "findings": visible_static_findings,
                    "coverage": static_result.coverage,
                    "finding_states": planning_states,
                }
            )
            await self._save_run_review(planning, recheck_attempt_id)
            syntax_blockers = [
                finding
                for finding in scoped_static_findings
                if finding.rule_id == "python.syntax-error"
            ]
            visible_syntax_blockers = [
                finding
                for finding in visible_static_findings
                if finding.rule_id == "python.syntax-error"
            ]
            if syntax_blockers:
                for finding in visible_static_findings:
                    await self._store.publish(
                        review_id,
                        owner_id,
                        "finding",
                        finding.model_dump(mode="json"),
                    )
                summary = _finding_summary(visible_static_findings, completed=True)
                if visible_syntax_blockers:
                    summary_text = (
                        f"语法检查发现 {len(visible_syntax_blockers)} 个阻断问题；"
                        "请先修复 Python 语法，再进行完整模型审查。"
                    )
                else:
                    summary_text = (
                        "已选择暂不处理该文件的 Python 语法问题；代码仍无法解析，已跳过模型审查。"
                    )
                summary = summary.model_copy(update={"text": summary_text})
                completed = planning.model_copy(
                    update={
                        "status": "completed",
                        "findings": visible_static_findings,
                        "summary": summary,
                        "error": None,
                        "error_code": None,
                    }
                )
                completed = completed.model_copy(
                    update={"recheck_attempt_status": "completed"}
                ) if recheck_attempt_id is not None else completed
                await self._transition_run_review(
                    completed,
                    "complete",
                    {
                        "review_id": review_id,
                        "summary": summary.model_dump(mode="json"),
                        "coverage_percent": 100.0,
                        "semantic_review_deferred": True,
                    },
                    recheck_attempt_id,
                )
                return

            existing_chunks = await self._store.chunks(review_id, owner_id)
            if existing_chunks:
                chunks = existing_chunks
                significant = {item.file_id: significant_lines(item) for item in planning.files}
            else:
                plan = self._planner.plan(review_id, planning.files)
                chunks = plan.chunks
                significant = plan.significant_lines
                await self._store.save_chunks(chunks, owner_id)
            for finding in visible_static_findings:
                await self._store.publish(
                    review_id,
                    owner_id,
                    "finding",
                    finding.model_dump(mode="json"),
                )

            reviewing = planning.model_copy(update={"status": "reviewing"})
            await self._transition_run_review(
                reviewing,
                "stage",
                {
                    "stage": "reviewing",
                    "progress": 15,
                    "message": "正在分块进行语义审查",
                },
                recheck_attempt_id,
            )
            pending = [item for item in chunks if item.status == ChunkStatus.PENDING]
            while pending:
                batch = pending[: self._settings.review_queue_limit]
                execution = asyncio.gather(
                    *(
                        executor.execute(
                            item,
                            reviewing.files,
                            visible_static_findings,
                            owner_id=owner_id,
                            model_profile_id=reviewing.model.profile_id,
                            model_name=reviewing.model.model,
                        )
                        for item in batch
                    )
                )
                batch_timeout = _semantic_batch_timeout(
                    reviewing,
                    self._settings.request_timeout_seconds,
                )
                results = (
                    await execution
                    if batch_timeout is None
                    else await asyncio.wait_for(execution, timeout=batch_timeout)
                )
                children = [child for result in results for child in result]
                current_chunks = await self._store.chunks(review_id, owner_id)
                queued_ids = {item.chunk_id for item in children}
                pending = children + [
                    item
                    for item in current_chunks
                    if item.status == ChunkStatus.PENDING and item.chunk_id not in queued_ids
                ]
                await self._publish_progress(review_id, owner_id, current_chunks)

            current_chunks = await self._store.chunks(review_id, owner_id)
            aggregating = reviewing.model_copy(update={"status": "aggregating"})
            await self._transition_run_review(
                aggregating,
                "stage",
                {
                    "stage": "aggregating",
                    "progress": 95,
                    "message": "正在校验覆盖率并合并问题",
                },
                recheck_attempt_id,
            )
            result = self._aggregator.aggregate(
                aggregating,
                current_chunks,
                static_findings=visible_static_findings,
                chunk_findings=await self._store.chunk_findings(review_id, owner_id),
                significant_lines=significant,
            )
            ignored = set(aggregating.ignored_finding_fingerprints)
            visible_findings = [
                finding
                for finding in result.findings
                if _finding_in_review_scope(finding, aggregating)
                and ignored.isdisjoint(finding_fingerprints(finding, aggregating.files))
            ]
            completed_summary = result.summary
            if len(visible_findings) != len(result.findings):
                completed_summary = _finding_summary(visible_findings, completed=True)
            states = dict(aggregating.finding_states)
            visible_fingerprints = {
                fingerprint
                for finding in visible_findings
                for fingerprint in finding_fingerprints(finding, aggregating.files)
            }
            fixed_fingerprints = {
                fingerprint
                for finding_id, decision in aggregating.finding_decisions.items()
                if decision == "fixed"
                for previous in [aggregating.decided_findings.get(finding_id)]
                if previous is not None
                for fingerprint in finding_fingerprints(previous, aggregating.files)
            }
            for finding in visible_findings:
                states[finding.finding_id] = (
                    "reopened"
                    if not fixed_fingerprints.isdisjoint(
                        finding_fingerprints(finding, aggregating.files)
                    )
                    else "active"
                )
            for finding_id, decision in aggregating.finding_decisions.items():
                if decision != "fixed":
                    continue
                previous = aggregating.decided_findings.get(finding_id)
                if previous is None:
                    continue
                states[finding_id] = (
                    "reopened"
                    if not visible_fingerprints.isdisjoint(
                        finding_fingerprints(previous, aggregating.files)
                    )
                    else "fixed_verified"
                )
            completed = aggregating.model_copy(
                update={
                    "status": "completed",
                    "findings": visible_findings,
                    "summary": completed_summary,
                    "error": None,
                    "error_code": None,
                    "finding_states": states,
                    "recheck_attempt_status": (
                        "completed" if recheck_attempt_id is not None else None
                    ),
                }
            )
            await self._transition_run_review(
                completed,
                "complete",
                {
                    "review_id": review_id,
                    "summary": completed.summary.model_dump(mode="json"),
                    "coverage_percent": result.coverage_percent,
                },
                recheck_attempt_id,
            )
        except asyncio.CancelledError:
            if self._shutting_down:
                raise
            if recheck_attempt_id is not None:
                raise
            current = await self._store.get(review_id, owner_id)
            if current is not None and current.status != "cancelled":
                cancelled = current.model_copy(update={"status": "cancelled"})
                await self._transition_run_review(
                    cancelled,
                    "cancelled",
                    {"review_id": review_id},
                    recheck_attempt_id,
                )
            raise
        except Exception as error:
            current = await self._store.get(review_id, owner_id)
            if current is not None:
                chunks = await self._store.chunks(review_id, owner_id)
                overflow = any(str(item.error_code) == "context_overflow" for item in chunks)
                local_failure_codes = {
                    str(item.error_code)
                    for item in chunks
                    if item.error_code is not None
                    and str(item.error_code).startswith("local_model_")
                }
                local_failure_code = next(
                    (
                        code
                        for code in (
                            "local_model_circuit_open",
                            "local_model_connection_refused",
                            "local_model_timeout",
                        )
                        if code in local_failure_codes
                    ),
                    None,
                )
                if overflow:
                    message = "输入代码过长，超出模型上下文范围，请减少文件或分批审查。"
                    retryable = False
                elif isinstance(error, ReviewPlanningError) and error.code != "coverage_incomplete":
                    message = f"无法规划代码审查：{error}"
                    retryable = False
                elif local_failure_code is not None:
                    revalidation_pending = _has_pending_revalidation(current)
                    message = (
                        "修复已应用并保留；本地模型服务未运行或正在恢复，可重新复查。"
                        if revalidation_pending
                        else (
                            "本地模型服务未运行或正在恢复。"
                            "已创建的审查和完成分块已保留，可重新复查。"
                        )
                    )
                    retryable = True
                else:
                    revalidation_pending = _has_pending_revalidation(current)
                    message = (
                        "修复已应用并保留；统一复查未完成，可在模型恢复后重新复查。"
                        if revalidation_pending
                        else "模型审查失败，已保留完成分块，可继续未完成审查。"
                    )
                    retryable = True
                code = local_failure_code or (
                    error.code if isinstance(error, ReviewPlanningError) else "review_failed"
                )
                summary = current.summary
                if _has_pending_revalidation(current):
                    summary = summary.model_copy(update={"text": message})
                failed = current.model_copy(
                    update={
                        "status": "failed",
                        "error": message,
                        "error_code": code,
                        "summary": summary,
                        "recheck_attempt_status": (
                            "failed" if recheck_attempt_id is not None else None
                        ),
                    }
                )
                await self._transition_run_review(
                    failed,
                    "error",
                    {
                        "code": code,
                        "message": message,
                        "retryable": retryable,
                    },
                    recheck_attempt_id,
                )
        finally:
            current_recheck_attempt.reset(context_token)

    async def _publish_progress(
        self,
        review_id: str,
        owner_id: str,
        chunks: list[ReviewChunk],
    ) -> None:
        leaves = [item for item in chunks if item.status != ChunkStatus.SUPERSEDED]
        completed = sum(item.status == ChunkStatus.COMPLETED for item in leaves)
        failed = sum(item.status == ChunkStatus.FAILED for item in leaves)
        running = sum(
            item.status in {ChunkStatus.RUNNING, ChunkStatus.VALIDATING} for item in leaves
        )
        queued = sum(item.status in {ChunkStatus.PENDING, ChunkStatus.QUEUED} for item in leaves)
        coverage = 100.0 if not leaves else completed * 100.0 / len(leaves)
        await self._store.publish(
            review_id,
            owner_id,
            "progress",
            {
                "total": len(leaves),
                "completed": completed,
                "failed": failed,
                "running": running,
                "queued": queued,
                "coverage_percent": coverage,
            },
        )

    async def resume(self, review_id: str, owner_id: str) -> bool:
        lock = self._fix_locks.setdefault(review_id, asyncio.Lock())
        async with lock:
            orphan = getattr(self, "_orphaned_rechecks", {}).get(review_id)
            if orphan is not None and not orphan.done():
                raise RecheckCleanupInProgressError(
                    "上一轮统一复查仍在安全清理，请稍后再次点击重新复查。"
                )
            session = await self._store.get(review_id, owner_id)
            if session is None:
                return False
            if session.status == "completed":
                return True
            if session.status in {"queued", "planning", "reviewing", "aggregating"}:
                return True
            if session.status != "failed":
                return False
            chunks = await self._store.chunks(review_id, owner_id)
            reset = [
                item.model_copy(
                    update={
                        "status": ChunkStatus.PENDING,
                        "error_code": None,
                        "error_message": None,
                    }
                )
                if item.status
                in {
                    ChunkStatus.FAILED,
                    ChunkStatus.RUNNING,
                    ChunkStatus.VALIDATING,
                    ChunkStatus.QUEUED,
                }
                else item
                for item in chunks
            ]
            await self._store.save_chunks(reset, owner_id)
            revalidation_pending = _has_pending_revalidation(session)
            summary = session.summary
            if revalidation_pending:
                summary = summary.model_copy(
                    update={"text": "修复已保留，正在重新复查修改后的代码。"}
                )
            await self._store.save(
                session.model_copy(
                    update={
                        "status": "queued",
                        "error": None,
                        "error_code": None,
                        "summary": summary,
                        "recheck_attempt_id": (
                            f"recheck-{uuid.uuid4().hex}"
                            if revalidation_pending
                            else None
                        ),
                        "recheck_attempt_status": (
                            "running" if revalidation_pending else None
                        ),
                        "recheck_deadline_at": (
                            datetime.now(tz=UTC)
                            + timedelta(seconds=_REVALIDATION_BATCH_TIMEOUT_SECONDS)
                            if revalidation_pending
                            else None
                        ),
                    }
                )
            )
            await self.start(review_id, owner_id)
            return True

    async def recover(self) -> list[str]:
        recovered: list[str] = []
        for review_id, owner_id in await self._store.recoverable_reviews():
            await self.start(review_id, owner_id)
            recovered.append(review_id)
        return recovered

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        tasks.extend(
            task for task in self._orphaned_recheck_tasks if not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._orphaned_recheck_tasks.clear()
        self._orphaned_rechecks.clear()

    async def cancel(self, review_id: str, owner_id: str) -> bool:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            return False
        if session.status in {"completed", "failed", "cancelled"}:
            return True
        task = self._tasks.get(review_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        current = await self._store.get(review_id, owner_id)
        if current is not None and current.status != "cancelled":
            cancelled = current.model_copy(update={"status": "cancelled"})
            await self._store.transition_review(
                cancelled,
                "cancelled",
                {"review_id": review_id},
            )
        return True

    async def events(
        self,
        review_id: str,
        owner_id: str,
        after_sequence: int = 0,
    ) -> AsyncIterator[ReviewEvent]:
        sequence = after_sequence
        while True:
            session = await self._store.get(review_id, owner_id)
            if session is None:
                return
            events = await self._store.events_after(review_id, owner_id, sequence)
            for event in events:
                sequence = event.sequence
                yield event
            if session.status in {"completed", "cancelled", "failed"} and not events:
                return
            await asyncio.sleep(0.05)

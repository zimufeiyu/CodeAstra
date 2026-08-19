from __future__ import annotations

import ast
import asyncio
import builtins
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_review.domain.review_models import CoverageState, Finding, FindingVerification, SourceFile


@dataclass(frozen=True)
class AnalyzerResult:
    findings: list[Finding]
    coverage: list[CoverageState]


class StaticAnalyzer:
    async def analyze(self, files: list[SourceFile]) -> AnalyzerResult:
        findings: list[Finding] = []
        coverage: list[CoverageState] = []
        by_language: dict[str, list[SourceFile]] = {}
        for source in files:
            by_language.setdefault(source.language, []).append(source)

        for language, language_files in by_language.items():
            if language == "python":
                for source in language_files:
                    findings.extend(self._analyze_python(source))
                coverage.append(
                    CoverageState(
                        language="python",
                        analyzer="python-ast",
                        available=True,
                        message="已完成 Python 语法、名称和导入检查。",
                    )
                )
            elif language == "cpp":
                cpp_findings, state = await asyncio.to_thread(
                    self._analyze_cpp,
                    language_files,
                )
                findings.extend(cpp_findings)
                coverage.append(state)

        return AnalyzerResult(findings=findings, coverage=coverage)

    def _analyze_python(self, source: SourceFile) -> list[Finding]:
        try:
            tree = ast.parse(source.content, filename=source.relative_path)
        except SyntaxError as error:
            line = max(1, error.lineno or 1)
            column = max(1, error.offset or 1)
            evidence = self._line(source, line).strip() or "语法错误"
            return [
                self._finding(
                    source=source,
                    rule_id="python.syntax-error",
                    category="syntax",
                    severity="medium",
                    line=line,
                    column=column,
                    end_column=column + 1,
                    title="Python 语法错误",
                    evidence=evidence,
                    impact="代码无法被 Python 解释器正常加载或运行。",
                    suggestion=(f"修复第 {line} 行第 {column} 列的 Python 语法错误：{error.msg}。"),
                    analyzer="python-ast",
                )
            ]

        findings: list[Finding] = []
        imported: dict[str, ast.alias] = {}
        loaded = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        defined = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param))
        }
        defined.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        defined.update(
            argument.arg
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            for argument in (
                [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
        )

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported[alias.asname or alias.name.split(".", 1)[0]] = alias

        for name, alias in imported.items():
            if name in loaded:
                continue
            findings.append(
                self._finding(
                    source=source,
                    rule_id="python.unused-import",
                    category="code_quality",
                    severity="info",
                    line=alias.lineno,
                    column=alias.col_offset + 1,
                    end_column=(alias.end_col_offset or alias.col_offset + len(name)) + 1,
                    title=f"未使用的导入：{name}",
                    evidence=name,
                    impact="无效导入会增加阅读成本，并可能引入不必要的初始化开销。",
                    suggestion=f"如果其他位置确实没有使用 {name}，请删除该导入。",
                    analyzer="python-ast",
                )
            )

        known = set(dir(builtins)) | defined | set(imported)
        seen_undefined: set[tuple[str, int, int]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            key = (node.id, node.lineno, node.col_offset)
            if node.id in known or key in seen_undefined:
                continue
            seen_undefined.add(key)
            findings.append(
                self._finding(
                    source=source,
                    rule_id="python.undefined-name",
                    category="correctness",
                    severity="medium",
                    line=node.lineno,
                    column=node.col_offset + 1,
                    end_column=(node.end_col_offset or node.col_offset + len(node.id)) + 1,
                    title=f"名称未定义：{node.id}",
                    evidence=node.id,
                    impact="执行到该位置时会触发 NameError。",
                    suggestion="修正名称拼写，或在使用前完成定义和导入。",
                    analyzer="python-ast",
                )
            )

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec"}
            ):
                continue
            evidence = ast.get_source_segment(source.content, node) or node.func.id
            findings.append(
                self._finding(
                    source=source,
                    rule_id=f"python.dangerous-{node.func.id}",
                    category="security",
                    severity="high",
                    line=node.lineno,
                    column=node.col_offset + 1,
                    end_column=(node.end_col_offset or node.col_offset + len(evidence)) + 1,
                    title=f"危险的 {node.func.id} 调用",
                    evidence=evidence,
                    impact="不可信输入可能被解释为 Python 代码并执行。",
                    suggestion="使用安全解析器、显式数据结构或白名单映射。",
                    analyzer="python-ast",
                )
            )
        return findings

    def _analyze_cpp(self, files: list[SourceFile]) -> tuple[list[Finding], CoverageState]:
        compiler = shutil.which("g++")
        if compiler is None:
            return [], CoverageState(
                language="cpp",
                analyzer="g++",
                available=False,
                message="g++ 静态检查不可用，C++ 结果存在覆盖缺口。",
            )
        findings: list[Finding] = []
        with tempfile.TemporaryDirectory(prefix="code-review-cpp-") as directory:
            root = Path(directory)
            for source in files:
                target = root / Path(source.relative_path).name
                target.write_text(source.content, encoding="utf-8")
                result = subprocess.run(
                    [compiler, "-fsyntax-only", "-Wall", "-Wextra", "-fanalyzer", str(target)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                findings.extend(self._parse_cpp_diagnostics(source, result.stderr))
        return findings, CoverageState(
            language="cpp",
            analyzer="g++",
            available=True,
            message="已完成 g++ 语法、警告和静态分析检查。",
        )

    def _parse_cpp_diagnostics(self, source: SourceFile, stderr: str) -> list[Finding]:
        findings: list[Finding] = []
        pattern = re.compile(r"^.+?:(\d+):(\d+):\s+(error|warning):\s+(.+)$")
        for line in stderr.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            line_number, column, level, message = match.groups()
            severity = "medium" if level == "error" else "low"
            evidence = self._line(source, int(line_number)).strip() or message
            findings.append(
                self._finding(
                    source=source,
                    rule_id="cpp.compiler-error" if level == "error" else "cpp.compiler-warning",
                    category="correctness",
                    severity=severity,
                    line=int(line_number),
                    column=int(column),
                    end_column=int(column) + 1,
                    title="C++ 编译错误" if level == "error" else "C++ 编译警告",
                    evidence=evidence,
                    impact=message,
                    suggestion="根据编译器诊断修正对应表达式或声明。",
                    analyzer="g++",
                )
            )
        return findings

    @staticmethod
    def _line(source: SourceFile, line_number: int) -> str:
        lines = source.content.splitlines()
        return lines[line_number - 1] if 0 < line_number <= len(lines) else ""

    @staticmethod
    def _finding(
        *,
        source: SourceFile,
        rule_id: str,
        category: str,
        severity: str,
        line: int,
        column: int,
        end_column: int,
        title: str,
        evidence: str,
        impact: str,
        suggestion: str,
        analyzer: str,
    ) -> Finding:
        return Finding(
            finding_id=f"finding-{uuid.uuid4().hex}",
            source="static",
            analyzer=analyzer,
            rule_id=rule_id,
            category=category,
            severity=severity,  # type: ignore[arg-type]
            confidence=1.0,
            file_id=source.file_id,
            start_line=line,
            start_column=column,
            end_line=line,
            end_column=max(column + 1, end_column),
            title=title,
            hover_summary=impact,
            detail=impact,
            evidence=evidence,
            impact=impact,
            suggestion=suggestion,
            verification=FindingVerification(
                range_valid=True,
                evidence_matched=True,
                static_confirmed=True,
            ),
        )

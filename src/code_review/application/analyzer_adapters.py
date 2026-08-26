from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from code_review.domain.review_models import RenamePlan, SourceFile

_MAX_SOURCE_BYTES = 1_000_000
_DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class AnalyzerCapability:
    analyzer_id: str
    available: bool
    mode: Literal["isolated", "fallback", "unavailable"]
    message: str
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class StandardDiagnostic:
    analyzer_id: str
    rule_id: str
    severity: Literal["error", "warning", "info"]
    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    message: str


class AnalyzerAdapter(Protocol):
    @property
    def capability(self) -> AnalyzerCapability: ...

    def analyze(self, source: SourceFile) -> list[StandardDiagnostic]: ...


def _validated_source(source: SourceFile) -> None:
    path = PurePosixPath(source.relative_path.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("source path escapes the review workspace")
    if len(source.content.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("source exceeds the isolated analyzer size limit")


class RuffAnalyzerAdapter:
    """Runs Ruff on stdin with repository configuration disabled."""

    def __init__(self, executable: str | None = None, timeout_seconds: float = 5.0) -> None:
        sibling_name = "ruff.exe" if os.name == "nt" else "ruff"
        sibling = Path(sys.executable).with_name(sibling_name)
        self._executable = (
            executable
            or (str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None)
            or shutil.which("ruff")
        )
        self._timeout_seconds = min(max(timeout_seconds, 0.1), _DEFAULT_TIMEOUT_SECONDS)

    @property
    def capability(self) -> AnalyzerCapability:
        if not self._executable:
            return AnalyzerCapability(
                "ruff", False, "fallback", "Ruff 不可用，继续使用内置词法作用域分析。"
            )
        return AnalyzerCapability(
            "ruff",
            True,
            "isolated",
            "Ruff 以 --isolated/stdin/无缓存模式运行。",
            self._timeout_seconds,
        )

    def analyze(self, source: SourceFile) -> list[StandardDiagnostic]:
        _validated_source(source)
        if not self._executable:
            return []
        with tempfile.TemporaryDirectory(prefix="codeastra-ruff-") as workdir:
            completed = subprocess.run(
                [
                    self._executable,
                    "check",
                    "--isolated",
                    "--no-cache",
                    "--select=E9,F401,F821",
                    "--output-format=json",
                    "--stdin-filename",
                    source.relative_path,
                    "-",
                ],
                input=source.content,
                text=True,
                capture_output=True,
                cwd=workdir,
                env={"PATH": os.path.dirname(self._executable)},
                timeout=self._timeout_seconds,
                check=False,
            )
        try:
            payload = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return []
        diagnostics: list[StandardDiagnostic] = []
        for item in payload if isinstance(payload, list) else []:
            location = item.get("location", {})
            end_location = item.get("end_location", location)
            code = item.get("code")
            message = item.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                continue
            diagnostics.append(
                StandardDiagnostic(
                    analyzer_id="ruff",
                    rule_id=f"ruff.{code.casefold()}",
                    severity="error" if code.startswith(("E9", "F")) else "warning",
                    path=source.relative_path,
                    line=max(1, int(location.get("row", 1))),
                    column=max(1, int(location.get("column", 1))),
                    end_line=max(1, int(end_location.get("row", location.get("row", 1)))),
                    end_column=max(1, int(end_location.get("column", location.get("column", 1)))),
                    message=message,
                )
            )
        return diagnostics


class PyflakesAnalyzerAdapter:
    """Optional stdin-only Pyflakes adapter; it never imports reviewed code."""

    @property
    def capability(self) -> AnalyzerCapability:
        available = importlib.util.find_spec("pyflakes") is not None
        return AnalyzerCapability(
            "pyflakes",
            available,
            "isolated" if available else "fallback",
            "Pyflakes stdin 分析可用。" if available else "Pyflakes 不可用。",
            _DEFAULT_TIMEOUT_SECONDS if available else None,
        )

    def analyze(self, source: SourceFile) -> list[StandardDiagnostic]:
        _validated_source(source)
        if not self.capability.available:
            return []
        with tempfile.TemporaryDirectory(prefix="codeastra-pyflakes-") as workdir:
            completed = subprocess.run(
                [sys.executable, "-I", "-m", "pyflakes", "-"],
                input=source.content,
                text=True,
                capture_output=True,
                cwd=workdir,
                env={"PATH": os.path.dirname(sys.executable)},
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                check=False,
            )
        if completed.stderr.strip() and not completed.stdout.strip():
            raise OSError("isolated pyflakes process was unavailable")
        diagnostics: list[StandardDiagnostic] = []
        for line in completed.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
                continue
            row, column, message = int(parts[1]), int(parts[2]), parts[3].strip()
            diagnostics.append(
                StandardDiagnostic(
                    analyzer_id="pyflakes",
                    rule_id="pyflakes.diagnostic",
                    severity="error" if "undefined name" in message.casefold() else "warning",
                    path=source.relative_path,
                    line=max(1, row),
                    column=max(1, column),
                    end_line=max(1, row),
                    end_column=max(2, column + 1),
                    message=message,
                )
            )
        return diagnostics


class LibCSTRenameAdapter:
    required_providers = (
        "ScopeProvider",
        "PositionProvider",
        "ParentNodeProvider",
        "QualifiedNameProvider",
    )

    @property
    def capability(self) -> AnalyzerCapability:
        available = importlib.util.find_spec("libcst") is not None
        return AnalyzerCapability(
            "libcst-rename",
            available,
            "isolated" if available else "unavailable",
            (
                "LibCST 及作用域/位置/父节点/限定名提供器可用。"
                if available
                else "LibCST 未安装；结构化重命名 fail-closed，不回退到文本替换。"
            ),
        )

    def apply(
        self,
        content: str,
        plan: RenamePlan,
        *,
        target_line: int,
        function_name: str,
    ) -> str:
        if not self.capability.available:
            raise RuntimeError(self.capability.message)
        import libcst as cst
        from libcst.metadata import (
            ParentNodeProvider,
            PositionProvider,
            QualifiedNameProvider,
            ScopeProvider,
        )

        old_name = plan.old_name
        new_name = plan.new_name

        class RenameTransformer(cst.CSTTransformer):
            METADATA_DEPENDENCIES = (
                ScopeProvider,
                PositionProvider,
                ParentNodeProvider,
                QualifiedNameProvider,
            )

            def __init__(self) -> None:
                self.target_depth = 0
                self.changed_definition = False
                self.changed_calls = 0

            def visit_FunctionDef(self, node: Any) -> None:
                position = self.get_metadata(PositionProvider, node)
                name = getattr(getattr(node, "name", None), "value", None)
                if position.start.line == target_line and name == function_name:
                    self.target_depth += 1

            def leave_FunctionDef(self, original_node: Any, updated_node: Any) -> Any:
                position = self.get_metadata(PositionProvider, original_node)
                name = getattr(getattr(original_node, "name", None), "value", None)
                if position.start.line == target_line and name == function_name:
                    self.target_depth -= 1
                    self.changed_definition = True
                return updated_node

            def leave_Name(self, original_node: Any, updated_node: Any) -> Any:
                if self.target_depth and getattr(original_node, "value", None) == old_name:
                    return updated_node.with_changes(value=new_name)
                return updated_node

            def leave_Arg(self, original_node: Any, updated_node: Any) -> Any:
                keyword = getattr(original_node, "keyword", None)
                if keyword is None or getattr(keyword, "value", None) != old_name:
                    return updated_node
                parent = self.get_metadata(ParentNodeProvider, original_node)
                function = getattr(parent, "func", None)
                called = (
                    function.value
                    if isinstance(function, cst.Name)
                    else function.attr.value
                    if isinstance(function, cst.Attribute)
                    else None
                )
                if called != function_name:
                    return updated_node
                self.changed_calls += 1
                return updated_node.with_changes(keyword=keyword.with_changes(value=new_name))

        module = cst.parse_module(content)
        wrapper = cst.MetadataWrapper(module)
        transformer = RenameTransformer()
        revised = wrapper.visit(transformer).code
        if not transformer.changed_definition:
            raise ValueError("scope_mismatch")
        expected_calls = sum(
            item.relative_path == plan.scope.split(":", 1)[0]
            for item in plan.affected_keyword_callsites
        )
        if transformer.changed_calls != expected_calls:
            raise ValueError("scope_mismatch")
        if revised == content:
            raise ValueError("no_effective_diff")
        return revised


class CodeQLDeepModeAdapter:
    def __init__(self, executable: str | None = None, timeout_seconds: float = 120.0) -> None:
        self._executable = executable or shutil.which("codeql")
        self._timeout_seconds = min(max(timeout_seconds, 1.0), 120.0)

    @property
    def capability(self) -> AnalyzerCapability:
        return AnalyzerCapability(
            "codeql-deep",
            False,
            "unavailable",
            (
                "CodeQL 可执行文件存在，但本轮未配置受控离线数据库构建器。"
                if self._executable
                else "CodeQL 未安装；异步 deep mode 不可用，未联网安装。"
            ),
            self._timeout_seconds,
        )


class SemgrepStructuralAdapter:
    """Capability-only adapter until an isolated, bounded runner is configured."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or shutil.which("semgrep")

    @property
    def capability(self) -> AnalyzerCapability:
        return AnalyzerCapability(
            "semgrep-structural",
            False,
            "unavailable",
            (
                "Semgrep 可执行文件存在，但未配置隔离、规则锁定和资源限制；默认流程禁用。"
                if self._executable
                else "Semgrep 未安装；为避免重复诊断和宿主机执行，默认流程禁用。"
            ),
        )


class CppIsolationExecutor:
    @property
    def capability(self) -> AnalyzerCapability:
        return AnalyzerCapability(
            "cpp-isolated-executor",
            False,
            "unavailable",
            "未检测到可证明的 container/userns/seccomp/资源限制；禁止宿主机 g++。",
        )

    def run_allowlisted(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(self.capability.message)


def analyzer_capabilities() -> list[AnalyzerCapability]:
    return [
        RuffAnalyzerAdapter().capability,
        PyflakesAnalyzerAdapter().capability,
        LibCSTRenameAdapter().capability,
        CodeQLDeepModeAdapter().capability,
        SemgrepStructuralAdapter().capability,
        CppIsolationExecutor().capability,
    ]

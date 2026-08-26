from __future__ import annotations

import ast
import asyncio
import builtins
import difflib
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from code_review.application.analyzer_adapters import (
    AnalyzerAdapter,
    PyflakesAnalyzerAdapter,
    RuffAnalyzerAdapter,
    StandardDiagnostic,
)
from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.python_analysis_cache import parse_python_source
from code_review.domain.review_models import (
    CoverageState,
    Finding,
    FindingVerification,
    RepairIntentOption,
    SourceFile,
    SymbolCandidate,
    SymbolRepairPlan,
    UseDefEvidence,
)


@dataclass(frozen=True)
class AnalyzerResult:
    findings: list[Finding]
    coverage: list[CoverageState]


@dataclass
class _PythonScope:
    parent: _PythonScope | None
    kind: str
    bindings: set[str]
    globals: set[str]
    nonlocals: set[str]


class _PythonNameAnalyzer(ast.NodeVisitor):
    """Collect lexical bindings first, then resolve loads using Python scope rules."""

    def __init__(self, tree: ast.AST) -> None:
        self.root = _PythonScope(None, "module", set(), set(), set())
        self.scopes: dict[ast.AST, _PythonScope] = {tree: self.root}
        self._scope = self.root
        self.loads: list[tuple[ast.Name, _PythonScope]] = []
        self.imports: dict[str, ast.alias] = {}
        self._collect(tree)
        self._scope = self.root
        self.visit(tree)

    @staticmethod
    def _arguments(node: ast.arguments) -> set[str]:
        values = {*[item.arg for item in node.posonlyargs], *[item.arg for item in node.args]}
        values.update(item.arg for item in node.kwonlyargs)
        if node.vararg:
            values.add(node.vararg.arg)
        if node.kwarg:
            values.add(node.kwarg.arg)
        return values

    @staticmethod
    def _pattern_names(pattern: ast.pattern) -> set[str]:
        names: set[str] = set()
        for item in ast.walk(pattern):
            if isinstance(item, (ast.MatchAs, ast.MatchStar)) and item.name:
                names.add(item.name)
            elif isinstance(item, ast.MatchMapping) and item.rest:
                names.add(item.rest)
        return names

    def _bind(self, name: str, scope: _PythonScope | None = None) -> None:
        target = scope or self._scope
        if name in target.globals:
            self.root.bindings.add(name)
        elif name in target.nonlocals:
            parent = target.parent
            while parent is not None and parent.kind == "class":
                parent = parent.parent
            if parent is not None:
                parent.bindings.add(name)
        else:
            target.bindings.add(name)

    def _child(self, node: ast.AST, kind: str, initial: set[str] | None = None) -> _PythonScope:
        child = _PythonScope(self._scope, kind, set(initial or ()), set(), set())
        self.scopes[node] = child
        return child

    def _collect(self, node: ast.AST) -> None:
        previous = self._scope
        scope = self.scopes.get(node)
        if scope is not None:
            self._scope = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._bind(node.name, previous)
            for item in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
                if item is not None:
                    self._collect(item)
            child = self._child(node, "function", self._arguments(node.args))
            self._scope = child
            for statement in node.body:
                self._collect(statement)
            self._scope = previous
            return
        if isinstance(node, ast.Lambda):
            for item in [*node.args.defaults, *node.args.kw_defaults]:
                if item is not None:
                    self._collect(item)
            child = self._child(node, "function", self._arguments(node.args))
            self._scope = child
            self._collect(node.body)
            self._scope = previous
            return
        if isinstance(node, ast.ClassDef):
            self._bind(node.name, previous)
            for expression in node.decorator_list:
                self._collect(expression)
            for expression in node.bases:
                self._collect(expression)
            for keyword in node.keywords:
                self._collect(keyword)
            child = self._child(node, "class")
            self._scope = child
            for statement in node.body:
                self._collect(statement)
            self._scope = previous
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            child = self._child(node, "comprehension")
            self._scope = child
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            target_scope = self._scope
            while target_scope.kind == "comprehension" and target_scope.parent is not None:
                target_scope = target_scope.parent
            self._bind(node.target.id, target_scope)
            self._collect(node.value)
            self._scope = previous
            return
        if isinstance(node, ast.Global):
            self._scope.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            self._scope.nonlocals.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
            self._bind(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                self._bind(name)
                self.imports[name] = alias
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(node.name)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for name in self._pattern_names(case.pattern):
                    self._bind(name)
        for child_node in ast.iter_child_nodes(node):
            self._collect(child_node)
        self._scope = previous

    def _visit_body_scope(self, node: ast.AST, body: list[ast.stmt]) -> None:
        previous = self._scope
        self._scope = self.scopes[node]
        for statement in body:
            self.visit(statement)
        self._scope = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for item in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)
        if node.returns:
            self.visit(node.returns)
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if argument.annotation:
                self.visit(argument.annotation)
        self._visit_body_scope(node, node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for item in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(item)
        self._visit_body_scope(node, node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        previous = self._scope
        self._scope = self.scopes[node]
        self.visit(node.body)
        self._scope = previous

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.visit(node.generators[0].iter)
        previous = self._scope
        self._scope = self.scopes[node]
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)
        self._scope = previous

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.visit_ListComp(node)  # type: ignore[arg-type]

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.visit(node.generators[0].iter)
        previous = self._scope
        self._scope = self.scopes[node]
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.key)
        self.visit(node.value)
        self._scope = previous

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit_ListComp(node)  # type: ignore[arg-type]

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.loads.append((node, self._scope))

    def resolves(self, name: str, scope: _PythonScope) -> bool:
        if name in dir(builtins):
            return True
        current: _PythonScope | None = scope
        while current is not None:
            if name in current.globals:
                return name in self.root.bindings
            if name in current.bindings:
                return True
            if current.kind == "function" and current.parent and current.parent.kind == "class":
                current = current.parent.parent
            else:
                current = current.parent
        return False


class StaticAnalyzer:
    def __init__(
        self,
        standard_adapter: AnalyzerAdapter | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self._metrics = metrics or PipelineMetrics()
        self._python_cache: dict[str, list[Finding]] = {}
        self._python_cache_analyzer: dict[str, str] = {}
        self._python_ast_cache: dict[str, ast.Module] = {}
        self._python_scope_cache: dict[str, _PythonNameAnalyzer] = {}
        self._use_def_cache: dict[tuple[object, ...], Finding] = {}
        if standard_adapter is not None:
            self._standard_adapter = standard_adapter
        else:
            ruff = RuffAnalyzerAdapter()
            pyflakes = PyflakesAnalyzerAdapter()
            self._standard_adapter = ruff if ruff.capability.available else pyflakes

    async def analyze(
        self, files: list[SourceFile], *, external_standard: bool = True
    ) -> AnalyzerResult:
        self._metrics.increment("analyzer_calls")
        findings: list[Finding] = []
        coverage: list[CoverageState] = []
        by_language: dict[str, list[SourceFile]] = {}
        for source in files:
            by_language.setdefault(source.language, []).append(source)

        for language, language_files in by_language.items():
            if language == "python":
                adapter = self._standard_adapter
                adapter_available = external_standard and adapter.capability.available
                adapter_failed = False
                for source in language_files:
                    cache_key = (
                        f"{source.sha256}:{adapter.capability.analyzer_id}"
                        if adapter_available
                        else f"{source.sha256}:lexical"
                    )
                    cached = self._python_cache.get(cache_key)
                    if cached is None:
                        self._metrics.increment("analyzer_cache_misses")
                        cached = self._analyze_python(
                            source, include_standard_names=not adapter_available
                        )
                        if adapter_available and not any(
                            item.rule_id == "python.syntax-error" for item in cached
                        ):
                            try:
                                diagnostics = adapter.analyze(source)
                            except (OSError, subprocess.SubprocessError, ValueError):
                                adapter_failed = True
                                cached = self._analyze_python(source, include_standard_names=True)
                            else:
                                cached = self._merge_adapter_diagnostics(
                                    source, cached, diagnostics
                                )
                        used_analyzer = (
                            adapter.capability.analyzer_id
                            if adapter_available and not adapter_failed
                            else "python-ast"
                        )
                        self._python_cache[cache_key] = cached
                        self._python_cache_analyzer[cache_key] = used_analyzer
                    else:
                        self._metrics.increment("analyzer_cache_hits")
                        if self._python_cache_analyzer.get(cache_key) == "python-ast":
                            adapter_failed = True
                    findings.extend(self._enrich_undefined_findings(source, cached, language_files))
                analyzer_name = (
                    adapter.capability.analyzer_id
                    if adapter_available and not adapter_failed
                    else "python-ast"
                )
                coverage.append(
                    CoverageState(
                        language="python",
                        analyzer=analyzer_name,
                        available=True,
                        message=(
                            "标准名称/导入规则由隔离 adapter 提供；内置 AST 只补充非重复规则。"
                            if adapter_available and not adapter_failed
                            else "外部 adapter 不可用，已 fail-closed 回退内置词法作用域分析。"
                        ),
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

    def _analyze_python(
        self, source: SourceFile, *, include_standard_names: bool = True
    ) -> list[Finding]:
        try:
            tree = parse_python_source(source)
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
        names = _PythonNameAnalyzer(tree)
        self._python_ast_cache[source.sha256] = tree
        self._python_scope_cache[source.sha256] = names
        imported = names.imports
        loaded = {node.id for node, _ in names.loads}

        for name, alias in imported.items() if include_standard_names else []:
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

        seen_undefined: set[tuple[str, int, int]] = set()
        for node, scope in names.loads if include_standard_names else []:
            key = (node.id, node.lineno, node.col_offset)
            if names.resolves(node.id, scope) or key in seen_undefined:
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

        for call_node in ast.walk(tree):
            if not (
                isinstance(call_node, ast.Call)
                and isinstance(call_node.func, ast.Name)
                and call_node.func.id in {"eval", "exec"}
            ):
                continue
            evidence = ast.get_source_segment(source.content, call_node) or call_node.func.id
            findings.append(
                self._finding(
                    source=source,
                    rule_id=f"python.dangerous-{call_node.func.id}",
                    category="security",
                    severity="high",
                    line=call_node.lineno,
                    column=call_node.col_offset + 1,
                    end_column=(call_node.end_col_offset or call_node.col_offset + len(evidence))
                    + 1,
                    title=f"危险的 {call_node.func.id} 调用",
                    evidence=evidence,
                    impact="不可信输入可能被解释为 Python 代码并执行。",
                    suggestion="使用安全解析器、显式数据结构或白名单映射。",
                    analyzer="python-ast",
                )
            )
        return findings

    def _enrich_undefined_findings(
        self,
        source: SourceFile,
        findings: list[Finding],
        project_files: list[SourceFile],
    ) -> list[Finding]:
        project_revision = tuple(sorted((item.file_id, item.sha256) for item in project_files))
        enriched: list[Finding] = []
        for finding in findings:
            if finding.rule_id != "python.undefined-name":
                enriched.append(finding)
                continue
            key = (
                source.sha256,
                finding.start_line,
                finding.start_column,
                finding.evidence,
                project_revision,
            )
            cached = self._use_def_cache.get(key)
            if cached is None:
                cached = self._build_use_def_finding(source, finding, project_files)
                self._use_def_cache[key] = cached
                if len(self._use_def_cache) > 512:
                    self._use_def_cache.pop(next(iter(self._use_def_cache)))
            enriched.append(cached)
        return enriched

    def _build_use_def_finding(
        self,
        source: SourceFile,
        finding: Finding,
        project_files: list[SourceFile],
    ) -> Finding:
        tree = self._python_ast_cache.get(source.sha256) or parse_python_source(source)
        names = self._python_scope_cache.get(source.sha256) or _PythonNameAnalyzer(tree)
        target_entry = next(
            (
                (node, scope)
                for node, scope in names.loads
                if node.id == finding.evidence
                and node.lineno == finding.start_line
                and node.col_offset + 1 == finding.start_column
            ),
            None,
        )
        if target_entry is None:
            return finding
        target, scope = target_entry
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        statements = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.stmt)
            and node.lineno <= target.lineno <= (node.end_lineno or node.lineno)
        ]
        statement = min(
            statements,
            key=lambda node: (node.end_lineno or node.lineno) - node.lineno,
        )
        scope_node = next((node for node, item in names.scopes.items() if item is scope), tree)
        scope_symbol = (
            scope_node.name
            if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else None
        )
        parameters = (
            sorted(_PythonNameAnalyzer._arguments(scope_node.args))
            if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            else []
        )
        visible: set[str] = set()
        current: _PythonScope | None = scope
        while current is not None:
            visible.update(current.bindings)
            if current.kind == "function" and current.parent and current.parent.kind == "class":
                current = current.parent.parent
            else:
                current = current.parent
        visible.discard(target.id)
        visible_imports = sorted(name for name in visible if name in names.imports)
        visible_assignments = sorted(visible - set(parameters) - set(visible_imports))
        similar: list[SymbolCandidate] = []
        for candidate in sorted(visible):
            confidence = difflib.SequenceMatcher(None, target.id, candidate).ratio()
            if confidence < 0.6:
                continue
            kind: Literal["parameter", "import", "assignment", "binding", "export"] = (
                "parameter"
                if candidate in parameters
                else "import"
                if candidate in visible_imports
                else "assignment"
            )
            similar.append(
                SymbolCandidate(
                    name=candidate,
                    kind=kind,
                    confidence=confidence,
                    relative_path=source.relative_path,
                    rationale="当前词法作用域中可见，名称与未解析符号相似。",
                )
            )
        similar.sort(key=lambda item: (-item.confidence, item.name))
        cross_exports = self._cross_file_exports(target.id, source, project_files)
        high_confidence = [item for item in similar if item.confidence >= 0.84]
        plan: SymbolRepairPlan | None = None
        if len(high_confidence) == 1:
            selected = high_confidence[0]
            plan = SymbolRepairPlan(
                mode="rename",
                unresolved_name=target.id,
                replacement_name=selected.name,
                definition_symbol=scope_symbol,
                statement_start_line=statement.lineno,
                statement_end_line=statement.end_lineno or statement.lineno,
                use_line=target.lineno,
                use_column=target.col_offset + 1,
                base_sha=source.sha256,
                safety="safe",
            )
        elif not high_confidence and len(cross_exports) == 1:
            exported = cross_exports[0]
            plan = SymbolRepairPlan(
                mode="import",
                unresolved_name=target.id,
                module=self._module_name(exported.relative_path or ""),
                definition_symbol=scope_symbol,
                statement_start_line=statement.lineno,
                statement_end_line=statement.end_lineno or statement.lineno,
                use_line=target.lineno,
                use_column=target.col_offset + 1,
                base_sha=source.sha256,
                safety="safe",
            )
        options = [
            RepairIntentOption(
                option_id=f"rename:{item.name}",
                kind="rename_existing",
                label=f"将 {target.id} 改为已有符号 {item.name}",
                symbol=item.name,
            )
            for item in similar[:8]
        ]
        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            options.append(
                RepairIntentOption(
                    option_id="declare_parameter",
                    kind="declare_parameter",
                    label=f"将 {target.id} 声明为函数参数",
                )
            )
        options.append(
            RepairIntentOption(
                option_id="declare_local",
                kind="declare_local",
                label=f"在使用前声明局部变量 {target.id}",
                requires_input="initializer",
                input_label="请输入初始化表达式（不会提供默认值）",
            )
        )
        options.extend(
            RepairIntentOption(
                option_id=f"import:{item.relative_path}",
                kind="import_symbol",
                label=f"从 {item.relative_path} 导入 {target.id}",
                symbol=target.id,
                module=self._module_name(item.relative_path or ""),
            )
            for item in cross_exports[:8]
        )
        if plan is None:
            options.append(
                RepairIntentOption(
                    option_id="custom_behavior",
                    kind="custom_behavior",
                    label="描述期望的业务行为",
                    requires_input="behavior",
                    input_label="期望行为（自然语言，不是 Python 表达式）",
                )
            )
        options.append(
            RepairIntentOption(
                option_id="defer",
                kind="defer",
                label="暂不处理，保留问题",
            )
        )
        conditional_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match)
        current_node: ast.AST | None = statement
        conditional = False
        while current_node is not None and current_node is not scope_node:
            current_node = parents.get(current_node)
            conditional = conditional or isinstance(current_node, conditional_nodes)
        use_def = UseDefEvidence(
            unresolved_name=target.id,
            scope_kind=scope.kind,
            scope_symbol=scope_symbol,
            use_line=target.lineno,
            use_column=target.col_offset + 1,
            statement_kind=type(statement).__name__,
            statement_start_line=statement.lineno,
            statement_end_line=statement.end_lineno or statement.lineno,
            statement_text=ast.get_source_segment(source.content, statement) or finding.evidence,
            visible_parameters=parameters,
            visible_imports=visible_imports,
            visible_assignments=visible_assignments,
            similar_candidates=similar,
            cross_file_exports=cross_exports,
            control_flow_reachability="conditional" if conditional else "reachable",
            outcome="safe_plan" if plan is not None else "needs_intent",
            explanation=(
                "存在唯一高置信结构候选，可生成确定性修复计划。"
                if plan is not None
                else "已定位未解析名称，但没有唯一证据能确定开发者意图。"
            ),
            options=options,
        )
        return finding.model_copy(
            update={
                "use_def_evidence": use_def,
                "symbol_repair_plan": plan,
                # The unresolved-name diagnostic is an applicable deterministic fact;
                # only the repair intent is ambiguous.
                "applicability": "applicable",
                "fix_safety": plan.safety if plan is not None else "unsafe",
                "symbol": scope_symbol,
                "suggestion": (
                    f"将 {target.id} 修正为唯一候选 {plan.replacement_name}。"
                    if plan is not None and plan.mode == "rename"
                    else f"从 {plan.module} 导入 {target.id}。"
                    if plan is not None
                    else "请选择已有符号、参数、局部声明或导入来源；系统不会猜测值。"
                ),
            }
        )

    @staticmethod
    def _module_name(relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        if normalized.endswith("/__init__.py"):
            normalized = normalized[: -len("/__init__.py")]
        elif normalized.endswith(".py"):
            normalized = normalized[:-3]
        return normalized.strip("/").replace("/", ".")

    def _cross_file_exports(
        self,
        name: str,
        source: SourceFile,
        project_files: list[SourceFile],
    ) -> list[SymbolCandidate]:
        exports: list[SymbolCandidate] = []
        for candidate_source in project_files:
            if candidate_source.file_id == source.file_id or candidate_source.language != "python":
                continue
            try:
                tree = parse_python_source(candidate_source)
            except SyntaxError:
                continue
            for statement in tree.body:
                exported = (
                    statement.name
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    else None
                )
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    if any(
                        isinstance(target, ast.Name) and target.id == name for target in targets
                    ):
                        exported = name
                if exported == name:
                    exports.append(
                        SymbolCandidate(
                            name=name,
                            kind="export",
                            confidence=0.95,
                            relative_path=candidate_source.relative_path,
                            line=statement.lineno,
                            rationale="项目内唯一模块级同名导出。",
                        )
                    )
        return exports

    def _merge_adapter_diagnostics(
        self,
        source: SourceFile,
        built_in: list[Finding],
        diagnostics: list[StandardDiagnostic],
    ) -> list[Finding]:
        findings = list(built_in)
        identities = {(item.rule_id, item.start_line, item.start_column) for item in findings}
        for diagnostic in diagnostics:
            message = diagnostic.message.casefold()
            if diagnostic.rule_id in {"ruff.f821"} or "undefined name" in message:
                rule_id = "python.undefined-name"
                title = "名称未定义"
                impact = "执行到该位置时会触发 NameError。"
                suggestion = "修正名称拼写，或在使用前完成定义和导入。"
            elif diagnostic.rule_id in {"ruff.f401"} or "imported but unused" in message:
                rule_id = "python.unused-import"
                title = "未使用的导入"
                impact = "无效导入会增加阅读成本，并可能引入不必要的初始化开销。"
                suggestion = "如果其他位置确实没有使用该导入，请删除它。"
            else:
                continue
            identity = (rule_id, diagnostic.line, diagnostic.column)
            if identity in identities:
                continue
            quoted = re.search(r"`([^`]+)`", diagnostic.message)
            evidence = (
                quoted.group(1)
                if quoted is not None
                else self._line(source, diagnostic.line).strip() or diagnostic.message
            )
            findings.append(
                self._finding(
                    source=source,
                    rule_id=rule_id,
                    category=(
                        "correctness" if rule_id.endswith("undefined-name") else "code_quality"
                    ),
                    severity="medium" if rule_id.endswith("undefined-name") else "info",
                    line=diagnostic.line,
                    column=diagnostic.column,
                    end_column=diagnostic.end_column,
                    title=title,
                    evidence=evidence,
                    impact=impact,
                    suggestion=suggestion,
                    analyzer=diagnostic.analyzer_id,
                )
            )
            identities.add(identity)
        return findings

    def _analyze_cpp(self, files: list[SourceFile]) -> tuple[list[Finding], CoverageState]:
        del files
        return [], CoverageState(
            language="cpp",
            analyzer="g++",
            available=False,
            message=(
                "C++ 静态覆盖不可用：主服务没有低权限容器或受验证的隔离执行器，"
                "因此未启动 g++，也未运行任何用户程序。-I 仅影响搜索顺序，不能隔离文件读取。"
            ),
        )

    def _parse_cpp_diagnostics(
        self,
        targets: dict[Path, SourceFile],
        stderr: str,
    ) -> list[Finding]:
        findings: list[Finding] = []
        pattern = re.compile(r"^(.+?):(\d+):(\d+):\s+(error|warning):\s+(.+)$")
        for line in stderr.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            filename, line_number, column, level, message = match.groups()
            try:
                diagnostic_path = Path(filename).resolve()
            except OSError:
                continue
            source = targets.get(diagnostic_path)
            if source is None:
                continue
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
            relative_path=source.relative_path,
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
            applicability="applicable",
        )

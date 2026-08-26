from __future__ import annotations

import ast
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.python_analysis_cache import parse_python_source
from code_review.domain.model_protocol import ReviewFinding
from code_review.domain.review_models import (
    Finding,
    RenameCallsite,
    RenamePlan,
    SourceFile,
)

_SNAKE_CASE = re.compile(r"^[a-z_][a-z0-9_]*$")
_PARAMETER_NAMING_MARKERS = (
    "参数命名",
    "parameter naming",
    "parameter_name",
)


@dataclass(frozen=True)
class FindingVerificationResult:
    action: Literal["accept", "suppress"]
    code: Literal[
        "verified",
        "already_compliant",
        "finding_not_actionable",
        "speculative_finding",
        "root_cause_unverified",
    ]
    message: str
    symbol: str | None = None
    rename_plan: RenamePlan | None = None


class FindingVerifier:
    """Rule-specific verification for LLM hypotheses before navigation or repair."""

    minimum_confidence = 0.7

    def __init__(self, metrics: PipelineMetrics | None = None) -> None:
        self._metrics = metrics or PipelineMetrics()
        self._naming_cache: OrderedDict[tuple[object, ...], FindingVerificationResult] = (
            OrderedDict()
        )

    def _remember(
        self,
        key: tuple[object, ...],
        result: FindingVerificationResult,
    ) -> FindingVerificationResult:
        self._naming_cache[key] = result
        self._naming_cache.move_to_end(key)
        while len(self._naming_cache) > 256:
            self._naming_cache.popitem(last=False)
        return result

    def verify_draft(
        self,
        draft: ReviewFinding,
        source: SourceFile,
        files: list[SourceFile],
    ) -> FindingVerificationResult:
        naming = self._verify_parameter_naming(
            title=draft.title,
            category=draft.category,
            rule_id=draft.rule_id,
            evidence=draft.evidence,
            start_line=draft.start_line,
            source=source,
            files=files,
        )
        if naming is not None:
            return naming
        root_cause = self._verify_root_cause_draft(draft, source)
        if root_cause is not None:
            return root_cause
        if draft.confidence < self.minimum_confidence:
            return FindingVerificationResult(
                action="suppress",
                code="finding_not_actionable",
                message=(
                    f"模型置信度 {draft.confidence:.2f} 低于正式问题阈值 "
                    f"{self.minimum_confidence:.2f}。"
                ),
            )
        return FindingVerificationResult(
            action="accept",
            code="verified",
            message="文件、范围、证据与置信度校验通过；语义结论仍需人工确认。",
        )

    def verify_existing(
        self,
        finding: Finding,
        files: list[SourceFile],
    ) -> FindingVerificationResult:
        source = next((item for item in files if item.file_id == finding.file_id), None)
        if source is None:
            return FindingVerificationResult(
                "suppress", "finding_not_actionable", "问题对应文件已不存在。"
            )
        naming = self._verify_parameter_naming(
            title=finding.title,
            category=finding.category,
            rule_id=finding.rule_id,
            evidence=finding.evidence,
            start_line=finding.start_line,
            source=source,
            files=files,
        )
        if naming is not None:
            return naming
        speculative = self._verify_speculative_existing(finding, source)
        return speculative or FindingVerificationResult(
            "accept", "verified", "现有问题不属于参数命名规则。", symbol=finding.symbol
        )

    @staticmethod
    def _is_speculative(text: str) -> bool:
        normalized = text.casefold()
        return any(
            marker in normalized
            for marker in (
                "可能",
                "考虑",
                "边缘情况",
                "其他类型",
                "不完整",
                " may ",
                " could ",
                "consider",
                "edge case",
                "other type",
            )
        )

    @staticmethod
    def _has_bool_fallback(source: SourceFile, line: int) -> bool:
        try:
            tree = parse_python_source(source)
        except SyntaxError:
            return False
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= line <= (node.end_lineno or node.lineno)
        ]
        if not functions:
            return False
        target = min(functions, key=lambda node: (node.end_lineno or node.lineno) - node.lineno)
        return any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "bool"
            for node in ast.walk(target)
        )

    def _verify_root_cause_draft(
        self, draft: ReviewFinding, source: SourceFile
    ) -> FindingVerificationResult | None:
        text = " ".join((draft.title, draft.impact, draft.suggestion, draft.evidence))
        if self._has_bool_fallback(source, draft.start_line) and (
            "isinstance" in draft.evidence and self._is_speculative(text)
        ):
            return FindingVerificationResult(
                "suppress",
                "speculative_finding",
                "默认 return bool(value) 已覆盖容器真值；没有契约或反例证明该分支有缺陷。",
            )
        if draft.root_cause_claim is None:
            return FindingVerificationResult(
                "suppress",
                "speculative_finding" if self._is_speculative(text) else "root_cause_unverified",
                "模型结论缺少具体失败输入、预期与实际行为、受影响路径和修复不变量。",
            )
        claim = draft.root_cause_claim
        if claim.affected_path != source.relative_path:
            return FindingVerificationResult(
                "suppress", "root_cause_unverified", "根因证据的受影响路径与当前文件不一致。"
            )
        return FindingVerificationResult(
            "accept", "verified", "结构化根因、具体反例、预期/实际行为和修复不变量已提供。"
        )

    def _verify_speculative_existing(
        self, finding: Finding, source: SourceFile
    ) -> FindingVerificationResult | None:
        if finding.source == "static":
            return None
        text = " ".join((finding.title, finding.detail, finding.suggestion, finding.evidence))
        if self._is_speculative(text) and finding.root_cause_claim is None:
            message = "该模型建议没有可验证的具体反例或业务契约。"
            if (
                self._has_bool_fallback(source, finding.start_line)
                and "isinstance" in finding.evidence
            ):
                message = (
                    "默认 return bool(value) 已覆盖容器真值，不能据单个 isinstance 分支推断缺陷。"
                )
            return FindingVerificationResult("suppress", "speculative_finding", message)
        return None

    def _verify_parameter_naming(
        self,
        *,
        title: str,
        category: str,
        rule_id: str,
        evidence: str,
        start_line: int,
        source: SourceFile,
        files: list[SourceFile],
    ) -> FindingVerificationResult | None:
        claim = " ".join((title, category, rule_id)).casefold()
        if not any(marker in claim for marker in _PARAMETER_NAMING_MARKERS):
            return None
        cache_key: tuple[object, ...] = (
            claim,
            start_line,
            source.file_id,
            source.sha256,
            tuple(sorted((item.file_id, item.sha256) for item in files)),
        )
        cached = self._naming_cache.get(cache_key)
        if cached is not None:
            self._metrics.increment("verifier_cache_hits")
            self._naming_cache.move_to_end(cache_key)
            return cached
        self._metrics.increment("verifier_cache_misses")
        try:
            tree = parse_python_source(source)
        except SyntaxError:
            return self._remember(
                cache_key,
                FindingVerificationResult(
                    "suppress",
                    "finding_not_actionable",
                    "原文件无法解析，不能验证参数命名契约。",
                ),
            )
        target, symbol, class_name, base_names = self._target_function(tree, start_line)
        if target is None:
            return self._remember(
                cache_key,
                FindingVerificationResult(
                    "suppress",
                    "finding_not_actionable",
                    "证据没有对应到可验证的函数定义。",
                ),
            )
        del evidence
        parameters = self._parameters(target)
        inconsistent = [
            (index, name)
            for index, name in enumerate(parameters)
            if name not in {"self", "cls"} and not _SNAKE_CASE.fullmatch(name)
        ]
        if not inconsistent:
            return self._remember(
                cache_key,
                FindingVerificationResult(
                    "suppress",
                    "already_compliant",
                    "函数参数已经全部符合 snake_case，未发现可执行的命名修复。",
                    symbol=symbol,
                ),
            )
        index, old_name = inconsistent[0]
        candidates = self._family_parameter_names(
            files,
            target.name,
            index,
            target_file_id=source.file_id,
            target_line=target.lineno,
            target_class=class_name,
            target_bases=base_names,
        )
        canonical = {name for name in candidates if _SNAKE_CASE.fullmatch(name)}
        if len(canonical) != 1:
            return self._remember(
                cache_key,
                FindingVerificationResult(
                    "suppress",
                    "finding_not_actionable",
                    "没有同一接口族或 override 的唯一结构证据来确定新参数名。",
                    symbol=symbol,
                ),
            )
        new_name = next(iter(canonical))
        callsites, dynamic = self._keyword_callsites(files, target.name, old_name)
        unsafe_reasons: list[str] = []
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
            and node is not target
            and any(
                isinstance(child, ast.Name) and child.id == old_name for child in ast.walk(node)
            )
            for node in ast.walk(target)
        ):
            unsafe_reasons.append("目标定义包含引用该名称的嵌套作用域。")
        if dynamic:
            unsafe_reasons.append("存在 **kwargs、动态反射或字符串调用，无法证明调用点完整。")
        if any(item.relative_path != source.relative_path for item in callsites):
            unsafe_reasons.append("重命名涉及多文件原子修订，当前候选事务不支持。")
        if not target.name.startswith("_"):
            unsafe_reasons.append("方法可能属于外部 API，重命名关键字参数需要人工确认。")
        safety: Literal["safe", "requires_review"] = "requires_review" if unsafe_reasons else "safe"
        plan = RenamePlan(
            old_name=old_name,
            new_name=new_name,
            definition_symbol=symbol or target.name,
            affected_keyword_callsites=callsites,
            scope=f"{source.relative_path}:{target.lineno}-{target.end_lineno or target.lineno}",
            base_sha=source.sha256,
            safety=safety,
            executable=not unsafe_reasons,
            unsafe_reasons=unsafe_reasons,
        )
        return self._remember(
            cache_key,
            FindingVerificationResult(
                "accept",
                "verified",
                "参数名与同一接口族的对应位置不一致，已生成结构化 RenamePlan。",
                symbol=symbol,
                rename_plan=plan,
            ),
        )

    @staticmethod
    def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        return [item.arg for item in [*node.args.posonlyargs, *node.args.args]]

    @staticmethod
    def _target_function(
        tree: ast.Module, line: int
    ) -> tuple[
        ast.FunctionDef | ast.AsyncFunctionDef | None,
        str | None,
        str | None,
        set[str],
    ]:
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= line <= (node.end_lineno or node.lineno)
        ]
        if not candidates:
            return None, None, None, set()
        target = min(candidates, key=lambda node: (node.end_lineno or node.lineno) - node.lineno)
        parent = parents.get(target)
        if not isinstance(parent, ast.ClassDef):
            return target, target.name, None, set()
        base_names = {
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else ""
            for base in parent.bases
        }
        base_names.discard("")
        return target, f"{parent.name}.{target.name}", parent.name, base_names

    def _family_parameter_names(
        self,
        files: list[SourceFile],
        function_name: str,
        index: int,
        *,
        target_file_id: str,
        target_line: int,
        target_class: str | None,
        target_bases: set[str],
    ) -> list[str]:
        if target_class is None:
            return []
        names: list[str] = []
        for source in files:
            if source.language != "python":
                continue
            try:
                tree = parse_python_source(source)
            except SyntaxError:
                continue
            for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
                class_bases = {
                    base.id
                    if isinstance(base, ast.Name)
                    else base.attr
                    if isinstance(base, ast.Attribute)
                    else ""
                    for base in class_node.bases
                }
                related = class_node.name in target_bases or target_class in class_bases
                if not related:
                    continue
                for node in class_node.body:
                    if not (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == function_name
                    ):
                        continue
                    if source.file_id == target_file_id and node.lineno == target_line:
                        continue
                    parameters = self._parameters(node)
                    if index < len(parameters):
                        names.append(parameters[index])
        return names

    @staticmethod
    def _keyword_callsites(
        files: list[SourceFile], function_name: str, old_name: str
    ) -> tuple[list[RenameCallsite], bool]:
        callsites: list[RenameCallsite] = []
        dynamic = False
        for source in files:
            if source.language != "python":
                continue
            try:
                tree = parse_python_source(source)
            except SyntaxError:
                dynamic = True
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    called = (
                        node.func.id
                        if isinstance(node.func, ast.Name)
                        else node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else None
                    )
                    if called == function_name:
                        for keyword in node.keywords:
                            if keyword.arg == old_name:
                                callsites.append(
                                    RenameCallsite(
                                        relative_path=source.relative_path,
                                        line=keyword.lineno,
                                        keyword=old_name,
                                    )
                                )
                            elif keyword.arg is None:
                                dynamic = True
                    if (
                        isinstance(node.func, ast.Name)
                        and node.func.id in {"getattr", "setattr"}
                        and any(
                            isinstance(argument, ast.Constant)
                            and argument.value in {function_name, old_name}
                            for argument in node.args
                        )
                    ):
                        dynamic = True
        return callsites, dynamic

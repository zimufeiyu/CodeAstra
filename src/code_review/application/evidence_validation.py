from __future__ import annotations

import re
import uuid

from code_review.domain.model_protocol import ReviewFinding
from code_review.domain.review_chunks import ReviewChunk
from code_review.domain.review_models import Finding, FindingVerification, SourceFile

_DETERMINISTIC_MARKERS = (
    "unused",
    "未使用",
    "undefined",
    "未定义",
    "syntax",
    "语法",
)
_GENERIC_TITLES = {
    "存在风险",
    "代码存在问题",
    "建议优化",
    "潜在问题",
    "未使用安全编码实践",
}
_HIGH_RISK_MARKERS = (
    "eval(",
    "exec(",
    "命令注入",
    "command injection",
    "sql注入",
    "sql injection",
    "认证绕过",
    "任意代码执行",
    "远程代码执行",
    "路径穿越",
    "不安全反序列化",
)


class EvidenceValidator:
    def validate(
        self,
        draft: ReviewFinding,
        files: list[SourceFile],
        static_findings: list[Finding],
        *,
        target_chunk: ReviewChunk | None = None,
        analyzer_name: str = "qwen3-8b",
    ) -> Finding | None:
        source = next(
            (item for item in files if item.relative_path == draft.file),
            None,
        )
        if source is None:
            return None
        if target_chunk is not None:
            if source.file_id != target_chunk.target_file_id:
                return None
            if (
                draft.start_line < target_chunk.target_start_line
                or draft.end_line > target_chunk.target_end_line
            ):
                return None

        lines = source.content.splitlines()
        if not (1 <= draft.start_line <= draft.end_line <= max(1, len(lines))):
            return None

        title = draft.title.strip()
        if title in _GENERIC_TITLES or len(title) < 6:
            return None

        normalized_claim = " ".join(
            (draft.rule_id, draft.category, draft.title, draft.evidence)
        ).lower()
        if "literal_eval(" in draft.evidence.lower() and re.search(
            r"(?<![\\w.])eval\\(",
            normalized_claim,
        ):
            return None
        if any(
            marker in normalized_claim for marker in _DETERMINISTIC_MARKERS
        ) and not self._has_static_confirmation(draft, source, static_findings):
            return None

        location = self._locate_evidence(source, draft)
        if location is None:
            return None
        start_line, start_column, end_line, end_column = location

        severity = draft.severity
        if severity in {"critical", "high"} and not any(
            marker in normalized_claim for marker in _HIGH_RISK_MARKERS
        ):
            severity = "medium"

        impact = draft.impact.strip()
        return Finding(
            finding_id=f"finding-{uuid.uuid4().hex}",
            source="llm",
            analyzer=analyzer_name,
            rule_id=draft.rule_id,
            category=draft.category,
            severity=severity,
            confidence=draft.confidence,
            file_id=source.file_id,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            title=title,
            hover_summary=(impact or title)[:160],
            detail=impact,
            evidence=draft.evidence.strip(),
            impact=impact,
            suggestion=draft.suggestion.strip(),
            verification=FindingVerification(
                range_valid=True,
                evidence_matched=True,
                static_confirmed=False,
            ),
        )

    @staticmethod
    def _has_static_confirmation(
        draft: ReviewFinding,
        source: SourceFile,
        static_findings: list[Finding],
    ) -> bool:
        return any(
            finding.file_id == source.file_id
            and finding.start_line <= draft.end_line
            and finding.end_line >= draft.start_line
            for finding in static_findings
        )

    @staticmethod
    def _locate_evidence(
        source: SourceFile,
        draft: ReviewFinding,
    ) -> tuple[int, int, int, int] | None:
        evidence = draft.evidence.strip()
        if not evidence:
            return None
        lines = source.content.splitlines()
        selected = "\n".join(lines[draft.start_line - 1 : draft.end_line])
        normalized_evidence = re.sub(r"\s+", " ", evidence).strip()
        normalized_selected = re.sub(r"\s+", " ", selected).strip()
        if normalized_evidence not in normalized_selected:
            return None

        for line_number in range(draft.start_line, draft.end_line + 1):
            line = lines[line_number - 1]
            column = line.find(evidence)
            if column >= 0:
                return (
                    line_number,
                    column + 1,
                    line_number,
                    column + len(evidence) + 1,
                )

        first_token = evidence.splitlines()[0].strip()
        for line_number in range(draft.start_line, draft.end_line + 1):
            line = lines[line_number - 1]
            column = line.find(first_token)
            if column >= 0:
                end_line = min(draft.end_line, line_number + evidence.count("\n"))
                final_token = evidence.splitlines()[-1].strip()
                final_line = lines[end_line - 1]
                final_column = final_line.find(final_token)
                if final_column >= 0:
                    return line_number, column + 1, end_line, final_column + len(final_token) + 1
        return None


def merge_findings(static_findings: list[Finding], llm_findings: list[Finding]) -> list[Finding]:
    merged: list[Finding] = []
    for candidate in [*static_findings, *llm_findings]:
        duplicate_index = next(
            (index for index, existing in enumerate(merged) if _same_problem(existing, candidate)),
            None,
        )
        if duplicate_index is None:
            merged.append(candidate)
            continue
        existing = merged[duplicate_index]
        preferred = existing if existing.source == "static" else candidate
        preferred = preferred.model_copy(
            update={
                "verification": preferred.verification.model_copy(update={"deduplicated": True})
            }
        )
        merged[duplicate_index] = preferred
    return sorted(
        merged,
        key=lambda item: (item.file_id, item.start_line, item.start_column, item.rule_id),
    )


def _same_problem(left: Finding, right: Finding) -> bool:
    if left.file_id != right.file_id:
        return False
    ranges_overlap = left.start_line <= right.end_line and right.start_line <= left.end_line
    if not ranges_overlap:
        return False
    left_text = f"{left.rule_id} {left.category} {left.title} {left.evidence}".lower()
    right_text = f"{right.rule_id} {right.category} {right.title} {right.evidence}".lower()
    same_evidence = left.evidence.strip() == right.evidence.strip()
    same_category = left.category == right.category
    security_match = any(
        marker in left_text and marker in right_text for marker in _HIGH_RISK_MARKERS
    )
    return same_evidence or (same_category and security_match)

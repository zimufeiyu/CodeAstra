from __future__ import annotations

from dataclasses import dataclass

from code_review.application.evidence_validation import merge_findings
from code_review.domain.review_chunks import ChunkStatus, ReviewChunk, ReviewPlanningError
from code_review.domain.review_models import Finding, ReviewSession, ReviewSummary


@dataclass(frozen=True)
class AggregateResult:
    findings: list[Finding]
    summary: ReviewSummary
    coverage_percent: float


class ReviewAggregator:
    def aggregate(
        self,
        session: ReviewSession,
        chunks: list[ReviewChunk],
        *,
        static_findings: list[Finding],
        chunk_findings: list[Finding],
        significant_lines: dict[str, set[int]],
    ) -> AggregateResult:
        del session
        leaves = [item for item in chunks if item.status != ChunkStatus.SUPERSEDED]
        unresolved = [item.chunk_id for item in leaves if item.status != ChunkStatus.COMPLETED]
        if unresolved:
            raise ReviewPlanningError(
                code="coverage_incomplete",
                message="仍有未完成或失败的审查分块。",
                chunk_ids=unresolved,
            )
        required = sum(len(lines) for lines in significant_lines.values())
        covered_count = 0
        for file_id, lines in significant_lines.items():
            covered = {
                line
                for item in leaves
                if item.target_file_id == file_id
                for line in range(item.target_start_line, item.target_end_line + 1)
            }
            missing = lines - covered
            if missing:
                raise ReviewPlanningError(
                    code="coverage_incomplete",
                    message="审查覆盖率不足 100%。",
                    file_id=file_id,
                    lines=sorted(missing),
                )
            covered_count += len(lines & covered)
        coverage = 100.0 if required == 0 else covered_count * 100.0 / required
        if coverage != 100.0:
            raise ReviewPlanningError(
                code="coverage_incomplete",
                message="审查覆盖率不足 100%。",
            )
        findings = merge_findings(static_findings, chunk_findings)
        return AggregateResult(
            findings=findings,
            summary=self._summary(findings),
            coverage_percent=coverage,
        )

    @staticmethod
    def _summary(findings: list[Finding]) -> ReviewSummary:
        counts = {
            level: sum(item.severity == level for item in findings)
            for level in ("critical", "high", "medium", "low", "info")
        }
        if findings:
            labels = {
                "critical": "严重",
                "high": "高危",
                "medium": "中危",
                "low": "低危",
                "info": "提示",
            }
            distribution = "、".join(
                f"{labels[level]} {count} 项" for level, count in counts.items() if count
            )
            text = f"审查完成，共发现 {len(findings)} 个明确问题（{distribution}）。"
        else:
            text = "审查完成，未发现通过证据校验的明确问题。"
        return ReviewSummary(total=len(findings), text=text, **counts)

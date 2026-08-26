from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from code_review.domain.review_models import Finding


class SemanticJudgeDecision(BaseModel):
    accepted: bool
    confidence: float
    reason: str


class SemanticJudgePort(Protocol):
    async def verify(self, finding: Finding) -> SemanticJudgeDecision: ...


class BudgetedSemanticJudge:
    """Independent optional judge. Disabled by default, so normal reviews add no calls."""

    def __init__(self, judge: SemanticJudgePort | None, *, max_calls_per_review: int = 8) -> None:
        self._judge = judge
        self._remaining = max(0, max_calls_per_review)
        self._calls = 0

    @property
    def available(self) -> bool:
        return self._judge is not None and self._remaining > 0

    @property
    def calls(self) -> int:
        return self._calls

    async def verify(self, finding: Finding) -> SemanticJudgeDecision:
        if finding.source != "llm":
            return SemanticJudgeDecision(
                accepted=True, confidence=1.0, reason="deterministic findings bypass semantic judge"
            )
        if not self.available or self._judge is None:
            return SemanticJudgeDecision(
                accepted=True, confidence=finding.confidence, reason="independent judge disabled"
            )
        self._remaining -= 1
        self._calls += 1
        return await self._judge.verify(finding)

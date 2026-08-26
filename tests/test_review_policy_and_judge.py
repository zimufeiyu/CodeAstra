import pytest

from code_review.application.review_policy import (
    ReviewPolicy,
    ReviewPolicyEngine,
    ReviewPolicyRule,
    suggest_policy_changes,
)
from code_review.application.semantic_judge import BudgetedSemanticJudge, SemanticJudgeDecision
from code_review.domain.review_models import Finding


def finding(finding_id: str, *, source: str = "llm") -> Finding:
    return Finding(
        finding_id=finding_id,
        source=source,
        analyzer="test",
        rule_id="semantic.contract",
        category="correctness",
        severity="medium",
        confidence=0.9,
        file_id="file",
        relative_path="pkg/model.py",
        start_line=1,
        start_column=1,
        end_line=1,
        end_column=2,
        title="契约错误",
        hover_summary="契约错误",
        detail="契约错误",
        evidence="value",
        impact="impact",
        suggestion="fix",
    )


def test_versioned_policy_applies_scope_severity_and_never_auto_learns():
    policy = ReviewPolicy(
        policy_id="default",
        version=2,
        approved_by="admin",
        change_reason="explicit review",
        rules=[
            ReviewPolicyRule(
                rule_id="semantic.contract",
                severity="high",
                include_paths=["pkg/**"],
                allowed_fix_safety=["safe"],
            )
        ],
    )
    revised = ReviewPolicyEngine(policy).apply(finding("one"))
    assert revised.visible and revised.finding.severity == "high"
    assert revised.finding.applicability == "requires_review"
    suggestions = suggest_policy_changes([finding("one")], {"one": "dismissed"})
    assert suggestions[0].requires_human_approval
    assert policy.rules[0].enabled is True


class FakeJudge:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, finding):
        self.calls += 1
        return SemanticJudgeDecision(accepted=False, confidence=0.4, reason="unverified")


@pytest.mark.asyncio
async def test_independent_judge_only_sees_llm_findings_and_has_a_call_budget():
    port = FakeJudge()
    judge = BudgetedSemanticJudge(port, max_calls_per_review=1)
    static = await judge.verify(finding("static", source="static"))
    rejected = await judge.verify(finding("semantic"))
    budget_exhausted = await judge.verify(finding("second"))
    assert static.accepted and not rejected.accepted and budget_exhausted.accepted
    assert port.calls == 1
    assert judge.calls == 1

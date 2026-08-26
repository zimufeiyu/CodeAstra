from __future__ import annotations

import argparse
import json
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from code_review.domain.model_protocol import SeverityLevel
from code_review.domain.review_models import Finding


class ReviewPolicyRule(BaseModel):
    rule_id: str
    enabled: bool = True
    severity: SeverityLevel | None = None
    include_paths: list[str] = Field(default_factory=lambda: ["**"])
    exclude_paths: list[str] = Field(default_factory=list)
    allowed_fix_safety: list[str] = Field(
        default_factory=lambda: ["safe", "requires_review"]
    )


class ReviewPolicy(BaseModel):
    policy_id: str
    version: int = Field(ge=1)
    rules: list[ReviewPolicyRule] = Field(default_factory=list)
    approved_by: str
    change_reason: str


class PolicyDecision(BaseModel):
    visible: bool
    reason: str
    finding: Finding


class ReviewPolicyEngine:
    def __init__(self, policy: ReviewPolicy | None = None) -> None:
        self.policy = policy

    def apply(self, finding: Finding) -> PolicyDecision:
        if self.policy is None:
            return PolicyDecision(visible=True, reason="default policy", finding=finding)
        path = finding.relative_path or finding.file_id
        rule = next((item for item in self.policy.rules if item.rule_id == finding.rule_id), None)
        if rule is None:
            return PolicyDecision(visible=True, reason="rule not overridden", finding=finding)
        in_scope = any(fnmatch(path, pattern) for pattern in rule.include_paths) and not any(
            fnmatch(path, pattern) for pattern in rule.exclude_paths
        )
        if not rule.enabled or not in_scope:
            return PolicyDecision(
                visible=False,
                reason="disabled or out of policy scope",
                finding=finding,
            )
        if finding.fix_safety not in rule.allowed_fix_safety:
            revised = finding.model_copy(update={"applicability": "requires_review"})
        else:
            revised = finding
        if rule.severity is not None:
            revised = revised.model_copy(update={"severity": rule.severity})
        return PolicyDecision(visible=True, reason="versioned policy applied", finding=revised)

    def filter(self, findings: list[Finding]) -> list[Finding]:
        decisions = [self.apply(item) for item in findings]
        return [item.finding for item in decisions if item.visible]


class PolicySuggestion(BaseModel):
    rule_id: str
    dismissed_count: int = Field(ge=1)
    suggestion: str
    requires_human_approval: bool = True


def suggest_policy_changes(
    findings: list[Finding], decisions: dict[str, str]
) -> list[PolicySuggestion]:
    counts: dict[str, int] = {}
    by_id = {item.finding_id: item for item in findings}
    for finding_id, decision in decisions.items():
        finding = by_id.get(finding_id)
        if decision == "dismissed" and finding is not None:
            counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    return [
        PolicySuggestion(
            rule_id=rule_id,
            dismissed_count=count,
            suggestion="审查该规则的证据门槛；不会自动停用或修改严重度。",
        )
        for rule_id, count in sorted(counts.items())
    ]


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate a versioned CodeAstra review policy")
    parser.add_argument("policy", type=Path)
    arguments = parser.parse_args()
    policy = ReviewPolicy.model_validate_json(arguments.policy.read_text(encoding="utf-8"))
    print(json.dumps(policy.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

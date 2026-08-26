from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from code_review.domain.model_protocol import ModelSelection, SeverityLevel

BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class ReviewMode(StrEnum):
    PASTE = "paste"
    SINGLE = "single"
    PROJECT = "project"


Language = Literal["python", "cpp"]
ReviewStatus = Literal[
    "planning",
    "queued",
    "analyzing",
    "reviewing",
    "validating",
    "aggregating",
    "completed",
    "cancelled",
    "failed",
]
FindingSource = Literal["static", "llm", "merged"]
FindingDecision = Literal["fixed", "accepted_risk", "deferred", "dismissed"]


class FindingDecisionAudit(BaseModel):
    finding_id: str = Field(min_length=1)
    action: Literal["decided", "reopened"]
    decision: FindingDecision | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    reason: str = Field(min_length=1, max_length=2000)
    revision_retained: bool = False


ReviewEventType = Literal["stage", "chunk", "progress", "finding", "complete", "error", "cancelled"]


class SourceFile(BaseModel):
    file_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    language: Language
    content: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line_offsets: list[int]

    @classmethod
    def from_content(
        cls,
        *,
        file_id: str,
        relative_path: str,
        language: Language,
        content: str,
    ) -> SourceFile:
        offsets = [0]
        offsets.extend(index + 1 for index, character in enumerate(content) if character == "\n")
        return cls(
            file_id=file_id,
            relative_path=relative_path,
            language=language,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            line_offsets=offsets,
        )


class FindingVerification(BaseModel):
    range_valid: bool = False
    evidence_matched: bool = False
    static_confirmed: bool = False
    cross_file_checked: bool = False
    deduplicated: bool = False


RepairIntentKind = Literal[
    "rename_existing",
    "declare_parameter",
    "declare_local",
    "import_symbol",
    "custom_behavior",
    "defer",
]


class SymbolCandidate(BaseModel):
    name: str
    kind: Literal["parameter", "import", "assignment", "binding", "export"]
    confidence: float = Field(ge=0.0, le=1.0)
    relative_path: str | None = None
    line: int | None = Field(default=None, ge=1)
    rationale: str


class RepairIntentOption(BaseModel):
    option_id: str
    kind: RepairIntentKind
    label: str
    symbol: str | None = None
    module: str | None = None
    requires_input: Literal["initializer", "module", "behavior", "none"] = "none"
    input_label: str | None = None


class UseDefEvidence(BaseModel):
    unresolved_name: str
    scope_kind: str
    scope_symbol: str | None = None
    use_node_kind: str = "Name"
    use_line: int = Field(ge=1)
    use_column: int = Field(ge=1)
    statement_kind: str
    statement_start_line: int = Field(ge=1)
    statement_end_line: int = Field(ge=1)
    statement_text: str
    visible_parameters: list[str] = Field(default_factory=list)
    visible_imports: list[str] = Field(default_factory=list)
    visible_assignments: list[str] = Field(default_factory=list)
    similar_candidates: list[SymbolCandidate] = Field(default_factory=list)
    cross_file_exports: list[SymbolCandidate] = Field(default_factory=list)
    control_flow_reachability: Literal["reachable", "conditional", "unknown"] = "unknown"
    outcome: Literal["safe_plan", "needs_intent"] = "needs_intent"
    explanation: str
    options: list[RepairIntentOption] = Field(default_factory=list)


class SymbolRepairPlan(BaseModel):
    mode: Literal["rename", "import", "declare_parameter", "declare_local"]
    unresolved_name: str
    replacement_name: str | None = None
    module: str | None = None
    initializer: str | None = None
    definition_symbol: str | None = None
    statement_start_line: int = Field(ge=1)
    statement_end_line: int = Field(ge=1)
    use_line: int = Field(ge=1)
    use_column: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    safety: Literal["safe", "requires_review"]
    user_selected: bool = False


class RepairIntentSelection(BaseModel):
    review_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    option_id: str = Field(min_length=1)
    intent_kind: RepairIntentKind
    selected_symbol: str | None = None
    import_source: str | None = None
    initializer: str | None = None
    user_intent: str | None = Field(default=None, min_length=1, max_length=2000)


class RootCauseEvidence(BaseModel):
    claim: str
    concrete_failing_input: str
    expected_behavior: str
    actual_behavior: str
    affected_path: str
    repair_invariant: str
    contract_evidence: str | None = None
    reachable_path: str | None = None


class RenameCallsite(BaseModel):
    relative_path: str
    line: int = Field(ge=1)
    keyword: str


class RenamePlan(BaseModel):
    old_name: str
    new_name: str
    definition_symbol: str
    affected_keyword_callsites: list[RenameCallsite] = Field(default_factory=list)
    scope: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    safety: Literal["safe", "unsafe", "requires_review"] = "requires_review"
    executable: bool = False
    unsafe_reasons: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str = Field(min_length=1)
    source: FindingSource
    analyzer: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    severity: SeverityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    file_id: str = Field(min_length=1)
    relative_path: str | None = None
    start_line: int = Field(ge=1)
    start_column: int = Field(ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1)
    title: str = Field(min_length=1)
    hover_summary: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    verification: FindingVerification = Field(default_factory=FindingVerification)
    analyzer_id: str | None = None
    source_kind: Literal["deterministic_fact", "model_hypothesis", "verified_hypothesis"] | None = (
        None
    )
    applicability: Literal["applicable", "requires_review", "unavailable"] = "requires_review"
    fingerprint: str = ""
    symbol: str | None = None
    fix_safety: Literal["safe", "unsafe", "requires_review"] = "requires_review"
    verifier: str | None = None
    verification_reason: str | None = None
    rename_plan: RenamePlan | None = None
    use_def_evidence: UseDefEvidence | None = None
    symbol_repair_plan: SymbolRepairPlan | None = None
    root_cause_claim: RootCauseEvidence | None = None

    @model_validator(mode="after")
    def validate_source_range(self) -> Finding:
        start = (self.start_line, self.start_column)
        end = (self.end_line, self.end_column)
        if end < start:
            raise ValueError("end position must not precede start position")
        if self.analyzer_id is None:
            self.analyzer_id = self.analyzer
        if self.source_kind is None:
            self.source_kind = (
                "deterministic_fact"
                if self.source == "static"
                else "verified_hypothesis"
                if self.verification.range_valid and self.verification.evidence_matched
                else "model_hypothesis"
            )
        if not self.fingerprint:
            normalized = " ".join(self.evidence.casefold().split())
            self.fingerprint = hashlib.sha256(
                "\x1f".join(
                    (
                        (self.relative_path or self.file_id).casefold(),
                        self.rule_id.casefold(),
                        self.symbol or "",
                        normalized,
                    )
                ).encode()
            ).hexdigest()
        return self


class CoverageState(BaseModel):
    language: Language
    analyzer: str
    available: bool
    message: str


class ReviewSummary(BaseModel):
    total: int = Field(default=0, ge=0)
    critical: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)
    low: int = Field(default=0, ge=0)
    info: int = Field(default=0, ge=0)
    text: str = "等待审查。"


class ChangedLineRange(BaseModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> ChangedLineRange:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class GitLabReviewOrigin(BaseModel):
    type: Literal["gitlab"] = "gitlab"
    gitlab_host: str = Field(min_length=1)
    project_id: int = Field(ge=1)
    project_path: str = Field(min_length=1)
    merge_request_iid: int = Field(ge=1)
    merge_request_url: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    selected_paths: list[str] = Field(default_factory=list)
    changed_ranges: dict[str, list[ChangedLineRange]] | None = None


class LocalDiffReviewOrigin(BaseModel):
    type: Literal["local_diff"] = "local_diff"
    old_label: str = Field(min_length=1)
    new_label: str = Field(min_length=1)
    selected_paths: list[str] = Field(default_factory=list)
    changed_ranges: dict[str, list[ChangedLineRange]] | None = None
    old_sha256: dict[str, str] = Field(default_factory=dict)
    new_sha256: dict[str, str] = Field(default_factory=dict)


ReviewOrigin = Annotated[
    GitLabReviewOrigin | LocalDiffReviewOrigin,
    Field(discriminator="type"),
]


class ReviewRevision(BaseModel):
    revision_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    file_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    created_at: datetime
    before_content: str
    before_changed_ranges: list[ChangedLineRange] | None = None
    before_sha256: str = Field(default="0" * 64, min_length=64, max_length=64)
    after_sha256: str = Field(min_length=64, max_length=64)
    diff: str
    explanation: str | None = None
    validation: list[str] = Field(default_factory=list)
    undone_at: datetime | None = None


class FixCandidate(BaseModel):
    candidate_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    file_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime
    base_sha256: str = Field(min_length=64, max_length=64)
    after_sha256: str = Field(min_length=64, max_length=64)
    diff: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    validation: list[str] = Field(default_factory=list)
    output_token_budget: int = Field(ge=128)
    finding_state: Literal["candidate_ready"] = "candidate_ready"
    fix_safety: Literal["safe", "requires_review"] = "requires_review"


class ReviewSession(BaseModel):
    review_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1, exclude=True)
    mode: ReviewMode
    status: ReviewStatus = "queued"
    created_at: datetime
    expires_at: datetime
    title: str | None = None
    model: ModelSelection = Field(
        default_factory=lambda: ModelSelection(
            profile_id="local-qwen3-8b",
            provider="local",
            model="Qwen3-8B",
            display_name="\u672c\u5730 Qwen3-8B",
        )
    )
    origin: ReviewOrigin | None = None
    files: list[SourceFile]
    findings: list[Finding] = Field(default_factory=list)
    coverage: list[CoverageState] = Field(default_factory=list)
    summary: ReviewSummary = Field(default_factory=ReviewSummary)
    error: str | None = None
    error_code: str | None = None
    recheck_attempt_id: str | None = None
    recheck_attempt_status: Literal["running", "completed", "failed", "timed_out"] | None = None
    recheck_deadline_at: datetime | None = None
    finding_decisions: dict[str, FindingDecision] = Field(default_factory=dict)
    decided_findings: dict[str, Finding] = Field(default_factory=dict)
    finding_decision_history: list[FindingDecisionAudit] = Field(default_factory=list)
    ignored_finding_fingerprints: list[str] = Field(default_factory=list)
    revisions: list[ReviewRevision] = Field(default_factory=list)
    finding_states: dict[
        str,
        Literal[
            "active",
            "candidate_ready",
            "fixed_pending_revalidation",
            "fixed_verified",
            "accepted_risk",
            "deferred",
            "dismissed",
            "reopened",
        ],
    ] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_finding_decisions(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        decisions = value.get("finding_decisions")
        if isinstance(decisions, dict):
            value = dict(value)
            value["finding_decisions"] = {
                key: "fixed"
                if decision == "apply"
                else "accepted_risk"
                if decision == "keep"
                else decision
                for key, decision in decisions.items()
            }
        return value

    def display_title(self) -> str:
        if self.title and self.title.strip():
            return self.title.strip()
        if self.mode == ReviewMode.SINGLE and self.files:
            return self.files[0].relative_path
        if self.mode == ReviewMode.PROJECT:
            first_path = self.files[0].relative_path if self.files else "未命名项目"
            return f"项目审查 · {first_path} 等 {len(self.files)} 个文件"
        created_at = self.created_at.astimezone(BEIJING_TIMEZONE)
        return f"代码审查 · {created_at:%Y-%m-%d %H:%M}"

    @classmethod
    def create(
        cls,
        *,
        review_id: str,
        owner_id: str,
        mode: ReviewMode,
        files: list[SourceFile],
        origin: ReviewOrigin | None = None,
        model: ModelSelection | None = None,
        created_at: datetime | None = None,
        retention_hours: int | None = None,
    ) -> ReviewSession:
        created = created_at or datetime.now(tz=UTC)
        expires_at = (
            created + timedelta(hours=retention_hours)
            if retention_hours is not None
            else datetime.max.replace(tzinfo=UTC)
        )
        return cls(
            review_id=review_id,
            owner_id=owner_id,
            mode=mode,
            created_at=created,
            expires_at=expires_at,
            origin=origin,
            model=model
            or ModelSelection(
                profile_id="local-qwen3-8b",
                provider="local",
                model="Qwen3-8B",
                display_name="\u672c\u5730 Qwen3-8B",
            ),
            files=files,
        )


class ReviewEvent(BaseModel):
    sequence: int = Field(ge=1)
    event: ReviewEventType
    data: dict[str, object]


class FollowupMessage(BaseModel):
    message_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    role: Literal["user", "assistant"]
    context_key: str = Field(default="review", min_length=1, max_length=300)
    content: str = Field(min_length=1)
    created_at: datetime

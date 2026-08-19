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


class Finding(BaseModel):
    finding_id: str = Field(min_length=1)
    source: FindingSource
    analyzer: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    severity: SeverityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    file_id: str = Field(min_length=1)
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

    @model_validator(mode="after")
    def validate_source_range(self) -> Finding:
        start = (self.start_line, self.start_column)
        end = (self.end_line, self.end_column)
        if end < start:
            raise ValueError("end position must not precede start position")
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
    after_sha256: str = Field(min_length=64, max_length=64)
    diff: str
    explanation: str | None = None
    undone_at: datetime | None = None


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
    finding_decisions: dict[str, Literal["apply", "keep"]] = Field(default_factory=dict)
    ignored_finding_fingerprints: list[str] = Field(default_factory=list)
    revisions: list[ReviewRevision] = Field(default_factory=list)

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



from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from code_review.domain.review_models import Finding, Language


class ChunkStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ChunkErrorCode(StrEnum):
    PLANNING_ERROR = "planning_error"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_ERROR = "model_error"
    INVALID_OUTPUT = "invalid_output"
    VALIDATION_ERROR = "validation_error"
    CANCELLED = "cancelled"
    LOCAL_MODEL_CONNECTION_REFUSED = "local_model_connection_refused"
    LOCAL_MODEL_TIMEOUT = "local_model_timeout"
    LOCAL_MODEL_CIRCUIT_OPEN = "local_model_circuit_open"


class ContextReference(BaseModel):
    file_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    code: str
    reason: str = Field(min_length=1)
    rank: int = Field(default=100, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> ContextReference:
        if self.end_line < self.start_line:
            raise ValueError("end line must not precede start line")
        return self


class ReviewChunk(BaseModel):
    chunk_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    language: Language
    target_file_id: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    target_start_line: int = Field(ge=1)
    target_end_line: int = Field(ge=1)
    target_code: str
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_references: list[ContextReference] = Field(default_factory=list)
    prompt_tokens_estimate: int = Field(default=0, ge=0)
    output_tokens_budget: int = Field(default=0, ge=0)
    parent_chunk_id: str | None = None
    split_depth: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    status: ChunkStatus = ChunkStatus.PENDING
    error_code: ChunkErrorCode | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_target_range(self) -> ReviewChunk:
        if self.target_end_line < self.target_start_line:
            raise ValueError("target end line must not precede start line")
        return self


class ChunkAttempt(BaseModel):
    attempt_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    strategy: Literal["original", "trim_context", "split_target"]
    endpoint_id: str | None = None
    request_id: str = Field(min_length=1)
    prompt_tokens: int = Field(default=0, ge=0)
    output_tokens_budget: int = Field(default=0, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    error_code: ChunkErrorCode | None = None
    error_message: str | None = None


class ChunkProgress(BaseModel):
    total: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    coverage_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    active_file: str | None = None
    active_start_line: int | None = Field(default=None, ge=1)
    active_end_line: int | None = Field(default=None, ge=1)


class ReviewPlan(BaseModel):
    review_id: str
    chunks: list[ReviewChunk]
    significant_lines: dict[str, set[int]] = Field(default_factory=dict)
    zero_target_file_ids: set[str] = Field(default_factory=set)

    def leaf_chunks(self) -> list[ReviewChunk]:
        return [chunk for chunk in self.chunks if chunk.status != ChunkStatus.SUPERSEDED]

    def assert_complete(self, findings: list[Finding] | None = None) -> None:
        del findings
        unresolved = [
            chunk.chunk_id for chunk in self.leaf_chunks() if chunk.status != ChunkStatus.COMPLETED
        ]
        if unresolved:
            raise ReviewPlanningError(
                code="coverage_incomplete",
                message="???????????",
                chunk_ids=unresolved,
            )


class ReviewPlanningError(RuntimeError):
    def __init__(
        self,
        code: str = "planning_error",
        message: str = "???????????",
        *,
        file_id: str | None = None,
        lines: list[int] | None = None,
        chunk_ids: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.file_id = file_id
        self.lines = lines or []
        self.chunk_ids = chunk_ids or []

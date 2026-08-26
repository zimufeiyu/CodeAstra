from __future__ import annotations

from typing import Protocol

from code_review.domain.model_protocol import FixProposal, InferenceRequest, ReviewResponse
from code_review.domain.review_chunks import ChunkAttempt, ReviewChunk
from code_review.domain.review_models import (
    Finding,
    FollowupMessage,
    ReviewEvent,
    ReviewEventType,
    ReviewSession,
    SourceFile,
)


class ReviewStorePort(Protocol):
    async def create(self, session: ReviewSession) -> None: ...
    async def get(self, review_id: str, owner_id: str) -> ReviewSession | None: ...
    async def save(self, session: ReviewSession) -> None: ...
    async def update_title(self, review_id: str, owner_id: str, title: str) -> ReviewSession: ...
    async def reset_review(self, session: ReviewSession) -> None: ...
    async def list_sessions(
        self, owner_id: str, limit: int, offset: int
    ) -> list[ReviewSession]: ...
    async def delete(self, review_id: str, owner_id: str) -> bool: ...
    async def save_chunks(self, chunks: list[ReviewChunk], owner_id: str) -> None: ...
    async def chunks(self, review_id: str, owner_id: str) -> list[ReviewChunk]: ...
    async def save_chunk(self, chunk: ReviewChunk, owner_id: str) -> None: ...
    async def replace_chunk(
        self, parent: ReviewChunk, children: list[ReviewChunk], owner_id: str
    ) -> None: ...
    async def save_chunk_findings(
        self, chunk_id: str, owner_id: str, findings: list[Finding]
    ) -> None: ...
    async def chunk_findings(self, review_id: str, owner_id: str) -> list[Finding]: ...
    async def record_attempt(self, attempt: ChunkAttempt, owner_id: str) -> None: ...
    async def publish(
        self,
        review_id: str,
        owner_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent: ...
    async def events_after(
        self, review_id: str, owner_id: str, after: int
    ) -> list[ReviewEvent]: ...
    async def recoverable_reviews(self) -> list[tuple[str, str]]: ...
    async def followups(
        self, review_id: str, owner_id: str, context_key: str = "review"
    ) -> list[FollowupMessage]: ...
    async def append_followup_exchange(
        self,
        question: FollowupMessage,
        answer: FollowupMessage,
        owner_id: str,
        context_key: str = "review",
    ) -> None: ...
    async def transition_chunk(
        self,
        chunk: ReviewChunk,
        owner_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent: ...
    async def transition_review(
        self,
        session: ReviewSession,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent: ...
    async def transition_review_if_recheck_attempt(
        self,
        session: ReviewSession,
        attempt_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> bool: ...
    async def save_review_if_recheck_attempt(
        self, session: ReviewSession, attempt_id: str
    ) -> bool: ...
    async def is_recheck_attempt_current(
        self, review_id: str, owner_id: str, attempt_id: str
    ) -> bool: ...


class ReviewInferencePort(Protocol):
    async def review(self, request: InferenceRequest) -> ReviewResponse: ...
    async def answer_followup(self, request: InferenceRequest) -> str: ...
    async def propose_fix(self, request: InferenceRequest) -> FixProposal: ...


class SyntaxChunkerPort(Protocol):
    def split(
        self,
        review_id: str,
        source: SourceFile,
        *,
        parent_chunk_id: str | None = None,
        split_depth: int = 0,
    ) -> list[ReviewChunk]: ...


class TokenEstimatorPort(Protocol):
    def estimate_text(self, content: str) -> int: ...


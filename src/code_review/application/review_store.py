from __future__ import annotations

import asyncio
from datetime import datetime

from code_review.domain.review_chunks import ChunkAttempt, ChunkStatus, ReviewChunk
from code_review.domain.review_models import (
    Finding,
    FollowupMessage,
    ReviewEvent,
    ReviewEventType,
    ReviewSession,
)


class InMemoryReviewStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ReviewSession] = {}
        self._events: dict[str, list[ReviewEvent]] = {}
        self._followups: dict[tuple[str, str], list[FollowupMessage]] = {}
        self._lock = asyncio.Lock()

    def _require_owner(self, review_id: str, owner_id: str) -> ReviewSession:
        session = self._sessions.get(review_id)
        if session is None or session.owner_id != owner_id:
            raise KeyError(review_id)
        return session

    async def create(self, session: ReviewSession) -> None:
        async with self._lock:
            if session.review_id in self._sessions:
                raise ValueError(f"review already exists: {session.review_id}")
            self._sessions[session.review_id] = session.model_copy(deep=True)
            self._events[session.review_id] = []
            self._followups[(session.review_id, "review")] = []

    async def get(self, review_id: str, owner_id: str) -> ReviewSession | None:
        async with self._lock:
            session = self._sessions.get(review_id)
            return session.model_copy(deep=True) if session is not None and session.owner_id == owner_id else None

    async def save(self, session: ReviewSession) -> None:
        async with self._lock:
            current = self._require_owner(session.review_id, session.owner_id)
            persisted = session.model_copy(update={"title": current.title})
            self._sessions[session.review_id] = persisted.model_copy(deep=True)

    async def update_title(self, review_id: str, owner_id: str, title: str) -> ReviewSession:
        async with self._lock:
            current = self._require_owner(review_id, owner_id)
            renamed = current.model_copy(update={"title": title})
            self._sessions[review_id] = renamed.model_copy(deep=True)
            return renamed.model_copy(deep=True)

    async def list_sessions(self, owner_id: str, limit: int, offset: int) -> list[ReviewSession]:
        async with self._lock:
            sessions = sorted(
                (item for item in self._sessions.values() if item.owner_id == owner_id),
                key=lambda item: (item.created_at, item.review_id),
                reverse=True,
            )
            return [item.model_copy(deep=True) for item in sessions[offset : offset + limit]]

    async def publish(
        self,
        review_id: str,
        owner_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        async with self._lock:
            self._require_owner(review_id, owner_id)
            review_event = ReviewEvent(
                sequence=len(self._events[review_id]) + 1,
                event=event,
                data=data,
            )
            self._events[review_id].append(review_event)
            return review_event.model_copy(deep=True)

    async def events_after(
        self, review_id: str, owner_id: str, after: int
    ) -> list[ReviewEvent]:
        async with self._lock:
            self._require_owner(review_id, owner_id)
            return [
                event.model_copy(deep=True)
                for event in self._events[review_id]
                if event.sequence > after
            ]

    async def followups(self, review_id: str, owner_id: str, context_key: str = "review") -> list[FollowupMessage]:
        async with self._lock:
            self._require_owner(review_id, owner_id)
            return [item.model_copy(deep=True) for item in self._followups.get((review_id, context_key), [])]

    async def append_followup_exchange(
        self,
        question: FollowupMessage,
        answer: FollowupMessage,
        owner_id: str,
        context_key: str = "review",
    ) -> None:
        if question.review_id != answer.review_id:
            raise ValueError("follow-up messages must belong to the same review")
        if question.role != "user" or answer.role != "assistant":
            raise ValueError("follow-up exchange must contain a user question and assistant answer")
        async with self._lock:
            self._require_owner(question.review_id, owner_id)
            if question.context_key != context_key or answer.context_key != context_key:
                raise ValueError("follow-up messages must use the derived context key")
            self._followups.setdefault((question.review_id, context_key), []).extend(
                [question.model_copy(deep=True), answer.model_copy(deep=True)]
            )

    def _delete_review_locked(self, review_id: str) -> None:
        self._sessions.pop(review_id, None)
        self._events.pop(review_id, None)
        for key in [key for key in self._followups if key[0] == review_id]:
            self._followups.pop(key, None)

    async def delete_expired(self, now: datetime) -> list[str]:
        async with self._lock:
            expired = [
                review_id
                for review_id, session in self._sessions.items()
                if session.expires_at <= now
            ]
            for review_id in expired:
                self._delete_review_locked(review_id)
            return expired

    async def delete(self, review_id: str, owner_id: str) -> bool:
        async with self._lock:
            current = self._sessions.get(review_id)
            if current is None or current.owner_id != owner_id:
                return False
            self._delete_review_locked(review_id)
            return True


class EnhancedInMemoryReviewStore(InMemoryReviewStore):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: dict[str, ReviewChunk] = {}
        self._chunk_findings: dict[str, list[Finding]] = {}
        self._attempts: dict[str, ChunkAttempt] = {}

    async def reset_review(self, session: ReviewSession) -> None:
        async with self._lock:
            current = self._require_owner(session.review_id, session.owner_id)
            persisted = session.model_copy(update={"title": current.title})
            self._sessions[session.review_id] = persisted.model_copy(deep=True)
            self._events[session.review_id] = []
            chunk_ids = {
                chunk_id
                for chunk_id, chunk in self._chunks.items()
                if chunk.review_id == session.review_id
            }
            for chunk_id in chunk_ids:
                self._chunks.pop(chunk_id, None)
                self._chunk_findings.pop(chunk_id, None)
            self._attempts = {
                attempt_id: attempt
                for attempt_id, attempt in self._attempts.items()
                if attempt.review_id != session.review_id
            }

    async def save_chunks(self, chunks: list[ReviewChunk], owner_id: str) -> None:
        async with self._lock:
            for chunk in chunks:
                self._require_owner(chunk.review_id, owner_id)
                existing = self._chunks.get(chunk.chunk_id)
                if existing is not None and existing.review_id != chunk.review_id:
                    raise KeyError(chunk.chunk_id)
            for chunk in chunks:
                self._chunks[chunk.chunk_id] = chunk.model_copy(deep=True)

    async def chunks(self, review_id: str, owner_id: str) -> list[ReviewChunk]:
        async with self._lock:
            self._require_owner(review_id, owner_id)
            return [
                item.model_copy(deep=True)
                for item in sorted(self._chunks.values(), key=lambda value: value.chunk_id)
                if item.review_id == review_id
            ]

    async def save_chunk(self, chunk: ReviewChunk, owner_id: str) -> None:
        await self.save_chunks([chunk], owner_id)

    async def replace_chunk(
        self,
        parent: ReviewChunk,
        children: list[ReviewChunk],
        owner_id: str,
    ) -> None:
        if any(child.review_id != parent.review_id for child in children):
            raise ValueError("replacement chunks must belong to the parent review")
        superseded = parent.model_copy(update={"status": ChunkStatus.SUPERSEDED})
        await self.save_chunks([superseded, *children], owner_id)

    async def save_chunk_findings(
        self,
        chunk_id: str,
        owner_id: str,
        findings: list[Finding],
    ) -> None:
        async with self._lock:
            chunk = self._chunks.get(chunk_id)
            if chunk is None:
                raise KeyError(chunk_id)
            self._require_owner(chunk.review_id, owner_id)
            self._chunk_findings[chunk_id] = [item.model_copy(deep=True) for item in findings]

    async def chunk_findings(self, review_id: str, owner_id: str) -> list[Finding]:
        async with self._lock:
            self._require_owner(review_id, owner_id)
            chunk_ids = {
                item.chunk_id for item in self._chunks.values() if item.review_id == review_id
            }
            return [
                finding.model_copy(deep=True)
                for chunk_id in sorted(chunk_ids)
                for finding in self._chunk_findings.get(chunk_id, [])
            ]

    async def record_attempt(self, attempt: ChunkAttempt, owner_id: str) -> None:
        async with self._lock:
            self._require_owner(attempt.review_id, owner_id)
            chunk = self._chunks.get(attempt.chunk_id)
            if chunk is None or chunk.review_id != attempt.review_id:
                raise KeyError(attempt.chunk_id)
            existing = self._attempts.get(attempt.attempt_id)
            if existing is not None and existing.review_id != attempt.review_id:
                raise KeyError(attempt.attempt_id)
            self._attempts[attempt.attempt_id] = attempt.model_copy(deep=True)

    async def transition_chunk(
        self,
        chunk: ReviewChunk,
        owner_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        await self.save_chunk(chunk, owner_id)
        return await self.publish(chunk.review_id, owner_id, event, data)

    async def transition_review(
        self,
        session: ReviewSession,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        await self.save(session)
        return await self.publish(session.review_id, session.owner_id, event, data)

    async def recoverable_reviews(self) -> list[tuple[str, str]]:
        async with self._lock:
            return sorted(
                (review_id, session.owner_id)
                for review_id, session in self._sessions.items()
                if session.status not in {"completed", "cancelled", "failed"}
            )

    def _delete_review_locked(self, review_id: str) -> None:
        super()._delete_review_locked(review_id)
        chunk_ids = {
            chunk_id for chunk_id, chunk in self._chunks.items() if chunk.review_id == review_id
        }
        for chunk_id in chunk_ids:
            self._chunks.pop(chunk_id, None)
            self._chunk_findings.pop(chunk_id, None)
        self._attempts = {
            attempt_id: attempt
            for attempt_id, attempt in self._attempts.items()
            if attempt.review_id != review_id
        }


ReviewStore = EnhancedInMemoryReviewStore



from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from code_review.domain.review_chunks import ChunkAttempt, ChunkStatus, ReviewChunk
from code_review.domain.review_models import (
    Finding,
    FollowupMessage,
    ReviewEvent,
    ReviewEventType,
    ReviewSession,
)

T = TypeVar("T")
_TERMINAL_REVIEW_STATUSES = ("completed", "cancelled", "failed")
_RECOVERABLE_CHUNK_STATUSES = (
    ChunkStatus.QUEUED,
    ChunkStatus.RUNNING,
    ChunkStatus.VALIDATING,
)


class SQLiteReviewStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS review_sessions (
                review_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_chunks (
                chunk_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES review_sessions(review_id)
                    ON DELETE CASCADE,
                status TEXT NOT NULL,
                parent_chunk_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_review
                ON review_chunks(review_id, status);
            CREATE TABLE IF NOT EXISTS chunk_attempts (
                attempt_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES review_sessions(review_id)
                    ON DELETE CASCADE,
                chunk_id TEXT NOT NULL REFERENCES review_chunks(chunk_id)
                    ON DELETE CASCADE,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_findings (
                chunk_id TEXT NOT NULL REFERENCES review_chunks(chunk_id)
                    ON DELETE CASCADE,
                review_id TEXT NOT NULL REFERENCES review_sessions(review_id)
                    ON DELETE CASCADE,
                finding_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (chunk_id, finding_id)
            );
            CREATE TABLE IF NOT EXISTS review_events (
                review_id TEXT NOT NULL REFERENCES review_sessions(review_id)
                    ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                event TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (review_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS model_runs (
                request_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_followups (
                message_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES review_sessions(review_id)
                    ON DELETE CASCADE,
                role TEXT NOT NULL,
                context_key TEXT NOT NULL DEFAULT 'review',
                created_at TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_followups_review
                ON review_followups(review_id, created_at, message_id);
            """
        )
        columns = {str(row["name"]) for row in self._connection.execute("PRAGMA table_info(review_followups)")}
        if "context_key" not in columns:
            self._connection.execute("ALTER TABLE review_followups ADD COLUMN context_key TEXT NOT NULL DEFAULT 'review'")
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_followups_review_context ON review_followups(review_id, context_key, created_at, message_id)")
        self._connection.execute("PRAGMA user_version=3")
        self._connection.commit()

    async def _run(self, operation: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(operation)

    def _require_owner(self, review_id: str, owner_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT owner_id, payload FROM review_sessions WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row is None or row["owner_id"] != owner_id:
            raise KeyError(review_id)
        return row

    def _session_from_row(self, row: sqlite3.Row) -> ReviewSession:
        return ReviewSession.model_validate(
            {**json.loads(row["payload"]), "owner_id": row["owner_id"]}
        )

    def _preserve_title(self, session: ReviewSession) -> ReviewSession:
        current = self._session_from_row(
            self._require_owner(session.review_id, session.owner_id)
        )
        return session.model_copy(update={"title": current.title})

    def _upsert_chunk(self, chunk: ReviewChunk) -> None:
        existing = self._connection.execute(
            "SELECT review_id FROM review_chunks WHERE chunk_id = ?",
            (chunk.chunk_id,),
        ).fetchone()
        if existing is not None and existing["review_id"] != chunk.review_id:
            raise KeyError(chunk.chunk_id)
        self._connection.execute(
            """
            INSERT INTO review_chunks
                (chunk_id, review_id, status, parent_chunk_id, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                status = excluded.status,
                parent_chunk_id = excluded.parent_chunk_id,
                payload = excluded.payload
            """,
            (
                chunk.chunk_id,
                chunk.review_id,
                chunk.status,
                chunk.parent_chunk_id,
                chunk.model_dump_json(),
            ),
        )

    def _append_event(
        self,
        review_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM review_events WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        sequence = int(row["next_sequence"])
        review_event = ReviewEvent(sequence=sequence, event=event, data=data)
        self._connection.execute(
            """
            INSERT INTO review_events (review_id, sequence, event, data)
            VALUES (?, ?, ?, ?)
            """,
            (review_id, sequence, event, json.dumps(data, ensure_ascii=False)),
        )
        return review_event

    async def create(self, session: ReviewSession) -> None:
        def operation() -> None:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO review_sessions
                        (review_id, owner_id, status, created_at, expires_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.review_id,
                        session.owner_id,
                        session.status,
                        session.created_at.isoformat(),
                        session.expires_at.isoformat(),
                        session.model_dump_json(),
                    ),
                )
        await self._run(operation)

    async def get(self, review_id: str, owner_id: str) -> ReviewSession | None:
        def operation() -> ReviewSession | None:
            row = self._connection.execute(
                "SELECT owner_id, payload FROM review_sessions WHERE review_id = ? AND owner_id = ?",
                (review_id, owner_id),
            ).fetchone()
            return self._session_from_row(row) if row else None
        return await self._run(operation)

    async def save(self, session: ReviewSession) -> None:
        def operation() -> None:
            with self._connection:
                persisted = self._preserve_title(session)
                self._connection.execute(
                    """
                    UPDATE review_sessions
                    SET status = ?, expires_at = ?, payload = ?
                    WHERE review_id = ? AND owner_id = ?
                    """,
                    (
                        persisted.status,
                        persisted.expires_at.isoformat(),
                        persisted.model_dump_json(),
                        session.review_id,
                        session.owner_id,
                    ),
                )
        await self._run(operation)

    async def update_title(
        self, review_id: str, owner_id: str, title: str
    ) -> ReviewSession:
        def operation() -> ReviewSession:
            with self._connection:
                current = self._session_from_row(self._require_owner(review_id, owner_id))
                renamed = current.model_copy(update={"title": title})
                self._connection.execute(
                    "UPDATE review_sessions SET payload = ? WHERE review_id = ? AND owner_id = ?",
                    (renamed.model_dump_json(), review_id, owner_id),
                )
                return renamed
        return await self._run(operation)

    async def list_sessions(self, owner_id: str, limit: int, offset: int) -> list[ReviewSession]:
        def operation() -> list[ReviewSession]:
            rows = self._connection.execute(
                """
                SELECT owner_id, payload FROM review_sessions
                WHERE owner_id = ?
                ORDER BY created_at DESC, review_id DESC
                LIMIT ? OFFSET ?
                """,
                (owner_id, limit, offset),
            ).fetchall()
            return [self._session_from_row(row) for row in rows]
        return await self._run(operation)

    async def reset_review(self, session: ReviewSession) -> None:
        def operation() -> None:
            with self._connection:
                persisted = self._preserve_title(session)
                self._connection.execute(
                    """
                    UPDATE review_sessions
                    SET status = ?, expires_at = ?, payload = ?
                    WHERE review_id = ? AND owner_id = ?
                    """,
                    (
                        persisted.status,
                        persisted.expires_at.isoformat(),
                        persisted.model_dump_json(),
                        session.review_id,
                        session.owner_id,
                    ),
                )
                for table in (
                    "review_findings",
                    "chunk_attempts",
                    "review_chunks",
                    "review_events",
                    "model_runs",
                ):
                    self._connection.execute(
                        f"DELETE FROM {table} WHERE review_id = ?", (session.review_id,)
                    )
        await self._run(operation)

    async def save_chunks(self, chunks: list[ReviewChunk], owner_id: str) -> None:
        def operation() -> None:
            with self._connection:
                for chunk in chunks:
                    self._require_owner(chunk.review_id, owner_id)
                for chunk in chunks:
                    self._upsert_chunk(chunk)
        await self._run(operation)

    async def chunks(self, review_id: str, owner_id: str) -> list[ReviewChunk]:
        def operation() -> list[ReviewChunk]:
            with self._connection:
                self._require_owner(review_id, owner_id)
                rows = self._connection.execute(
                    "SELECT payload FROM review_chunks WHERE review_id = ? ORDER BY chunk_id",
                    (review_id,),
                ).fetchall()
                return [ReviewChunk.model_validate_json(row["payload"]) for row in rows]
        return await self._run(operation)

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

    async def record_attempt(self, attempt: ChunkAttempt, owner_id: str) -> None:
        def operation() -> None:
            with self._connection:
                self._require_owner(attempt.review_id, owner_id)
                chunk = self._connection.execute(
                    "SELECT 1 FROM review_chunks WHERE chunk_id = ? AND review_id = ?",
                    (attempt.chunk_id, attempt.review_id),
                ).fetchone()
                if chunk is None:
                    raise KeyError(attempt.chunk_id)
                existing = self._connection.execute(
                    "SELECT review_id FROM chunk_attempts WHERE attempt_id = ?",
                    (attempt.attempt_id,),
                ).fetchone()
                if existing is not None and existing["review_id"] != attempt.review_id:
                    raise KeyError(attempt.attempt_id)
                self._connection.execute(
                    """
                    INSERT INTO chunk_attempts
                        (attempt_id, review_id, chunk_id, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(attempt_id) DO UPDATE SET
                        chunk_id = excluded.chunk_id,
                        payload = excluded.payload
                    """,
                    (
                        attempt.attempt_id,
                        attempt.review_id,
                        attempt.chunk_id,
                        attempt.model_dump_json(),
                    ),
                )
        await self._run(operation)

    async def save_chunk_findings(
        self,
        chunk_id: str,
        owner_id: str,
        findings: list[Finding],
    ) -> None:
        def operation() -> None:
            with self._connection:
                row = self._connection.execute(
                    "SELECT review_id FROM review_chunks WHERE chunk_id = ?",
                    (chunk_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(chunk_id)
                review_id = str(row["review_id"])
                self._require_owner(review_id, owner_id)
                self._connection.execute(
                    "DELETE FROM review_findings WHERE chunk_id = ?", (chunk_id,)
                )
                self._connection.executemany(
                    """
                    INSERT INTO review_findings
                        (chunk_id, review_id, finding_id, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk_id,
                            review_id,
                            finding.finding_id,
                            finding.model_dump_json(),
                        )
                        for finding in findings
                    ],
                )
        await self._run(operation)

    async def chunk_findings(self, review_id: str, owner_id: str) -> list[Finding]:
        def operation() -> list[Finding]:
            with self._connection:
                self._require_owner(review_id, owner_id)
                rows = self._connection.execute(
                    "SELECT payload FROM review_findings WHERE review_id = ? ORDER BY finding_id",
                    (review_id,),
                ).fetchall()
                return [Finding.model_validate_json(row["payload"]) for row in rows]
        return await self._run(operation)

    async def publish(
        self,
        review_id: str,
        owner_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        def operation() -> ReviewEvent:
            with self._connection:
                self._require_owner(review_id, owner_id)
                return self._append_event(review_id, event, data)
        return await self._run(operation)

    async def transition_chunk(
        self,
        chunk: ReviewChunk,
        owner_id: str,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        def operation() -> ReviewEvent:
            with self._connection:
                self._require_owner(chunk.review_id, owner_id)
                self._upsert_chunk(chunk)
                return self._append_event(chunk.review_id, event, data)
        return await self._run(operation)

    async def transition_review(
        self,
        session: ReviewSession,
        event: ReviewEventType,
        data: dict[str, object],
    ) -> ReviewEvent:
        def operation() -> ReviewEvent:
            with self._connection:
                persisted = self._preserve_title(session)
                self._connection.execute(
                    """
                    UPDATE review_sessions SET status = ?, payload = ?
                    WHERE review_id = ? AND owner_id = ?
                    """,
                    (
                        persisted.status,
                        persisted.model_dump_json(),
                        session.review_id,
                        session.owner_id,
                    ),
                )
                return self._append_event(session.review_id, event, data)
        return await self._run(operation)

    async def events_after(
        self, review_id: str, owner_id: str, after: int
    ) -> list[ReviewEvent]:
        def operation() -> list[ReviewEvent]:
            with self._connection:
                self._require_owner(review_id, owner_id)
                rows = self._connection.execute(
                    """
                    SELECT sequence, event, data FROM review_events
                    WHERE review_id = ? AND sequence > ?
                    ORDER BY sequence
                    """,
                    (review_id, after),
                ).fetchall()
                return [
                    ReviewEvent(
                        sequence=row["sequence"],
                        event=row["event"],
                        data=json.loads(row["data"]),
                    )
                    for row in rows
                ]
        return await self._run(operation)

    async def recoverable_reviews(self) -> list[tuple[str, str]]:
        def operation() -> list[tuple[str, str]]:
            with self._connection:
                rows = self._connection.execute(
                    """
                    SELECT review_id, owner_id FROM review_sessions
                    WHERE status NOT IN (?, ?, ?)
                    ORDER BY created_at, review_id
                    """,
                    _TERMINAL_REVIEW_STATUSES,
                ).fetchall()
                reviews = [(str(row["review_id"]), str(row["owner_id"])) for row in rows]
                for review_id, _owner_id in reviews:
                    chunk_rows = self._connection.execute(
                        """
                        SELECT payload FROM review_chunks
                        WHERE review_id = ? AND status IN (?, ?, ?)
                        """,
                        (review_id, *_RECOVERABLE_CHUNK_STATUSES),
                    ).fetchall()
                    for row in chunk_rows:
                        item = ReviewChunk.model_validate_json(row["payload"])
                        self._upsert_chunk(
                            item.model_copy(
                                update={
                                    "status": ChunkStatus.PENDING,
                                    "error_code": None,
                                    "error_message": None,
                                }
                            )
                        )
                return reviews
        return await self._run(operation)

    async def followups(self, review_id: str, owner_id: str, context_key: str = "review") -> list[FollowupMessage]:
        def operation() -> list[FollowupMessage]:
            with self._connection:
                self._require_owner(review_id, owner_id)
                rows = self._connection.execute(
                    """
                    SELECT message_id, review_id, role, context_key, content, created_at
                    FROM review_followups
                    WHERE review_id = ? AND context_key = ?
                    ORDER BY created_at, message_id
                    """,
                    (review_id, context_key),
                ).fetchall()
                return [FollowupMessage.model_validate(dict(row)) for row in rows]
        return await self._run(operation)

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

        def operation() -> None:
            with self._connection:
                self._require_owner(question.review_id, owner_id)
                if question.context_key != context_key or answer.context_key != context_key:
                    raise ValueError("follow-up messages must use the derived context key")
                self._connection.executemany(
                    """
                    INSERT INTO review_followups
                        (message_id, review_id, role, context_key, created_at, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.message_id,
                            item.review_id,
                            item.role,
                            context_key,
                            item.created_at.isoformat(),
                            item.content,
                        )
                        for item in (question, answer)
                    ],
                )
        await self._run(operation)

    async def delete_expired(self, now: datetime) -> list[str]:
        def operation() -> list[str]:
            rows = self._connection.execute(
                "SELECT review_id FROM review_sessions WHERE expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
            review_ids = [str(row["review_id"]) for row in rows]
            with self._connection:
                self._connection.executemany(
                    "DELETE FROM review_sessions WHERE review_id = ?",
                    [(review_id,) for review_id in review_ids],
                )
            return review_ids
        return await self._run(operation)

    async def delete(self, review_id: str, owner_id: str) -> bool:
        def operation() -> bool:
            with self._connection:
                row = self._connection.execute(
                    "SELECT 1 FROM review_sessions WHERE review_id = ? AND owner_id = ?",
                    (review_id, owner_id),
                ).fetchone()
                if row is None:
                    return False
                self._connection.execute(
                    "DELETE FROM model_runs WHERE review_id = ?", (review_id,)
                )
                cursor = self._connection.execute(
                    "DELETE FROM review_sessions WHERE review_id = ? AND owner_id = ?",
                    (review_id, owner_id),
                )
                return cursor.rowcount == 1
        return await self._run(operation)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.close)

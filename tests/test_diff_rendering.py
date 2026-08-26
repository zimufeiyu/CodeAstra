import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import code_review.application.hybrid_review_service as hybrid_module
from code_review.application.hybrid_review_service import (
    HybridReviewService,
    _semantic_batch_timeout,
    _unified_source_diff,
)
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.domain.review_models import ReviewMode, ReviewSession, SourceFile
from code_review.infrastructure.persistence.sqlite_review_store import SQLiteReviewStore


def test_unified_diff_separates_eof_changes_without_a_trailing_newline() -> None:
    diff = _unified_source_diff(
        "def greet(name):\n    return message",
        "def greet(name):\n    return name",
        "snippet.py",
    )

    assert "-    return message\n\\ No newline at end of file\n" in diff
    assert "+    return name\n\\ No newline at end of file\n" in diff
    assert "message+" not in diff


def test_only_applied_fix_revalidation_has_a_bounded_semantic_batch_timeout() -> None:
    session = ReviewSession.create(
        review_id="review-timeout",
        owner_id="owner-timeout",
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id="file-timeout",
                relative_path="snippet.py",
                language="python",
                content="value = 1\n",
            )
        ],
    )
    assert _semantic_batch_timeout(session, 600) is None
    revalidation = session.model_copy(
        update={"finding_states": {"finding-1": "fixed_pending_revalidation"}}
    )
    assert _semantic_batch_timeout(revalidation, 600) == 60
    assert _semantic_batch_timeout(revalidation, 30) == 30


@pytest.mark.asyncio
async def test_revalidation_deadline_returns_to_retryable_failed_without_cancelling_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ReviewSession.create(
        review_id="review-deadline",
        owner_id="owner-deadline",
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id="file-deadline",
                relative_path="snippet.py",
                language="python",
                content="value = 1\n",
            )
        ],
    ).model_copy(
        update={
            "status": "reviewing",
            "finding_states": {"finding-1": "fixed_pending_revalidation"},
            "recheck_attempt_id": "recheck-deadline",
            "recheck_attempt_status": "running",
        }
    )

    class Store:
        def __init__(self) -> None:
            self.session = session
            self.event = ""
            self.data: dict[str, object] = {}

        async def get(self, review_id: str, owner_id: str) -> ReviewSession:
            return self.session

        async def transition_review(
            self,
            updated: ReviewSession,
            event: str,
            data: dict[str, object],
        ) -> None:
            self.session = updated
            self.event = event
            self.data = data

        async def transition_review_if_recheck_attempt(
            self,
            updated: ReviewSession,
            attempt_id: str,
            event: str,
            data: dict[str, object],
        ) -> bool:
            if self.session.recheck_attempt_id != attempt_id:
                return False
            self.session = updated
            self.event = event
            self.data = data
            return True

    store = Store()
    service = object.__new__(HybridReviewService)
    service._store = store
    service._orphaned_recheck_tasks = set()
    service._orphaned_rechecks = {}

    async def hanging_run(
        review_id: str,
        owner_id: str,
        *,
        recheck_attempt_id: str | None = None,
    ) -> None:
        await asyncio.Event().wait()

    service.run = hanging_run
    monkeypatch.setattr(hybrid_module, "_REVALIDATION_BATCH_TIMEOUT_SECONDS", 0.01)

    await service._run_revalidation_with_deadline(
        "review-deadline", "owner-deadline", "recheck-deadline"
    )

    assert store.session.status == "failed"
    assert store.session.files[0].content == "value = 1\n"
    assert store.event == "error"
    assert store.data["code"] == "revalidation_timeout"
    assert store.data["retryable"] is True


@pytest.mark.asyncio
async def test_revalidation_deadline_commits_terminal_event_when_child_ignores_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ReviewSession.create(
        review_id="review-stubborn-deadline",
        owner_id="owner-deadline",
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id="file-deadline",
                relative_path="snippet.py",
                language="python",
                content="value = 1\n",
            )
        ],
    ).model_copy(
        update={
            "status": "queued",
            "finding_states": {"finding-fixed": "fixed_pending_revalidation"},
            "recheck_attempt_id": "recheck-stubborn",
            "recheck_attempt_status": "running",
        }
    )

    class Store:
        def __init__(self) -> None:
            self.session = session
            self.events: list[tuple[str, dict[str, object]]] = []

        async def get(self, review_id: str, owner_id: str) -> ReviewSession:
            return self.session

        async def transition_review_if_recheck_attempt(
            self,
            updated: ReviewSession,
            attempt_id: str,
            event: str,
            data: dict[str, object],
        ) -> bool:
            if (
                self.session.recheck_attempt_id != attempt_id
                or self.session.recheck_attempt_status != "running"
            ):
                return False
            self.session = updated
            self.events.append((event, data))
            return True

    store = Store()
    service = object.__new__(HybridReviewService)
    service._store = store
    service._orphaned_recheck_tasks = set()
    service._orphaned_rechecks = {}
    release_cleanup = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def stubborn_run(
        review_id: str,
        owner_id: str,
        *,
        recheck_attempt_id: str | None = None,
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_cleanup.wait()

    service.run = stubborn_run
    monkeypatch.setattr(hybrid_module, "_REVALIDATION_BATCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(hybrid_module, "_REVALIDATION_CANCEL_GRACE_SECONDS", 0.01)

    deadline = asyncio.create_task(
        service._run_revalidation_with_deadline(
            "review-stubborn-deadline",
            "owner-deadline",
            "recheck-stubborn",
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert deadline.done(), "watchdog must not wait forever for cancellation cleanup"
        await deadline
        assert cancellation_seen.is_set()
        assert store.session.status == "failed"
        assert store.session.recheck_attempt_status == "timed_out"
        assert store.events == [
            (
                "error",
                {
                    "code": "revalidation_timeout",
                    "message": "修复已应用并保留；统一复查超时，可在模型恢复后重新复查。",
                    "retryable": True,
                    "terminal": True,
                    "recheck_attempt_id": "recheck-stubborn",
                },
            )
        ]
    finally:
        release_cleanup.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_late_recheck_result_cannot_overwrite_timed_out_attempt() -> None:
    store = EnhancedInMemoryReviewStore()
    running = ReviewSession.create(
        review_id="review-attempt-cas",
        owner_id="owner-attempt-cas",
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id="file-attempt-cas",
                relative_path="snippet.py",
                language="python",
                content="value = 1\n",
            )
        ],
    ).model_copy(
        update={
            "status": "reviewing",
            "recheck_attempt_id": "attempt-1",
            "recheck_attempt_status": "running",
        }
    )
    await store.create(running)
    failed = running.model_copy(
        update={"status": "failed", "recheck_attempt_status": "timed_out"}
    )
    assert await store.transition_review_if_recheck_attempt(
        failed,
        "attempt-1",
        "error",
        {"code": "revalidation_timeout", "terminal": True},
    )

    late = running.model_copy(
        update={"status": "completed", "recheck_attempt_status": "completed"}
    )
    assert not await store.transition_review_if_recheck_attempt(
        late, "attempt-1", "complete", {"review_id": running.review_id}
    )
    persisted = await store.get(running.review_id, running.owner_id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert [event.event for event in await store.events_after(
        running.review_id, running.owner_id, 0
    )] == ["error"]


@pytest.mark.asyncio
async def test_start_recovers_expired_persisted_recheck_as_terminal_failure() -> None:
    store = EnhancedInMemoryReviewStore()
    expired = ReviewSession.create(
        review_id="review-expired-recheck",
        owner_id="owner-expired-recheck",
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id="file-expired-recheck",
                relative_path="snippet.py",
                language="python",
                content="value = 1\n",
            )
        ],
    ).model_copy(
        update={
            "status": "reviewing",
            "finding_states": {"finding-fixed": "fixed_pending_revalidation"},
            "recheck_attempt_id": "attempt-expired",
            "recheck_attempt_status": "running",
            "recheck_deadline_at": datetime.now(tz=UTC) - timedelta(seconds=1),
        }
    )
    await store.create(expired)
    service = object.__new__(HybridReviewService)
    service._store = store
    service._tasks = {}

    await service.start(expired.review_id, expired.owner_id)

    persisted = await store.get(expired.review_id, expired.owner_id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.recheck_attempt_status == "timed_out"
    events = await store.events_after(expired.review_id, expired.owner_id, 0)
    assert [(event.event, event.data["code"]) for event in events] == [
        ("error", "revalidation_timeout")
    ]
    assert service._tasks == {}


@pytest.mark.asyncio
async def test_sqlite_recheck_cas_restores_owner_from_protected_column(tmp_path) -> None:
    store = SQLiteReviewStore(tmp_path / "reviews.sqlite3")
    session = ReviewSession.create(
        review_id="review-sqlite-cas",
        owner_id="owner-sqlite-cas",
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id="file-sqlite-cas",
                relative_path="snippet.py",
                language="python",
                content="value = 1\n",
            )
        ],
    ).model_copy(
        update={
            "status": "reviewing",
            "recheck_attempt_id": "attempt-sqlite",
            "recheck_attempt_status": "running",
        }
    )
    await store.create(session)
    failed = session.model_copy(
        update={"status": "failed", "recheck_attempt_status": "timed_out"}
    )
    assert await store.transition_review_if_recheck_attempt(
        failed,
        "attempt-sqlite",
        "error",
        {"code": "revalidation_timeout", "terminal": True},
    )
    persisted = await store.get(session.review_id, session.owner_id)
    assert persisted is not None
    assert persisted.status == "failed"

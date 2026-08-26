from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.domain.review_models import (
    Finding,
    FollowupMessage,
    ReviewMode,
    ReviewSession,
    SourceFile,
)
from code_review.infrastructure.persistence.sqlite_review_store import SQLiteReviewStore


def _finding(finding_id: str, file_id: str, evidence: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        source="static",
        analyzer="test",
        rule_id="test.rule",
        category="correctness",
        severity="medium",
        confidence=1.0,
        file_id=file_id,
        relative_path=f"{file_id}.py",
        start_line=1,
        start_column=1,
        end_line=1,
        end_column=len(evidence) + 1,
        title=f"问题 {finding_id}",
        hover_summary="问题",
        detail="问题",
        evidence=evidence,
        impact="impact",
        suggestion="fix",
    )


def _session() -> ReviewSession:
    files = [
        SourceFile.from_content(
            file_id="a", relative_path="a.py", language="python", content="a_secret = 1\n"
        ),
        SourceFile.from_content(
            file_id="b", relative_path="b.py", language="python", content="b_secret = 2\n"
        ),
    ]
    return ReviewSession.create(
        review_id="followups", owner_id="alice", mode=ReviewMode.PROJECT, files=files
    ).model_copy(
        update={
            "status": "completed",
            "findings": [
                _finding("finding-a", "a", "a_secret"),
                _finding("finding-b", "b", "b_secret"),
            ],
        }
    )


def _message(
    index: int, role: str, context_key: str, content: str
) -> FollowupMessage:
    return FollowupMessage(
        message_id=f"{context_key}-{index}-{role}",
        review_id="followups",
        role=role,
        context_key=context_key,
        content=content,
        created_at=datetime.now(tz=UTC) + timedelta(microseconds=index),
    )


async def _seed_exchange(store, context_key: str, index: int, prefix: str) -> None:
    await store.append_followup_exchange(
        _message(index * 2, "user", context_key, f"{prefix}-question-{index}"),
        _message(index * 2 + 1, "assistant", context_key, f"{prefix}-answer-{index}"),
        "alice",
        context_key,
    )


class FollowupInference:
    def __init__(self) -> None:
        self.requests = []

    async def answer_followup(self, request):
        self.requests.append(request)
        return "isolated answer"


@pytest.mark.asyncio
async def test_followup_prompt_uses_only_current_context_last_eight_and_owner():
    store = EnhancedInMemoryReviewStore()
    session = _session()
    await store.create(session)
    for index in range(5):
        await _seed_exchange(store, "finding:finding-a", index, "A")
    await _seed_exchange(store, "finding:finding-b", 0, "B-SECRET")
    inference = FollowupInference()
    service = HybridReviewService(inference, store)
    await service.ask_followup(
        "followups",
        "alice",
        "只解释 A",
        {"kind": "finding", "file_id": "a", "finding_id": "finding-a"},
    )
    prompt = inference.requests[0].messages[-1].content
    assert "a_secret = 1" in prompt
    assert "b_secret = 2" not in prompt
    assert "B-SECRET" not in prompt
    assert "A-question-0" not in prompt
    assert "A-question-1" in prompt
    with pytest.raises(KeyError):
        await service.followups(
            "followups",
            "bob",
            {"kind": "finding", "file_id": "a", "finding_id": "finding-a"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_finding_selection_and_legacy_followups_never_cross_contexts(
    backend, tmp_path
):
    store = (
        EnhancedInMemoryReviewStore()
        if backend == "memory"
        else SQLiteReviewStore(tmp_path / "followups.sqlite3")
    )
    await store.create(_session())
    await _seed_exchange(store, "finding:finding-a", 0, "A")
    await _seed_exchange(store, "finding:finding-b", 0, "B")
    selection_key = "selection:a:1:1:abcdef"
    await _seed_exchange(store, selection_key, 0, "SELECTION")
    await _seed_exchange(store, "review", 0, "LEGACY")
    finding_a = await store.followups("followups", "alice", "finding:finding-a")
    assert [item.content for item in finding_a] == [
        "A-question-0",
        "A-answer-0",
    ]
    finding_b = await store.followups("followups", "alice", "finding:finding-b")
    assert [item.content for item in finding_b] == [
        "B-question-0",
        "B-answer-0",
    ]
    selection = await store.followups("followups", "alice", selection_key)
    assert [item.content for item in selection] == [
        "SELECTION-question-0",
        "SELECTION-answer-0",
    ]
    assert [item.content for item in await store.followups("followups", "alice")] == [
        "LEGACY-question-0",
        "LEGACY-answer-0",
    ]
    if isinstance(store, SQLiteReviewStore):
        await store.close()

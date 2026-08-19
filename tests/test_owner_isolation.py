from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from code_review.application.chunk_review_service import ChunkReviewService
from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.review_store import EnhancedInMemoryReviewStore, InMemoryReviewStore
from code_review.domain.review_chunks import ChunkAttempt, ReviewChunk
from code_review.domain.review_ports import ReviewStorePort
from code_review.domain.review_models import Finding, FollowupMessage, ReviewEvent, ReviewMode, ReviewSession, SourceFile
from code_review.infrastructure.persistence.sqlite_review_store import SQLiteReviewStore


def _session(review_id: str, owner_id: str) -> ReviewSession:
    return ReviewSession.create(
        review_id=review_id,
        owner_id=owner_id,
        mode=ReviewMode.PASTE,
        files=[
            SourceFile.from_content(
                file_id=f'{review_id}-file',
                relative_path='example.py',
                language='python',
                content='pass\n',
            )
        ],
    )


@pytest_asyncio.fixture(params=['memory', 'sqlite'])
async def populated_store(request, tmp_path):
    store = (
        EnhancedInMemoryReviewStore()
        if request.param == 'memory'
        else SQLiteReviewStore(tmp_path / 'owner-contract.sqlite3')
    )
    alice_id, bob_id = 'alice-id', 'bob-id'
    alice_review, bob_review = 'alice-review', 'bob-review'
    await store.create(_session(alice_review, alice_id))
    await store.create(_session(bob_review, bob_id))
    try:
        yield store, alice_id, bob_id, alice_review, bob_review
    finally:
        if isinstance(store, SQLiteReviewStore):
            await store.close()


@pytest.mark.parametrize(
    'store_type',
    [ReviewStorePort, EnhancedInMemoryReviewStore, SQLiteReviewStore],
)
def test_review_stores_expose_required_owner_id_contract(store_type):
    methods = (
        'get',
        'list_sessions',
        'delete',
        'update_title',
        'save_chunks',
        'chunks',
        'save_chunk',
        'replace_chunk',
        'save_chunk_findings',
        'chunk_findings',
        'record_attempt',
        'publish',
        'events_after',
        'followups',
        'append_followup_exchange',
        'transition_chunk',
    )
    for method in methods:
        owner_id = inspect.signature(getattr(store_type, method)).parameters['owner_id']
        assert owner_id.default is inspect.Parameter.empty


def _chunk(review_id: str) -> ReviewChunk:
    return ReviewChunk(
        chunk_id=f'{review_id}-chunk',
        review_id=review_id,
        language='python',
        target_file_id=f'{review_id}-file',
        target_path='example.py',
        target_start_line=1,
        target_end_line=1,
        target_code='pass\n',
        content_fingerprint='0' * 64,
    )


def _finding(review_id: str) -> Finding:
    return Finding(
        finding_id=f'{review_id}-finding',
        source='static',
        analyzer='test',
        rule_id='TEST001',
        category='test',
        severity='low',
        confidence=1.0,
        file_id=f'{review_id}-file',
        start_line=1,
        start_column=1,
        end_line=1,
        end_column=4,
        title='Test finding',
        hover_summary='Test finding summary',
        detail='Test finding detail',
        evidence='pass',
        impact='Test impact',
        suggestion='Test suggestion',
    )


def _followup(review_id: str, role: str, message_id: str) -> FollowupMessage:
    return FollowupMessage(
        message_id=message_id,
        review_id=review_id,
        role=role,
        content='question' if role == 'user' else 'answer',
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_store_get_list_and_delete_are_scoped_by_owner_id(populated_store):
    store, alice_id, bob_id, alice_review, bob_review = populated_store
    assert await store.get(alice_review, alice_id) is not None
    assert await store.get(bob_review, alice_id) is None
    assert [item.review_id for item in await store.list_sessions(alice_id, limit=20, offset=0)] == [
        alice_review
    ]
    assert await store.delete(bob_review, alice_id) is False
    assert await store.get(bob_review, bob_id) is not None


@pytest.mark.asyncio
async def test_associated_persistence_rejects_cross_owner_access(populated_store):
    store, alice_id, bob_id, _alice_review, bob_review = populated_store
    bob_chunk = _chunk(bob_review)
    bob_finding = _finding(bob_review)
    question = _followup(bob_review, 'user', 'bob-question')
    answer = _followup(bob_review, 'assistant', 'bob-answer')

    await store.save_chunk(bob_chunk, bob_id)
    await store.save_chunk_findings(bob_chunk.chunk_id, bob_id, [bob_finding])
    await store.record_attempt(
        ChunkAttempt(
            attempt_id='bob-attempt',
            review_id=bob_review,
            chunk_id=bob_chunk.chunk_id,
            attempt_number=1,
            strategy='original',
            request_id='bob-request',
        ),
        bob_id,
    )
    await store.publish(bob_review, bob_id, 'stage', {'stage': 'reviewing'})
    await store.append_followup_exchange(question, answer, bob_id)

    with pytest.raises(KeyError):
        await store.chunks(bob_review, alice_id)
    with pytest.raises(KeyError):
        await store.chunk_findings(bob_review, alice_id)
    with pytest.raises(KeyError):
        await store.events_after(bob_review, alice_id, after=0)
    with pytest.raises(KeyError):
        await store.followups(bob_review, alice_id)
    assert await store.delete(bob_review, alice_id) is False


@pytest.mark.asyncio
async def test_serialized_review_output_does_not_expose_owner_id(populated_store):
    store, alice_id, _bob_id, alice_review, _bob_review = populated_store
    session = await store.get(alice_review, alice_id)

    assert session is not None
    assert 'owner_id' not in session.model_dump(mode='json')
    assert 'owner_id' not in json.loads(session.model_dump_json())


def _attempt_review_ids(store) -> dict[str, str]:
    if isinstance(store, SQLiteReviewStore):
        rows = store._connection.execute(
            'SELECT attempt_id, review_id FROM chunk_attempts ORDER BY attempt_id'
        ).fetchall()
        return {str(row['attempt_id']): str(row['review_id']) for row in rows}
    return {
        attempt_id: attempt.review_id
        for attempt_id, attempt in store._attempts.items()
    }


@pytest.mark.asyncio
async def test_owner_aware_mutations_reject_cross_owner_and_preserve_bob_data(populated_store):
    store, alice_id, bob_id, _alice_review, bob_review = populated_store
    bob_chunk = _chunk(bob_review)
    bob_finding = _finding(bob_review)
    bob_attempt = ChunkAttempt(
        attempt_id='bob-attempt',
        review_id=bob_review,
        chunk_id=bob_chunk.chunk_id,
        attempt_number=1,
        strategy='original',
        request_id='bob-request',
    )
    question = _followup(bob_review, 'user', 'bob-question')
    answer = _followup(bob_review, 'assistant', 'bob-answer')

    await store.save_chunk(bob_chunk, bob_id)
    await store.save_chunk_findings(bob_chunk.chunk_id, bob_id, [bob_finding])
    await store.record_attempt(bob_attempt, bob_id)
    await store.publish(bob_review, bob_id, 'stage', {'stage': 'reviewing'})
    await store.append_followup_exchange(question, answer, bob_id)

    mutations = (
        lambda: store.save_chunks([bob_chunk], alice_id),
        lambda: store.save_chunk(bob_chunk, alice_id),
        lambda: store.replace_chunk(bob_chunk, [], alice_id),
        lambda: store.save_chunk_findings(bob_chunk.chunk_id, alice_id, [bob_finding]),
        lambda: store.record_attempt(bob_attempt, alice_id),
        lambda: store.transition_chunk(bob_chunk, alice_id, 'chunk', {'status': 'running'}),
        lambda: store.publish(bob_review, alice_id, 'stage', {'stage': 'stolen'}),
        lambda: store.append_followup_exchange(question, answer, alice_id),
    )
    for mutation in mutations:
        with pytest.raises(KeyError):
            await mutation()

    assert [chunk.chunk_id for chunk in await store.chunks(bob_review, bob_id)] == [
        bob_chunk.chunk_id
    ]
    assert [finding.finding_id for finding in await store.chunk_findings(bob_review, bob_id)] == [
        bob_finding.finding_id
    ]
    assert _attempt_review_ids(store) == {bob_attempt.attempt_id: bob_review}
    assert [event.data for event in await store.events_after(bob_review, bob_id, after=0)] == [
        {'stage': 'reviewing'}
    ]
    assert {message.message_id for message in await store.followups(bob_review, bob_id)} == {
        question.message_id,
        answer.message_id,
    }


@pytest.mark.asyncio
async def test_primary_key_collisions_do_not_cross_review_boundaries(populated_store):
    store, alice_id, bob_id, alice_review, bob_review = populated_store
    bob_chunk = _chunk(bob_review)
    await store.save_chunk(bob_chunk, bob_id)
    bob_attempt = ChunkAttempt(
        attempt_id='shared-attempt',
        review_id=bob_review,
        chunk_id=bob_chunk.chunk_id,
        attempt_number=1,
        strategy='original',
        request_id='bob-request',
    )
    await store.record_attempt(bob_attempt, bob_id)

    alice_chunk = _chunk(alice_review)
    await store.save_chunk(alice_chunk, alice_id)
    colliding_chunk = alice_chunk.model_copy(update={'chunk_id': bob_chunk.chunk_id})
    with pytest.raises(KeyError):
        await store.save_chunk(colliding_chunk, alice_id)
    with pytest.raises(KeyError):
        await store.transition_chunk(colliding_chunk, alice_id, 'chunk', {'status': 'running'})

    colliding_attempt = ChunkAttempt(
        attempt_id=bob_attempt.attempt_id,
        review_id=alice_review,
        chunk_id=alice_chunk.chunk_id,
        attempt_number=1,
        strategy='original',
        request_id='alice-request',
    )
    with pytest.raises(KeyError):
        await store.record_attempt(colliding_attempt, alice_id)

    assert [chunk.review_id for chunk in await store.chunks(bob_review, bob_id)] == [bob_review]
    assert [chunk.chunk_id for chunk in await store.chunks(alice_review, alice_id)] == [
        alice_chunk.chunk_id
    ]
    assert _attempt_review_ids(store) == {bob_attempt.attempt_id: bob_review}


@pytest.mark.asyncio
async def test_delete_and_expiry_cleanup_make_reused_review_ids_empty(populated_store):
    store, alice_id, bob_id, _alice_review, bob_review = populated_store
    bob_chunk = _chunk(bob_review)
    await store.save_chunk(bob_chunk, bob_id)
    await store.save_chunk_findings(bob_chunk.chunk_id, bob_id, [_finding(bob_review)])
    await store.record_attempt(
        ChunkAttempt(
            attempt_id='bob-attempt',
            review_id=bob_review,
            chunk_id=bob_chunk.chunk_id,
            attempt_number=1,
            strategy='original',
            request_id='bob-request',
        ),
        bob_id,
    )
    await store.publish(bob_review, bob_id, 'stage', {'stage': 'reviewing'})
    await store.append_followup_exchange(
        _followup(bob_review, 'user', 'bob-question'),
        _followup(bob_review, 'assistant', 'bob-answer'),
        bob_id,
    )

    assert await store.delete(bob_review, bob_id) is True
    await store.create(_session(bob_review, alice_id))
    assert await store.chunks(bob_review, alice_id) == []
    assert await store.chunk_findings(bob_review, alice_id) == []
    assert await store.events_after(bob_review, alice_id, after=0) == []
    assert await store.followups(bob_review, alice_id) == []
    assert _attempt_review_ids(store) == {}

    expired_review = 'expired-review'
    expired = _session(expired_review, bob_id).model_copy(
        update={'expires_at': datetime.now(tz=UTC) - timedelta(seconds=1)}
    )
    await store.create(expired)
    expired_chunk = _chunk(expired_review)
    await store.save_chunk(expired_chunk, bob_id)
    await store.save_chunk_findings(expired_chunk.chunk_id, bob_id, [_finding(expired_review)])
    await store.record_attempt(
        ChunkAttempt(
            attempt_id='expired-attempt',
            review_id=expired_review,
            chunk_id=expired_chunk.chunk_id,
            attempt_number=1,
            strategy='original',
            request_id='expired-request',
        ),
        bob_id,
    )
    await store.publish(expired_review, bob_id, 'stage', {'stage': 'reviewing'})
    await store.append_followup_exchange(
        _followup(expired_review, 'user', 'expired-question'),
        _followup(expired_review, 'assistant', 'expired-answer'),
        bob_id,
    )

    assert expired_review in await store.delete_expired(datetime.now(tz=UTC))
    await store.create(_session(expired_review, alice_id))
    assert await store.chunks(expired_review, alice_id) == []
    assert await store.chunk_findings(expired_review, alice_id) == []
    assert await store.events_after(expired_review, alice_id, after=0) == []
    assert await store.followups(expired_review, alice_id) == []
    assert _attempt_review_ids(store) == {}


@pytest.mark.asyncio
async def test_chunk_executor_passes_owner_to_every_persistent_operation():
    class RecordingStore:
        def __init__(self) -> None:
            self.owners: list[str] = []

        async def transition_chunk(self, chunk, owner_id, event, data):
            self.owners.append(owner_id)

        async def record_attempt(self, attempt, owner_id):
            self.owners.append(owner_id)

        async def save_chunk_findings(self, chunk_id, owner_id, findings):
            self.owners.append(owner_id)

    class PromptBuilder:
        def build(self, chunk, attempt_id):
            return SimpleNamespace(
                max_output_tokens=16,
                model_copy=lambda update: SimpleNamespace(max_output_tokens=16),
            )

    class Inference:
        async def review(self, request):
            return SimpleNamespace(findings=[])

    store = RecordingStore()
    service = ChunkReviewService(
        inference_service=Inference(),
        store=store,
        planner=SimpleNamespace(),
        prompt_builder=PromptBuilder(),
        max_split_depth=0,
    )

    await service.execute(
        _chunk('owned-review'),
        [],
        [],
        owner_id='owner-id',
        model_profile_id='profile',
        model_name='model',
    )

    assert store.owners == ['owner-id'] * 6


@pytest.mark.asyncio
async def test_hybrid_events_pass_owner_to_the_store():
    class EventStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []
            self.session = _session('bob-review', 'bob-id').model_copy(update={'status': 'completed'})

        async def get(self, review_id, owner_id):
            return self.session if (review_id, owner_id) == ('bob-review', 'bob-id') else None

        async def events_after(self, review_id, owner_id, after):
            self.calls.append((review_id, owner_id, after))
            return [ReviewEvent(sequence=1, event='stage', data={'owner': owner_id})] if after == 0 else []

    service = object.__new__(HybridReviewService)
    store = EventStore()
    service._store = store

    events = [event async for event in service.events('bob-review', 'bob-id')]

    assert [event.data for event in events] == [{'owner': 'bob-id'}]
    assert store.calls == [('bob-review', 'bob-id', 0), ('bob-review', 'bob-id', 1)]


@pytest.mark.asyncio
async def test_hybrid_recovery_starts_each_review_with_its_persisted_owner():
    class RecoveryStore:
        async def recoverable_reviews(self):
            return [('alice-review', 'alice-id'), ('bob-review', 'bob-id')]

    service = object.__new__(HybridReviewService)
    service._store = RecoveryStore()
    started: list[tuple[str, str]] = []

    async def start(review_id: str, owner_id: str) -> None:
        started.append((review_id, owner_id))

    service.start = start

    assert await service.recover() == ['alice-review', 'bob-review']
    assert started == [('alice-review', 'alice-id'), ('bob-review', 'bob-id')]


@pytest.mark.asyncio
async def test_chunk_executor_failure_writes_only_with_the_owner():
    class RecordingStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def transition_chunk(self, chunk, owner_id, event, data):
            self.calls.append(('transition_chunk', owner_id))

        async def record_attempt(self, attempt, owner_id):
            self.calls.append(('record_attempt', owner_id))

    class PromptBuilder:
        def build(self, chunk, attempt_id):
            return SimpleNamespace(max_output_tokens=16, model_copy=lambda update: SimpleNamespace(max_output_tokens=16))

    class FailingInference:
        async def review(self, request):
            raise RuntimeError('model failure')

    store = RecordingStore()
    service = ChunkReviewService(
        inference_service=FailingInference(),
        store=store,
        planner=SimpleNamespace(),
        prompt_builder=PromptBuilder(),
        max_split_depth=0,
    )

    assert await service.execute(_chunk('failed-review'), [], [], owner_id='owner-id') == []
    assert {owner_id for _operation, owner_id in store.calls} == {'owner-id'}
    assert [operation for operation, _owner_id in store.calls].count('record_attempt') == 4


@pytest.mark.asyncio
async def test_chunk_executor_split_writes_and_publishes_only_with_the_owner():
    class RecordingStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def transition_chunk(self, chunk, owner_id, event, data):
            self.calls.append(('transition_chunk', owner_id))

        async def record_attempt(self, attempt, owner_id):
            self.calls.append(('record_attempt', owner_id))

        async def replace_chunk(self, parent, children, owner_id):
            self.calls.append(('replace_chunk', owner_id))

        async def publish(self, review_id, owner_id, event, data):
            self.calls.append(('publish', owner_id))

    class PromptBuilder:
        def build(self, chunk, attempt_id):
            return SimpleNamespace(max_output_tokens=16, model_copy=lambda update: SimpleNamespace(max_output_tokens=16))

    class FailingInference:
        async def review(self, request):
            raise RuntimeError('model failure')

    parent = _chunk('split-review')
    child = parent.model_copy(update={'chunk_id': 'split-review-child', 'split_depth': 1})
    store = RecordingStore()
    service = ChunkReviewService(
        inference_service=FailingInference(),
        store=store,
        planner=SimpleNamespace(split=lambda chunk: [child]),
        prompt_builder=PromptBuilder(),
        max_split_depth=1,
    )

    assert await service.execute(parent, [], [], owner_id='owner-id') == [child]
    assert ('replace_chunk', 'owner-id') in store.calls
    assert ('publish', 'owner-id') in store.calls
    assert {owner_id for _operation, owner_id in store.calls} == {'owner-id'}


@pytest.mark.asyncio
async def test_hybrid_run_publishes_static_findings_with_the_persisted_owner():
    class RunStore:
        def __init__(self, session) -> None:
            self.session = session
            self.publish_calls: list[tuple[str, str]] = []

        async def get(self, review_id, owner_id):
            assert (review_id, owner_id) == ('run-review', 'owner-id')
            return self.session

        async def transition_review(self, session, event, data):
            self.session = session

        async def save(self, session):
            self.session = session

        async def publish(self, review_id, owner_id, event, data):
            self.publish_calls.append((review_id, owner_id))

    class StaticAnalyzer:
        async def analyze(self, files):
            syntax_finding = _finding('run-review').model_copy(update={'rule_id': 'python.syntax-error'})
            return SimpleNamespace(findings=[syntax_finding], coverage=[])

    session = _session('run-review', 'owner-id')
    store = RunStore(session)
    service = object.__new__(HybridReviewService)
    service._store = store
    service._analyzer = StaticAnalyzer()
    service._executors = {}
    service._executor = SimpleNamespace()

    await service.run('run-review', 'owner-id')

    assert store.publish_calls == [('run-review', 'owner-id')]


@pytest.mark.asyncio
async def test_hybrid_resume_and_cancel_keep_the_owner_on_store_access():
    class ResumeCancelStore:
        def __init__(self) -> None:
            self.session = _session('resume-review', 'owner-id')
            self.calls: list[tuple[str, str]] = []

        async def get(self, review_id, owner_id):
            self.calls.append(('get', owner_id))
            return self.session if review_id == 'resume-review' else None

        async def chunks(self, review_id, owner_id):
            self.calls.append(('chunks', owner_id))
            return [_chunk(review_id).model_copy(update={'status': 'failed'})]

        async def save_chunks(self, chunks, owner_id):
            self.calls.append(('save_chunks', owner_id))

        async def save(self, session):
            self.session = session

        async def transition_review(self, session, event, data):
            self.calls.append(('transition_review', session.owner_id))
            self.session = session

    store = ResumeCancelStore()
    service = object.__new__(HybridReviewService)
    service._store = store
    service._tasks = {}
    restarted: list[tuple[str, str]] = []

    async def start(review_id: str, owner_id: str) -> None:
        restarted.append((review_id, owner_id))

    service.start = start

    assert await service.resume('resume-review', 'owner-id') is True
    assert await service.cancel('resume-review', 'owner-id') is True
    assert restarted == [('resume-review', 'owner-id')]
    assert {owner_id for _operation, owner_id in store.calls} == {'owner-id'}

import pytest

from code_review.application.evidence_validation import merge_findings
from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.domain.model_protocol import ReviewFinding, ReviewResponse, RootCauseClaim
from code_review.domain.review_models import RepairIntentSelection, ReviewMode, SourceFile


class PipelineInference:
    def __init__(self) -> None:
        self.review_requests = []
        self.fix_requests = []

    async def review(self, request):
        self.review_requests.append(request)
        prompt = request.messages[-1].content
        if "semantic.py" in prompt:
            finding = ReviewFinding(
                rule_id="semantic.zero-divisor",
                severity="medium",
                confidence=0.95,
                category="correctness",
                file="semantic.py",
                start_line=2,
                end_line=2,
                title="除数为零时会抛出异常",
                evidence="return total / count",
                impact="count 为零时函数抛出 ZeroDivisionError。",
                suggestion="根据调用契约显式处理零值。",
                root_cause_claim=RootCauseClaim(
                    claim="count 可能由公开参数传入零值。",
                    concrete_failing_input="divide(1, 0)",
                    expected_behavior="返回契约定义的失败结果。",
                    actual_behavior="抛出 ZeroDivisionError。",
                    affected_path="semantic.py",
                    repair_invariant="非零 count 的计算结果保持不变。",
                    contract_evidence="函数参数未限制 count 非零。",
                ),
            )
            return ReviewResponse(summary="semantic", findings=[finding, finding], uncovered=[])
        # Deliberately emit a deterministic duplicate. The verifier must suppress it.
        path = "intent.py" if "intent.py" in prompt else "safe.py"
        evidence = "valux" if path == "intent.py" else "valu"
        duplicate = ReviewFinding(
            rule_id="python.undefined-name",
            severity="medium",
            confidence=0.99,
            category="correctness",
            file=path,
            start_line=2,
            end_line=2,
            title="名称未定义重复模型判断",
            evidence=evidence,
            impact="模型不应重判确定性事实。",
            suggestion="交给 analyzer。",
            root_cause_claim=RootCauseClaim(
                claim="deterministic duplicate",
                concrete_failing_input="call",
                expected_behavior="defined",
                actual_behavior="undefined",
                affected_path=path,
                repair_invariant="rename",
                reachable_path="line 2",
            ),
        )
        return ReviewResponse(summary="deterministic", findings=[duplicate], uncovered=[])

    async def propose_fix(self, request):
        self.fix_requests.append(request)
        raise AssertionError("safe and selected intent plans must not call the model")


@pytest.mark.asyncio
async def test_complete_pipeline_is_connected_and_does_not_duplicate_work():
    store = EnhancedInMemoryReviewStore()
    inference = PipelineInference()
    service = HybridReviewService(inference, store)
    files = [
        SourceFile.from_content(
            file_id="safe",
            relative_path="safe.py",
            language="python",
            content="def safe(value):\n    return valu\n",
        ),
        SourceFile.from_content(
            file_id="intent",
            relative_path="intent.py",
            language="python",
            content="def intent(value):\n    return valux\n",
        ),
        SourceFile.from_content(
            file_id="semantic",
            relative_path="semantic.py",
            language="python",
            content="def divide(total, count):\n    return total / count\n",
        ),
    ]
    created = await service.create(ReviewMode.PROJECT, files, owner_id="owner")
    await service.run(created.review_id, "owner")
    reviewed = await service.get(created.review_id, "owner")
    assert reviewed is not None and reviewed.status == "completed"
    assert len(reviewed.findings) == 3
    assert sum(item.source == "llm" for item in reviewed.findings) == 1

    safe = next(item for item in reviewed.findings if item.file_id == "safe")
    safe_candidate = await service.preview_fix(created.review_id, "owner", safe.finding_id)
    assert safe_candidate.fix_safety == "safe"
    await service.confirm_fix(created.review_id, "owner", safe_candidate.candidate_id)

    current = await service.get(created.review_id, "owner")
    assert current is not None
    intent = next(item for item in current.findings if item.file_id == "intent")
    option = next(
        item for item in intent.use_def_evidence.options if item.option_id == "rename:value"
    )
    intent_source = next(item for item in current.files if item.file_id == "intent")
    intent_candidate = await service.preview_fix(
        created.review_id,
        "owner",
        intent.finding_id,
        RepairIntentSelection(
            review_id=created.review_id,
            finding_id=intent.finding_id,
            base_sha=intent_source.sha256,
            option_id=option.option_id,
            intent_kind=option.kind,
            selected_symbol="value",
        ),
    )
    await service.confirm_fix(created.review_id, "owner", intent_candidate.candidate_id)

    current = await service.get(created.review_id, "owner")
    assert current is not None
    semantic = next(item for item in current.findings if item.file_id == "semantic")
    decided, restarted = await service.record_finding_decision(
        created.review_id, "owner", semantic.finding_id, "dismissed"
    )
    assert restarted is not None and decided.findings == []
    task = service._tasks.get(created.review_id)
    assert task is not None
    await task

    final = await service.get(created.review_id, "owner")
    assert final is not None and final.status == "completed" and final.findings == []
    assert len(final.revisions) == 2
    assert final.finding_states[semantic.finding_id] == "dismissed"
    assert inference.fix_requests == []
    assert len(inference.review_requests) == 6  # three chunks, one initial and one re-review
    events = await store.events_after(created.review_id, "owner", 0)
    complete_events = [item for item in events if item.event == "complete"]
    assert len(complete_events) == 2
    assert [item.sequence for item in events] == list(range(1, len(events) + 1))
    counters = service.pipeline_counters()
    assert counters["model_review_calls"] == 6
    assert counters["re_review_count"] == 1
    assert counters["deduplicated_findings"] == 2
    assert counters["analyzer_cache_hits"] > 0


@pytest.mark.asyncio
async def test_three_chunk_pure_decisions_do_not_call_model_or_reset_sse_again():
    store = EnhancedInMemoryReviewStore()
    inference = PipelineInference()
    service = HybridReviewService(inference, store)
    files = [
        SourceFile.from_content(
            file_id="safe",
            relative_path="safe.py",
            language="python",
            content="def safe(value):\n    return valu\n",
        ),
        SourceFile.from_content(
            file_id="intent",
            relative_path="intent.py",
            language="python",
            content="def intent(value):\n    return valux\n",
        ),
        SourceFile.from_content(
            file_id="semantic",
            relative_path="semantic.py",
            language="python",
            content="def divide(total, count):\n    return total / count\n",
        ),
    ]
    created = await service.create(ReviewMode.PROJECT, files, owner_id="owner")
    await service.run(created.review_id, "owner")
    reviewed = await service.get(created.review_id, "owner")
    assert reviewed is not None and len(reviewed.findings) == 3
    for finding in list(reviewed.findings):
        decided, revised = await service.record_finding_decision(
            created.review_id, "owner", finding.finding_id, "accepted_risk"
        )
        assert revised is None
    assert decided.findings == []
    assert len(inference.review_requests) == 3
    counters = service.pipeline_counters()
    assert counters["model_review_calls"] == 3
    assert counters.get("re_review_count", 0) == 0
    events = await store.events_after(created.review_id, "owner", 0)
    assert len([item for item in events if item.event == "complete"]) == 1
    assert [item.sequence for item in events] == list(range(1, len(events) + 1))


def test_dedupe_does_not_merge_distinct_security_findings_on_same_line():
    source = SourceFile.from_content(
        file_id="f", relative_path="f.py", language="python", content="run(value)\n"
    )
    base = dict(
        source="llm",
        analyzer="fake",
        category="security",
        severity="high",
        confidence=0.9,
        file_id=source.file_id,
        relative_path=source.relative_path,
        start_line=1,
        start_column=1,
        end_line=1,
        end_column=4,
        hover_summary="risk",
        detail="risk",
        impact="risk",
        suggestion="fix",
    )
    from code_review.domain.review_models import Finding

    first = Finding(
        finding_id="one", rule_id="security.command", title="命令注入", evidence="run", **base
    )
    second = Finding(
        finding_id="two", rule_id="security.path", title="路径穿越", evidence="value", **base
    )
    assert len(merge_findings([], [first, second])) == 2


@pytest.mark.asyncio
async def test_reset_review_preserves_monotonic_sse_sequence():
    store = EnhancedInMemoryReviewStore()
    source = SourceFile.from_content(
        file_id="f", relative_path="f.py", language="python", content="value = 1\n"
    )
    from code_review.domain.review_models import ReviewSession

    session = ReviewSession.create(
        review_id="events", owner_id="owner", mode=ReviewMode.SINGLE, files=[source]
    )
    await store.create(session)
    await store.publish("events", "owner", "complete", {"round": 1})
    await store.reset_review(session.model_copy(update={"status": "queued"}))
    await store.publish("events", "owner", "complete", {"round": 2})
    events = await store.events_after("events", "owner", 1)
    assert len(events) == 1 and events[0].sequence == 2 and events[0].data == {"round": 2}

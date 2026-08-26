import ast
import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.inference_service import FixCandidateError
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.domain.model_protocol import FixProposal
from code_review.domain.review_models import Finding, ReviewMode, ReviewSession, SourceFile


def proposal_from_request(request, replacement: str, explanation: str) -> FixProposal:
    prompt = request.messages[-1].content
    target_file = re.search(r"文件：([^\n]+)", prompt)
    base_sha = re.search(r"基础 SHA：([0-9a-f]{64})", prompt)
    line_range = re.search(r"修复范围：(\d+)-(\d+)", prompt)
    mode = re.search(
        r"替换模式：(full_file|expression|statement_block|statement|definition|replace_span)",
        prompt,
    )
    anchor = re.search(r"稳定锚点：([^\n]+)", prompt)
    assert target_file and base_sha and line_range and mode and anchor
    return FixProposal(
        target_file=target_file.group(1),
        base_sha=base_sha.group(1),
        start_line=int(line_range.group(1)),
        end_line=int(line_range.group(2)),
        replacement_mode=mode.group(1),
        anchor=anchor.group(1),
        replacement=replacement,
        explanation=explanation,
    )


class FixInference:
    def __init__(self) -> None:
        self.requests = []

    async def propose_fix(self, request):
        self.requests.append(request)
        return proposal_from_request(request, "", "删除未使用的导入")


class SequenceInference:
    def __init__(self, *replacements: str) -> None:
        self.replacements = list(replacements)
        self.requests = []

    async def propose_fix(self, request):
        self.requests.append(request)
        return proposal_from_request(request, self.replacements.pop(0), "重写完整函数")


def finding(finding_id: str, *, line: int = 1, source: str = "static") -> Finding:
    return Finding(
        finding_id=finding_id,
        source=source,
        analyzer="test",
        rule_id="python.unused-import" if line == 1 else "test.pending",
        category="code_quality",
        severity="info",
        confidence=1,
        file_id="file-1",
        start_line=line,
        start_column=1,
        end_line=line,
        end_column=3,
        title="未使用导入" if line == 1 else "保留的问题",
        hover_summary="需要处理",
        detail="需要处理",
        evidence="os" if line == 1 else "value",
        impact="增加噪音",
        suggestion="删除导入",
    )


async def completed_service():
    store = EnhancedInMemoryReviewStore()
    source = SourceFile.from_content(
        file_id="file-1",
        relative_path="pkg/example.py",
        language="python",
        content="import os\nvalue = 1\n",
    )
    session = ReviewSession.create(
        review_id="review-1", owner_id="owner-1", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(
        update={
            "status": "completed",
            "findings": [finding("finding-1"), finding("finding-2", line=2, source="llm")],
        }
    )
    await store.create(session)
    inference = FixInference()
    return HybridReviewService(inference, store), store, inference


@pytest.mark.asyncio
async def test_compliant_parameter_naming_fix_is_rejected_before_model_call():
    store = EnhancedInMemoryReviewStore()
    signature = "def forward_l1(self, user_indices, item_indices, return_components=False):"
    source = SourceFile.from_content(
        file_id="file-1",
        relative_path="model.py",
        language="python",
        content=signature + "\n    return user_indices\n",
    )
    target = finding("naming", source="llm").model_copy(
        update={
            "rule_id": "R003",
            "title": "方法参数命名不一致",
            "evidence": signature,
            "confidence": 0.6,
        }
    )
    session = ReviewSession.create(
        review_id="naming-review", owner_id="owner-1", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    await store.create(session)
    inference = FixInference()
    service = HybridReviewService(inference, store)

    with pytest.raises(FixCandidateError) as caught:
        await service.preview_fix("naming-review", "owner-1", "naming")
    assert caught.value.code == "already_compliant"
    assert inference.requests == []
    assert await store.get("naming-review", "owner-1") == session


@pytest.mark.asyncio
async def test_preview_does_not_persist_and_cancel_keeps_session_unchanged():
    service, store, inference = await completed_service()
    before = await store.get("review-1", "owner-1")

    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    after_preview = await store.get("review-1", "owner-1")
    assert after_preview == before
    assert candidate.diff.startswith("--- a/pkg/example.py")
    assert candidate.base_sha256 == before.files[0].sha256
    assert candidate.after_sha256 != candidate.base_sha256
    assert inference.requests[0].max_output_tokens < 8192

    await service.cancel_fix("review-1", "owner-1", candidate.candidate_id)
    assert await store.get("review-1", "owner-1") == before
    with pytest.raises(LookupError):
        await service.confirm_fix("review-1", "owner-1", candidate.candidate_id)


@pytest.mark.asyncio
async def test_confirm_applies_revision_with_hashes_and_validation():
    service, store, _ = await completed_service()
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")

    decided, restarted = await service.confirm_fix("review-1", "owner-1", candidate.candidate_id)

    assert restarted is None
    assert decided.files[0].content == "value = 1\n"
    assert decided.finding_decisions["finding-1"] == "fixed"
    assert decided.decided_findings["finding-1"].title == "未使用导入"
    assert [item.finding_id for item in decided.findings] == ["finding-2"]
    revision = decided.revisions[-1]
    assert revision.before_sha256 == candidate.base_sha256
    assert revision.after_sha256 == candidate.after_sha256
    assert revision.validation


@pytest.mark.asyncio
async def test_confirm_rejects_sha_conflict_without_applying_candidate():
    service, store, _ = await completed_service()
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    session = await store.get("review-1", "owner-1")
    changed = SourceFile.from_content(
        file_id="file-1",
        relative_path="pkg/example.py",
        language="python",
        content="import os\nvalue = 2\n",
    )
    await store.save(session.model_copy(update={"files": [changed]}))

    with pytest.raises(FixCandidateError, match="SHA") as caught:
        await service.confirm_fix("review-1", "owner-1", candidate.candidate_id)
    assert caught.value.code == "stale_revision"
    current = await store.get("review-1", "owner-1")
    assert current.files[0].content.endswith("value = 2\n")
    assert current.revisions == []


@pytest.mark.asyncio
async def test_accept_risk_and_defer_remain_visible_and_distinct():
    service, _, _ = await completed_service()
    accepted, accepted_review = await service.record_finding_decision(
        "review-1", "owner-1", "finding-1", "accepted_risk"
    )
    deferred, deferred_review = await service.record_finding_decision(
        "review-1", "owner-1", "finding-2", "deferred"
    )

    assert [item.finding_id for item in accepted.findings] == ["finding-2"]
    assert accepted_review is None
    assert [item.finding_id for item in deferred.findings] == ["finding-2"]
    assert deferred_review is None
    assert deferred.finding_decisions == {
        "finding-1": "accepted_risk",
        "finding-2": "deferred",
    }
    assert deferred.ignored_finding_fingerprints
    assert accepted.finding_decision_history[-1].decision == "accepted_risk"
    assert accepted.finding_decision_history[-1].reason


@pytest.mark.asyncio
async def test_accepted_risk_reopen_is_idempotent_and_does_not_call_model():
    service, _, inference = await completed_service()
    accepted, _ = await service.record_finding_decision(
        "review-1", "owner-1", "finding-1", "accepted_risk"
    )
    assert [item.finding_id for item in accepted.findings] == ["finding-2"]
    calls_before = len(inference.requests)

    reopened, revision_retained, already_reopened = await service.reopen_finding(
        "review-1", "owner-1", "finding-1"
    )
    repeated, repeated_revision_retained, repeated_already = await service.reopen_finding(
        "review-1", "owner-1", "finding-1"
    )

    assert revision_retained is False
    assert already_reopened is False
    assert repeated_revision_retained is False
    assert repeated_already is True
    assert repeated == reopened
    assert "finding-1" not in reopened.finding_decisions
    assert reopened.finding_states["finding-1"] == "reopened"
    assert {item.finding_id for item in reopened.findings} == {"finding-1", "finding-2"}
    assert reopened.ignored_finding_fingerprints == []
    assert reopened.finding_decision_history[-1].action == "reopened"
    assert len(inference.requests) == calls_before


@pytest.mark.asyncio
async def test_accepted_risk_can_reopen_then_complete_normal_confirmed_fix_flow():
    service, _, inference = await completed_service()
    await service.record_finding_decision(
        "review-1", "owner-1", "finding-1", "accepted_risk"
    )
    reopened, _, _ = await service.reopen_finding(
        "review-1", "owner-1", "finding-1"
    )
    assert any(item.finding_id == "finding-1" for item in reopened.findings)
    assert inference.requests == []

    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    fixed, revised = await service.confirm_fix(
        "review-1", "owner-1", candidate.candidate_id
    )

    assert fixed.finding_decisions["finding-1"] == "fixed"
    assert fixed.revisions[-1].finding_id == "finding-1"
    assert fixed.finding_decision_history[-1].decision == "fixed"
    assert revised is None
    assert len(inference.requests) == 1


@pytest.mark.asyncio
async def test_fixed_reopen_retains_code_revision_and_restores_active_finding():
    service, _, inference = await completed_service()
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    fixed, _ = await service.confirm_fix(
        "review-1", "owner-1", candidate.candidate_id
    )
    fixed_content = fixed.files[0].content
    calls_before = len(inference.requests)

    reopened, revision_retained, already_reopened = await service.reopen_finding(
        "review-1", "owner-1", "finding-1"
    )

    assert revision_retained is True
    assert already_reopened is False
    assert reopened.files[0].content == fixed_content
    assert reopened.revisions[-1].undone_at is None
    assert any(item.finding_id == "finding-1" for item in reopened.findings)
    assert reopened.finding_decision_history[-1].revision_retained is True
    assert "代码修订仍然保留" in reopened.finding_decision_history[-1].reason
    assert len(inference.requests) == calls_before


@pytest.mark.asyncio
async def test_reopen_is_owner_scoped():
    service, _, _ = await completed_service()
    with pytest.raises(KeyError):
        await service.reopen_finding("review-1", "other-owner", "finding-1")


@pytest.mark.asyncio
async def test_concurrent_pure_decisions_do_not_start_re_review(monkeypatch):
    service, _, _ = await completed_service()
    restarts = []

    async def restart(session):
        restarts.append(session.review_id)
        return session.model_copy(update={"status": "queued", "findings": []})

    monkeypatch.setattr(service, "_restart_review_in_place", restart)
    results = await asyncio.gather(
        service.record_finding_decision(
            "review-1", "owner-1", "finding-1", "accepted_risk"
        ),
        service.record_finding_decision(
            "review-1", "owner-1", "finding-2", "accepted_risk"
        ),
    )
    assert restarts == []
    assert sum(result[1] is not None for result in results) == 0


@pytest.mark.asyncio
async def test_final_decision_with_pending_code_revision_starts_one_re_review(monkeypatch):
    service, store, _ = await completed_service()
    session = await store.get("review-1", "owner-1")
    assert session is not None
    await store.save(
        session.model_copy(
            update={
                "finding_states": {
                    **session.finding_states,
                    "already-fixed": "fixed_pending_revalidation",
                }
            }
        )
    )
    restarts = []

    async def restart(decided):
        restarts.append(decided.review_id)
        return decided.model_copy(update={"status": "queued", "findings": []})

    monkeypatch.setattr(service, "_restart_review_in_place", restart)
    results = await asyncio.gather(
        service.record_finding_decision(
            "review-1", "owner-1", "finding-1", "accepted_risk"
        ),
        service.record_finding_decision(
            "review-1", "owner-1", "finding-2", "accepted_risk"
        ),
    )
    assert restarts == ["review-1"]
    assert sum(result[1] is not None for result in results) == 1


async def function_fix_service(*replacements: str):
    store = EnhancedInMemoryReviewStore()
    source = SourceFile.from_content(
        file_id="file-1",
        relative_path="settings.py",
        language="python",
        content=(
            "def _bool_value(value):\n"
            "    if value is None:\n"
            "        return False\n"
            "    if isinstance(value, str):\n"
            "        return value.lower() == 'true'\n"
            "    return bool(value)\n"
        ),
    )
    target = finding("bool-finding", line=4, source="llm").model_copy(
        update={"rule_id": "semantic.bool", "evidence": "isinstance(value, str)"}
    )
    session = ReviewSession.create(
        review_id="bool-review", owner_id="owner-1", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    await store.create(session)
    inference = SequenceInference(*replacements)
    return HybridReviewService(inference, store), store, inference


@pytest.mark.asyncio
async def test_full_bool_value_definition_replaces_old_body_without_tail():
    replacement = "def _bool_value(value):\n    return bool(value)"
    service, _, _ = await function_fix_service(replacement, "break", replacement)
    candidate = await service.preview_fix("bool-review", "owner-1", "bool-finding")
    prepared = service._fix_candidates[candidate.candidate_id]
    assert candidate.finding_state == "candidate_ready"
    assert candidate.fix_safety == "requires_review"
    assert prepared.plan.scope.expected_symbol == "_bool_value"
    assert prepared.plan.scope.replacement_mode == "definition"
    assert prepared.plan.scope.file_sha == candidate.base_sha256
    decided, _ = await service.confirm_fix("bool-review", "owner-1", candidate.candidate_id)
    content = decided.files[0].content
    tree = ast.parse(content)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(functions) == 1
    assert content.count("return bool(value)") == 1
    assert "isinstance(value, str)" not in content


@pytest.mark.asyncio
async def test_syntax_failure_gets_one_bounded_retry_then_succeeds():
    service, _, inference = await function_fix_service(
        "if value",
        "value is not None",
    )
    candidate = await service.preview_fix("bool-review", "owner-1", "bool-finding")
    assert candidate.validation
    assert len(inference.requests) == 2
    assert "确定性验证" in inference.requests[1].messages[1].content


@pytest.mark.asyncio
async def test_second_syntax_failure_rejects_without_persisting_or_caching():
    service, store, inference = await function_fix_service(
        "if value",
        "return value",
        "def _bool_value(value):\n    break",
    )
    before = await store.get("bool-review", "owner-1")
    with pytest.raises(Exception, match="有界纠正"):
        await service.preview_fix("bool-review", "owner-1", "bool-finding")
    assert await store.get("bool-review", "owner-1") == before
    assert service._fix_candidates == {}
    assert len(inference.requests) == 3


@pytest.mark.asyncio
async def test_candidate_can_be_confirmed_atomically_only_once():
    service, _, _ = await function_fix_service("value is not None")
    candidate = await service.preview_fix("bool-review", "owner-1", "bool-finding")
    results = await asyncio.gather(
        service.confirm_fix("bool-review", "owner-1", candidate.candidate_id),
        service.confirm_fix("bool-review", "owner-1", candidate.candidate_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, LookupError) for result in results) == 1


@pytest.mark.asyncio
async def test_candidate_cache_enforces_review_limit_and_cleans_expired_entries():
    replacement = "value is not None"
    service, _, _ = await function_fix_service(*([replacement] * 10))
    candidates = [
        await service.preview_fix("bool-review", "owner-1", "bool-finding") for _ in range(9)
    ]
    assert len(service._fix_candidates) == 8
    assert candidates[0].candidate_id not in service._fix_candidates
    prepared = service._fix_candidates[candidates[1].candidate_id]
    service._fix_candidates[candidates[1].candidate_id] = replace(
        prepared,
        candidate=prepared.candidate.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        ),
    )
    await service.preview_fix("bool-review", "owner-1", "bool-finding")
    assert candidates[1].candidate_id not in service._fix_candidates


@pytest.mark.asyncio
async def test_accepted_risk_to_deferred_removes_ignore_fingerprint():
    service, _, _ = await completed_service()
    accepted, _ = await service.record_finding_decision(
        "review-1", "owner-1", "finding-1", "accepted_risk"
    )
    assert accepted.ignored_finding_fingerprints
    assert accepted.finding_states["finding-1"] == "accepted_risk"
    deferred, _ = await service.record_finding_decision(
        "review-1", "owner-1", "finding-1", "deferred"
    )
    assert deferred.ignored_finding_fingerprints == []
    assert any(item.finding_id == "finding-1" for item in deferred.findings)
    assert deferred.finding_states["finding-1"] == "deferred"


def test_legacy_keep_migrates_to_accepted_risk():
    session = ReviewSession.create(
        review_id="legacy",
        owner_id="owner-1",
        mode=ReviewMode.SINGLE,
        files=[
            SourceFile.from_content(
                file_id="file-1", relative_path="x.py", language="python", content="value = 1\n"
            )
        ],
    )
    payload = session.model_dump(mode="python")
    payload["owner_id"] = "owner-1"
    payload["finding_decisions"] = {"old": "keep"}
    migrated = ReviewSession.model_validate(payload)
    assert migrated.finding_decisions == {"old": "accepted_risk"}

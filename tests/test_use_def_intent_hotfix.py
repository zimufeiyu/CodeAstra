import ast
import re

import pytest

from code_review.application.evidence_validation import EvidenceValidator
from code_review.application.hybrid_review_service import (
    HybridReviewService,
    _normalize_statement_replacement,
)
from code_review.application.inference_service import FixCandidateError
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.application.static_analysis import StaticAnalyzer
from code_review.domain.model_protocol import FixProposal, ReviewFinding
from code_review.domain.review_models import (
    RepairIntentSelection,
    ReviewMode,
    ReviewSession,
    SourceFile,
)


class NoModelInference:
    def __init__(self) -> None:
        self.requests = []

    async def propose_fix(self, request):
        self.requests.append(request)
        raise AssertionError("ambiguous deterministic finding must not call the model")


class IntentInference:
    def __init__(self, replacement: str) -> None:
        self.replacement = replacement
        self.requests = []

    async def propose_fix(self, request):
        self.requests.append(request)
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
            replacement=self.replacement,
            explanation="按确认的业务行为生成受限候选。",
        )


async def analyze(content: str, *, path: str = "sample.py", files=None):
    source = SourceFile.from_content(
        file_id=path, relative_path=path, language="python", content=content
    )
    project = files or [source]
    result = await StaticAnalyzer().analyze(project, external_standard=False)
    return source, result


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["a = b\n", "def train():\n    a = b\n    return a\n"])
async def test_ambiguous_undefined_name_has_use_def_evidence_and_no_safe_plan(content):
    _, result = await analyze(content)
    finding = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    assert finding.evidence == "b"
    assert finding.use_def_evidence is not None
    assert finding.use_def_evidence.statement_kind == "Assign"
    assert finding.use_def_evidence.outcome == "needs_intent"
    assert finding.symbol_repair_plan is None
    assert {item.kind for item in finding.use_def_evidence.options} >= {"declare_local", "defer"}


@pytest.mark.asyncio
async def test_ambiguous_preview_does_not_call_model_or_change_session():
    source, result = await analyze("a = b\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    session = ReviewSession.create(
        review_id="r", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = NoModelInference()
    service = HybridReviewService(inference, store)
    with pytest.raises(FixCandidateError) as caught:
        await service.preview_fix("r", "u", target.finding_id)
    assert caught.value.code == "needs_intent"
    assert "use_def_evidence" in caught.value.details
    assert inference.requests == []
    assert await store.get("r", "u") == session


@pytest.mark.asyncio
async def test_unique_typo_uses_safe_deterministic_rename_and_valid_net_diff():
    source, result = await analyze("def f(value):\n    result = valu\n    return result\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    assert target.symbol_repair_plan is not None
    assert target.symbol_repair_plan.replacement_name == "value"
    store = EnhancedInMemoryReviewStore()
    session = ReviewSession.create(
        review_id="r2", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    await store.create(session)
    inference = NoModelInference()
    candidate = await HybridReviewService(inference, store).preview_fix(
        "r2", "u", target.finding_id
    )
    assert "-    result = valu" in candidate.diff
    assert "+    result = value" in candidate.diff
    assert candidate.fix_safety == "safe"
    assert inference.requests == []


@pytest.mark.asyncio
async def test_server_issued_rename_intent_creates_preview_and_cancel_does_not_persist():
    source, result = await analyze("def f(value):\n    result = valux\n    return result\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    assert target.symbol_repair_plan is None
    assert target.use_def_evidence is not None
    option = next(
        item for item in target.use_def_evidence.options if item.option_id == "rename:value"
    )
    session = ReviewSession.create(
        review_id="intent", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = NoModelInference()
    service = HybridReviewService(inference, store)
    candidate = await service.preview_fix(
        "intent",
        "u",
        target.finding_id,
        RepairIntentSelection(
            review_id="intent",
            finding_id=target.finding_id,
            base_sha=source.sha256,
            option_id=option.option_id,
            intent_kind=option.kind,
            selected_symbol="value",
        ),
    )
    assert "+    result = value" in candidate.diff
    assert await store.get("intent", "u") == session
    await service.cancel_fix("intent", "u", candidate.candidate_id)
    assert await store.get("intent", "u") == session
    assert inference.requests == []


@pytest.mark.asyncio
async def test_custom_behavior_enters_bound_model_prompt_diff_and_auditable_explanation():
    source, result = await analyze("def f():\n    a = b\n    return a\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    evidence = target.use_def_evidence
    assert evidence is not None
    option = next(item for item in evidence.options if item.kind == "custom_behavior")
    session = ReviewSession.create(
        review_id="custom", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = IntentInference("{}")
    service = HybridReviewService(inference, store)
    user_intent = "缺少 b 时使用当前用户的默认配置"
    candidate = await service.preview_fix(
        "custom",
        "u",
        target.finding_id,
        RepairIntentSelection(
            review_id="custom",
            finding_id=target.finding_id,
            base_sha=source.sha256,
            option_id=option.option_id,
            intent_kind="custom_behavior",
            user_intent=user_intent,
        ),
    )
    assert len(inference.requests) == 1
    prompt = inference.requests[0].messages[-1].content
    assert user_intent in prompt
    assert "修复范围：2-2" in prompt
    assert "+    a = {}" in candidate.diff
    assert user_intent in candidate.explanation
    assert await store.get("custom", "u") == session


@pytest.mark.asyncio
async def test_followup_fix_instruction_enters_model_but_preview_does_not_change_session():
    source, result = await analyze("def f():\n    a = b\n    return a\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    session = ReviewSession.create(
        review_id="followup", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = IntentInference("{}")
    service = HybridReviewService(inference, store)
    instruction = "这里的 b 表示缺省配置；不存在时请使用空字典"

    candidate = await service.preview_followup_fix(
        "followup",
        "u",
        instruction,
        source.sha256,
        {
            "kind": "finding",
            "file_id": source.file_id,
            "finding_id": target.finding_id,
        },
    )

    assert len(inference.requests) == 1
    assert instruction in inference.requests[0].messages[-1].content
    assert instruction in candidate.explanation
    assert "+    a = {}" in candidate.diff
    assert await store.get("followup", "u") == session


@pytest.mark.asyncio
async def test_followup_fix_confirms_once_and_audits_instruction():
    source, result = await analyze("def f():\n    a = b\n    return a\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    blocker = target.model_copy(
        update={
            "finding_id": "semantic-blocker",
            "rule_id": "semantic.pending",
            "source": "llm",
            "title": "待确认的语义问题",
            "evidence": "return a",
            "start_line": 3,
            "end_line": 3,
        }
    )
    session = ReviewSession.create(
        review_id="followup-confirm", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target, blocker]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    instruction = "b 应当按空字典处理"
    service = HybridReviewService(IntentInference("{}"), store)
    candidate = await service.preview_followup_fix(
        "followup-confirm",
        "u",
        instruction,
        source.sha256,
        {
            "kind": "finding",
            "file_id": source.file_id,
            "finding_id": target.finding_id,
        },
    )

    decided, revised = await service.confirm_fix(
        "followup-confirm", "u", candidate.candidate_id
    )

    assert revised is None
    assert "a = {}" in decided.files[0].content
    ast.parse(decided.files[0].content)
    assert instruction in decided.revisions[-1].explanation
    with pytest.raises(LookupError):
        await service.confirm_fix("followup-confirm", "u", candidate.candidate_id)


@pytest.mark.asyncio
async def test_followup_fix_rejects_stale_owner_and_context_mismatch_without_model_call():
    source, result = await analyze("def f():\n    a = b\n    return a\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    session = ReviewSession.create(
        review_id="followup-guard", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = IntentInference("{}")
    service = HybridReviewService(inference, store)
    context = {
        "kind": "finding",
        "file_id": source.file_id,
        "finding_id": target.finding_id,
    }

    with pytest.raises(KeyError):
        await service.preview_followup_fix(
            "followup-guard", "attacker", "use empty mapping", source.sha256, context
        )
    with pytest.raises(FixCandidateError) as stale:
        await service.preview_followup_fix(
            "followup-guard", "u", "use empty mapping", "0" * 64, context
        )
    assert stale.value.code == "stale_revision"
    with pytest.raises(FixCandidateError) as mismatch:
        await service.preview_followup_fix(
            "followup-guard",
            "u",
            "use empty mapping",
            source.sha256,
            {**context, "file_id": "other-file"},
        )
    assert mismatch.value.code == "scope_mismatch"
    with pytest.raises(FixCandidateError) as selection:
        await service.preview_followup_fix(
            "followup-guard",
            "u",
            "use empty mapping",
            source.sha256,
            {"kind": "selection", "file_id": source.file_id},
        )
    assert selection.value.code == "scope_mismatch"
    assert inference.requests == []


@pytest.mark.asyncio
async def test_declare_local_initializer_remains_deterministic_without_model_call():
    source, result = await analyze("def f():\n    a = b\n    return a\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    evidence = target.use_def_evidence
    assert evidence is not None
    option = next(item for item in evidence.options if item.kind == "declare_local")
    session = ReviewSession.create(
        review_id="initializer", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = NoModelInference()
    candidate = await HybridReviewService(inference, store).preview_fix(
        "initializer",
        "u",
        target.finding_id,
        RepairIntentSelection(
            review_id="initializer",
            finding_id=target.finding_id,
            base_sha=source.sha256,
            option_id=option.option_id,
            intent_kind="declare_local",
            initializer="0",
        ),
    )
    assert "+    b = 0" in candidate.diff
    assert inference.requests == []


@pytest.mark.asyncio
async def test_intent_rejects_stale_sha_and_forged_option():
    source, result = await analyze("def f(value):\n    return valux\n")
    target = next(item for item in result.findings if item.rule_id == "python.undefined-name")
    session = ReviewSession.create(
        review_id="guard", owner_id="u", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [target]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    service = HybridReviewService(NoModelInference(), store)
    base = {
        "review_id": "guard",
        "finding_id": target.finding_id,
        "option_id": "rename:value",
        "intent_kind": "rename_existing",
        "selected_symbol": "value",
    }
    with pytest.raises(FixCandidateError) as stale:
        await service.preview_fix(
            "guard",
            "u",
            target.finding_id,
            RepairIntentSelection(base_sha="0" * 64, **base),
        )
    assert stale.value.code == "stale_revision"
    with pytest.raises(FixCandidateError) as forged:
        await service.preview_fix(
            "guard",
            "u",
            target.finding_id,
            RepairIntentSelection(
                base_sha=source.sha256,
                **{**base, "option_id": "rename:attacker"},
            ),
        )
    assert forged.value.code == "ambiguous_symbol"


@pytest.mark.asyncio
async def test_unique_cross_file_export_creates_import_plan():
    source = SourceFile.from_content(
        file_id="a", relative_path="pkg/a.py", language="python", content="result = helper()\n"
    )
    helper = SourceFile.from_content(
        file_id="b",
        relative_path="pkg/utils.py",
        language="python",
        content="def helper():\n    return 1\n",
    )
    result = await StaticAnalyzer().analyze([source, helper], external_standard=False)
    target = next(
        item for item in result.findings if item.file_id == "a" and item.evidence == "helper"
    )
    assert target.symbol_repair_plan is not None
    assert target.symbol_repair_plan.mode == "import"
    assert target.symbol_repair_plan.module == "pkg.utils"


def test_statement_replacement_dedents_once_inside_function():
    replacement = "        a = value\n        return a"
    normalized = _normalize_statement_replacement(replacement, "    ")
    ast.parse("def f():\n" + "\n".join("    " + line for line in normalized.splitlines()) + "\n")
    assert normalized.splitlines()[0] == "    a = value"


def test_statement_fragment_normalization_defers_syntax_authority_to_full_file():
    normalized = _normalize_statement_replacement("a = 1\n  b = 2", "    ")
    assert normalized == "    a = 1\n      b = 2"


def test_vague_type_hypothesis_with_bool_fallback_is_filtered():
    source = SourceFile.from_content(
        file_id="f",
        relative_path="value.py",
        language="python",
        content=(
            "def enabled(value):\n"
            "    if isinstance(value, (int, float)):\n"
            "        return value != 0\n"
            "    return bool(value)\n"
        ),
    )
    draft = ReviewFinding(
        rule_id="type-checking-logic",
        severity="medium",
        confidence=0.9,
        category="correctness",
        file="value.py",
        start_line=2,
        end_line=2,
        title="类型检查逻辑可能不完整",
        evidence="if isinstance(value, (int, float)):",
        impact="可能没有处理其他类型",
        suggestion="考虑添加字典或列表处理",
    )
    assert EvidenceValidator().validate(draft, [source], []) is None


def test_concrete_type_hypothesis_with_contract_is_retained():
    source = SourceFile.from_content(
        file_id="f",
        relative_path="value.py",
        language="python",
        content="def parse(value: int) -> int:\n    return value + 1\n",
    )
    draft = ReviewFinding(
        rule_id="contract-overflow",
        severity="medium",
        confidence=0.9,
        category="correctness",
        file="value.py",
        start_line=2,
        end_line=2,
        title="负数违反接口契约",
        evidence="return value + 1",
        impact="输入 -1 时返回 0",
        suggestion="拒绝负数",
        root_cause_claim={
            "claim": "负数未被拒绝",
            "concrete_failing_input": "value=-1",
            "expected_behavior": "抛出 ValueError",
            "actual_behavior": "返回 0",
            "affected_path": "value.py",
            "repair_invariant": "非负输入结果不变",
            "contract_evidence": "类型注释旁的 API 约定要求非负数",
        },
    )
    assert EvidenceValidator().validate(draft, [source], []) is not None

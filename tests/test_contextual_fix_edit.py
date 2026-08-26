import ast
import re

import pytest

from code_review.application.hybrid_review_service import (
    HybridReviewService,
    _syntax_error_caret,
)
from code_review.application.inference_service import FixCandidateError
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.domain.model_protocol import FixProposal
from code_review.domain.review_models import Finding, ReviewMode, ReviewSession, SourceFile


class ContextualInference:
    def __init__(self, *replacements: str) -> None:
        self.replacements = list(replacements)
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
            replacement=self.replacements.pop(0),
            explanation="按用户目标执行上下文感知替换。",
        )


async def contextual_service(
    content: str,
    *,
    start_line: int,
    end_line: int,
    replacements: tuple[str, ...],
    start_column: int = 1,
    end_column: int = 80,
):
    source = SourceFile.from_content(
        file_id="file-1", relative_path="sample.py", language="python", content=content
    )
    finding = Finding(
        finding_id="finding-1",
        source="llm",
        analyzer="semantic-test",
        rule_id="semantic.target",
        category="correctness",
        severity="medium",
        confidence=0.95,
        file_id=source.file_id,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
        title="目标逻辑需要修改",
        hover_summary="有明确业务目标",
        detail="按用户确认目标修改范围内逻辑",
        evidence=content.splitlines()[start_line - 1].strip(),
        impact="行为不符合已确认目标",
        suggestion="仅替换目标结构",
    )
    session = ReviewSession.create(
        review_id="review-1", owner_id="owner-1", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(update={"status": "completed", "findings": [finding]})
    store = EnhancedInMemoryReviewStore()
    await store.create(session)
    inference = ContextualInference(*replacements)
    return HybridReviewService(inference, store), store, source


@pytest.mark.asyncio
async def test_fenced_indented_if_block_is_validated_after_full_file_assembly():
    source = (
        "def normalize(value):\n"
        "    if value is None:\n"
        "        return None\n"
        "    return value\n"
    )
    replacement = (
        "```python\n"
        "    if value is None:\n"
        "        return ''\n"
        "    elif isinstance(value, str):\n"
        "        return value.strip()\n"
        "```"
    )
    service, store, _ = await contextual_service(
        source, start_line=2, end_line=3, replacements=(replacement,)
    )

    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    prepared = service._fix_candidates[candidate.candidate_id]
    ast.parse(prepared.revised_content)
    compile(prepared.revised_content, "sample.py", "exec")
    assert "```" not in prepared.revised_content
    assert prepared.revised_content.count("if value is None") == 1
    assert prepared.plan.scope.replacement_mode == "statement"
    assert await store.get("review-1", "owner-1") is not None


@pytest.mark.asyncio
async def test_except_raise_from_keeps_legal_nested_indent():
    source = (
        "def load(path):\n"
        "    try:\n"
        "        return read(path)\n"
        "    except Exception as exc:\n"
        "        raise OSError(path) from exc\n"
    )
    service, _, _ = await contextual_service(
        source,
        start_line=5,
        end_line=5,
        replacements=("        raise RuntimeError(f'load failed: {path}') from exc",),
    )
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    revised = service._fix_candidates[candidate.candidate_id].revised_content
    ast.parse(revised)
    assert "        raise RuntimeError" in revised
    assert "OSError" not in revised


@pytest.mark.asyncio
async def test_method_definition_fence_replaces_span_without_old_tail():
    source = (
        "class Parser:\n"
        "    def normalize(self, value):\n"
        "        if value is None:\n"
        "            return None\n"
        "        return value\n"
    )
    replacement = (
        "```python\n"
        "    def normalize(self, value):\n"
        "        return '' if value is None else str(value).strip()\n"
        "```"
    )
    service, _, _ = await contextual_service(
        source,
        start_line=3,
        end_line=4,
        replacements=(replacement, "break", replacement),
    )
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    prepared = service._fix_candidates[candidate.candidate_id]
    tree = ast.parse(prepared.revised_content)
    method = tree.body[0].body[0]
    assert isinstance(method, ast.FunctionDef)
    assert len(method.body) == 1
    assert prepared.revised_content.count("def normalize") == 1
    assert "return value\n" not in prepared.revised_content
    assert prepared.plan.scope.replacement_mode == "definition"


@pytest.mark.asyncio
async def test_full_file_error_drives_one_correction_with_context_and_original_intent():
    source = "def convert(value):\n    if value:\n        return value\n    return None\n"
    service, _, _ = await contextual_service(
        source,
        start_line=2,
        end_line=3,
        replacements=(
            "    if value:\n  return str(value)",
            "    if value:\n        return str(value)",
        ),
    )
    intent = "缩写的目的：有值时提前返回字符串"
    candidate = await service.preview_fix(
        "review-1", "owner-1", "finding-1", followup_instruction=intent
    )
    prepared = service._fix_candidates[candidate.candidate_id]
    compile(prepared.revised_content, "sample.py", "exec")
    assert len(service._inference_service.requests) == 2
    correction = service._inference_service.requests[1].messages[-1].content
    assert intent in correction
    assert "确定性验证" in correction
    assert "目标节点最小完整上下文" in correction
    assert "第 2-3 行" in correction
    assert intent in candidate.explanation


@pytest.mark.asyncio
async def test_second_invalid_candidate_reports_real_full_file_location_and_does_not_cache():
    source = "def convert(value):\n    if value:\n        return value\n    return None\n"
    service, store, _ = await contextual_service(
        source,
        start_line=2,
        end_line=3,
        replacements=(
            "    if value:\n  return str(value)",
            "    if value:\n return str(value)",
            "def convert(value):\n    break",
        ),
    )
    before = await store.get("review-1", "owner-1")
    with pytest.raises(FixCandidateError) as caught:
        await service.preview_fix("review-1", "owner-1", "finding-1")
    assert caught.value.code in {"syntax_invalid", "replacement_indent_invalid"}
    assert caught.value.details["file_line"] == 2
    assert caught.value.details["file_column"] > 1
    assert "sample.py 第 2 行" in str(caught.value)
    assert service._fix_candidates == {}
    assert await store.get("review-1", "owner-1") == before


def test_non_sensitive_line_eight_fixture_records_the_old_unexpected_indent_path():
    assembled = (
        "class Converter:\n"
        "    def convert(self, value):\n"
        "        if value is None:\n"
        "            return None\n"
        "\n"
        "        prefix = str(value)\n"
        "        target = value.strip()\n"
        "                return target\n"
        "        return target\n"
    )
    with pytest.raises(SyntaxError) as caught:
        compile(assembled, "snippet.py", "exec")
    assert caught.value.lineno == 8
    assert caught.value.msg == "unexpected indent"
    caret = _syntax_error_caret(assembled, caught.value)
    assert caret.startswith("8:                 return target\n")
    assert caret.endswith("^")


@pytest.mark.asyncio
async def test_expression_target_rejects_a_statement_without_splicing_it():
    source = "value = left + right\n"
    service, store, _ = await contextual_service(
        source,
        start_line=1,
        end_line=1,
        start_column=9,
        end_column=21,
        replacements=("return left", "value = left"),
    )
    before = await store.get("review-1", "owner-1")
    with pytest.raises(FixCandidateError) as caught:
        await service.preview_fix("review-1", "owner-1", "finding-1")
    assert caught.value.code == "model_output_invalid"
    assert "expression" in str(caught.value)
    assert len(service._inference_service.requests) == 2
    assert service._fix_candidates == {}
    assert await store.get("review-1", "owner-1") == before


@pytest.mark.asyncio
async def test_statement_replacement_consumes_original_leading_indent_once():
    source = (
        "class Converter:\n"
        "    def convert(self, value):\n"
        "        if value is None:\n"
        "            return None\n"
        "        return value\n"
    )
    service, _, _ = await contextual_service(
        source,
        start_line=5,
        end_line=5,
        replacements=("        return str(value).strip()",),
    )
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    prepared = service._fix_candidates[candidate.candidate_id]
    compile(prepared.revised_content, "snippet.py", "exec")
    assert "                return str" not in prepared.revised_content
    assert prepared.revised_content.count("return str(value).strip()") == 1
    assert prepared.plan.scope.replacement_mode == "statement"


@pytest.mark.asyncio
async def test_contiguous_sibling_statements_form_one_bounded_statement_block():
    source = (
        "def convert(value):\n"
        "    prefix = str(value)\n"
        "    normalized = prefix.strip()\n"
        "    result = normalized.lower()\n"
        "    return result\n"
    )
    service, _, _ = await contextual_service(
        source,
        start_line=3,
        end_line=4,
        replacements=("normalized = str(value).strip()\nresult = normalized.casefold()",),
    )
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    prepared = service._fix_candidates[candidate.candidate_id]
    compile(prepared.revised_content, "snippet.py", "exec")
    assert prepared.plan.scope.replacement_mode == "statement_block"
    assert prepared.revised_content.count("normalized =") == 1
    assert prepared.revised_content.count("result =") == 1


@pytest.mark.asyncio
async def test_multiline_expression_is_replaced_as_expression_only():
    source = (
        "def total(value):\n"
        "    result = (\n"
        "        value\n"
        "        + 1\n"
        "    )\n"
        "    return result\n"
    )
    service, _, _ = await contextual_service(
        source,
        start_line=3,
        end_line=4,
        start_column=9,
        end_column=12,
        replacements=("value * 2",),
    )
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    prepared = service._fix_candidates[candidate.candidate_id]
    compile(prepared.revised_content, "snippet.py", "exec")
    assert prepared.plan.scope.replacement_mode == "expression"
    assert "value * 2" in prepared.revised_content
    assert "+ 1" not in prepared.revised_content


@pytest.mark.asyncio
async def test_mixed_tab_space_candidate_is_rejected_safely():
    source = "current = value\n"
    service, _, _ = await contextual_service(
        source,
        start_line=1,
        end_line=1,
        replacements=("return value\n\t return value", "return value\n\t return value"),
    )
    with pytest.raises(FixCandidateError) as caught:
        await service.preview_fix("review-1", "owner-1", "finding-1")
    assert caught.value.code == "replacement_indent_invalid"
    assert service._fix_candidates == {}


@pytest.mark.asyncio
async def test_local_failure_and_correction_escalate_once_to_decorated_function():
    source = (
        "def traced(fn):\n"
        "    return fn\n"
        "\n"
        "class Converter:\n"
        "    @traced\n"
        "    def convert(self, value):\n"
        "        normalized = str(value)\n"
        "        return normalized\n"
    )
    service, _, _ = await contextual_service(
        source,
        start_line=7,
        end_line=7,
        replacements=(
            "break",
            "continue",
            "@traced\ndef convert(self, value):\n    return str(value).strip()",
        ),
    )
    candidate = await service.preview_fix("review-1", "owner-1", "finding-1")
    prepared = service._fix_candidates[candidate.candidate_id]
    compile(prepared.revised_content, "snippet.py", "exec")
    assert len(service._inference_service.requests) == 3
    assert "替换模式：definition" in service._inference_service.requests[2].messages[-1].content
    assert prepared.plan.scope.replacement_mode == "definition"
    assert prepared.revised_content.count("@traced") == 1
    assert prepared.revised_content.count("def convert") == 1
    assert "normalized =" not in prepared.revised_content
    assert "安全边界：模型仅提供语义候选内容" in candidate.explanation


@pytest.mark.asyncio
async def test_local_correction_and_function_fallback_failure_never_cache_or_persist():
    source = "def convert(value):\n    current = value\n    return current\n"
    service, store, _ = await contextual_service(
        source,
        start_line=2,
        end_line=2,
        replacements=(
            "break",
            "continue",
            "def convert(value):\n    break",
        ),
    )
    before = await store.get("review-1", "owner-1")
    with pytest.raises(FixCandidateError) as caught:
        await service.preview_fix("review-1", "owner-1", "finding-1")
    assert caught.value.code == "syntax_invalid"
    assert caught.value.details["file_line"] == 2
    assert caught.value.details["target_mode"] == "definition"
    assert len(service._inference_service.requests) == 3
    assert service._fix_candidates == {}
    assert await store.get("review-1", "owner-1") == before

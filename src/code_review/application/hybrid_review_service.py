from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

from code_review.application.chunk_prompt import ChunkPromptBuilder
from code_review.application.chunk_review_service import ChunkReviewService
from code_review.application.context_budget import ContextBudgeter
from code_review.application.inference_service import ReviewOutputError
from code_review.application.review_aggregator import ReviewAggregator
from code_review.application.review_planner import ReviewPlanner, significant_lines
from code_review.application.static_analysis import StaticAnalyzer
from code_review.config.settings import GatewaySettings
from code_review.domain.model_protocol import (
    ChatMessage,
    InferenceRequest,
    ModelSelection,
)
from code_review.domain.review_chunks import ChunkStatus, ReviewChunk, ReviewPlanningError
from code_review.domain.review_models import (
    Finding,
    FollowupMessage,
    ReviewEvent,
    ReviewMode,
    ReviewOrigin,
    ReviewRevision,
    ReviewSession,
    ReviewSummary,
    SourceFile,
)
from code_review.domain.review_ports import ReviewInferencePort, ReviewStorePort

_MAX_FULL_FILE_SYNTAX_FIX_CHARS = 6000
_MAX_FULL_FILE_SYNTAX_FIX_LINES = 120
_MAX_LOCAL_SYNTAX_BLOCK_CHARS = 6000
_MAX_LOCAL_SYNTAX_BLOCK_LINES = 160
_SYNTAX_FIX_PADDING_LINES = 8
_MR_CHANGED_LINE_MARGIN = 2
_MAX_REVISIONS = 20
_TOP_LEVEL_DEFINITION_RE = re.compile(r"^(?:async\s+def|def|class)\s+")
_TOP_LEVEL_BOUNDARY_RE = re.compile(r"^(?:@|async\s+def|def|class)\s*")


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _finding_in_review_scope(
    finding: Finding,
    session: ReviewSession,
) -> bool:
    origin = session.origin
    if origin is None:
        return True
    path = next(
        (source.relative_path for source in session.files if source.file_id == finding.file_id),
        None,
    )
    if path is None:
        return False
    if path not in origin.selected_paths:
        return True
    if origin.changed_ranges is None:
        return True
    ranges = origin.changed_ranges.get(path, [])
    if finding.rule_id == "python.syntax-error":
        return True
    if not ranges:
        return False
    return any(
        finding.start_line <= changed.end_line + _MR_CHANGED_LINE_MARGIN
        and finding.end_line >= max(1, changed.start_line - _MR_CHANGED_LINE_MARGIN)
        for changed in ranges
    )


def _python_syntax_fix_scope(
    source_lines: list[str],
    finding: Finding,
) -> tuple[int, int]:
    """Prefer a complete top-level definition over an arbitrary line window."""
    if not source_lines:
        return 1, 1

    error_index = min(max(finding.start_line - 1, 0), len(source_lines) - 1)
    definition_start: int | None = None
    for index in range(error_index, -1, -1):
        if _TOP_LEVEL_DEFINITION_RE.match(source_lines[index]):
            definition_start = index
            break

    if definition_start is not None:
        block_start = definition_start
        while block_start > 0 and source_lines[block_start - 1].startswith("@"):
            block_start -= 1

        block_end = len(source_lines)
        for index in range(definition_start + 1, len(source_lines)):
            if _TOP_LEVEL_BOUNDARY_RE.match(source_lines[index]):
                block_end = index
                break

        block = "".join(source_lines[block_start:block_end])
        if (
            block_end - block_start <= _MAX_LOCAL_SYNTAX_BLOCK_LINES
            and len(block) <= _MAX_LOCAL_SYNTAX_BLOCK_CHARS
        ):
            return block_start + 1, block_end

    return (
        max(1, finding.start_line - _SYNTAX_FIX_PADDING_LINES),
        min(len(source_lines), finding.end_line + _SYNTAX_FIX_PADDING_LINES),
    )


def finding_fingerprints(
    finding: Finding,
    files: list[SourceFile],
) -> set[str]:
    """Build stable issue identities from both evidence and the referenced source."""
    source = next((item for item in files if item.file_id == finding.file_id), None)
    path = source.relative_path if source is not None else finding.file_id
    anchors = {" ".join(finding.evidence.casefold().split())}
    if finding.rule_id == "python.syntax-error":
        anchors.add("file-level-python-syntax-error")
    if source is not None:
        lines = source.content.splitlines()
        start = max(0, finding.start_line - 1)
        end = min(len(lines), max(finding.end_line, finding.start_line))
        source_span = " ".join(" ".join(line.casefold().split()) for line in lines[start:end])
        if source_span:
            anchors.add(source_span)

    return {
        hashlib.sha256(
            "\x1f".join((path.casefold(), finding.rule_id.casefold(), anchor)).encode("utf-8")
        ).hexdigest()
        for anchor in anchors
        if anchor
    }


def _finding_summary(findings: list[Finding], *, completed: bool = False) -> ReviewSummary:
    counts = {level: 0 for level in ("critical", "high", "medium", "low", "info")}
    for finding in findings:
        counts[finding.severity] += 1
    if findings and completed:
        text = f"审查完成，发现 {len(findings)} 个需要处理的问题。"
    elif findings:
        text = f"剩余 {len(findings)} 个待处理问题。"
    elif completed:
        text = "审查完成，未发现新的明确问题。"
    else:
        text = "当前问题已全部处理，准备统一复查。"
    return ReviewSummary(
        total=len(findings),
        critical=counts["critical"],
        high=counts["high"],
        medium=counts["medium"],
        low=counts["low"],
        info=counts["info"],
        text=text,
    )


def _followup_code_context(
    session: ReviewSession,
    context: dict[str, object] | None,
) -> tuple[str, str]:
    if not context:
        return "review", "未指定活动代码上下文。"
    file_id = context.get("file_id")
    if not isinstance(file_id, str):
        raise ValueError("未指定有效的活动文件")
    source = next((item for item in session.files if item.file_id == file_id), None)
    if source is None:
        raise ValueError("活动文件不属于当前审查")

    start_line = _positive_int(context.get("start_line"))
    end_line = _positive_int(context.get("end_line"))
    finding_id = context.get("finding_id")
    context_kind = context.get("kind")
    lines = source.content.splitlines()
    finding_section = ""
    if isinstance(finding_id, str):
        finding = next((item for item in session.findings if item.finding_id == finding_id), None)
        if finding is None or finding.file_id != file_id:
            raise ValueError("审查问题不属于当前文件")
        if (start_line is not None and start_line != finding.start_line) or (end_line is not None and end_line != finding.end_line):
            raise ValueError("审查问题的代码范围不匹配")
        start_line, end_line = finding.start_line, finding.end_line
        context_key = f"finding:{finding.finding_id}"
        finding_section = (
            f"\n当前审查问题：[{finding.severity}] {finding.title}\n"
            f"证据：{finding.evidence}\n影响：{finding.impact}\n建议：{finding.suggestion}"
        )
    elif context_kind == "finding":
        raise ValueError("未指定有效的审查问题")
    else:
        if start_line is None or end_line is None or start_line > end_line:
            raise ValueError("所选代码范围无效")
        if end_line > len(lines):
            raise ValueError("所选代码范围超出文件内容")
        selected = context.get("selected_code")
        selected_code = selected.strip() if isinstance(selected, str) else ""
        range_code = "\n".join(lines[start_line - 1 : end_line])
        if selected_code and selected_code not in range_code:
            raise ValueError("所选代码不属于指定范围")
        context_key = "selection:{}:{}:{}:{}".format(
            source.file_id, start_line, end_line,
            hashlib.sha256(range_code.encode("utf-8")).hexdigest()[:16],
        )

    first_index = max(0, start_line - 21)
    last_index = min(len(lines), end_line + 20)
    excerpt = "\n".join(f"{index + 1}: {lines[index]}" for index in range(first_index, last_index))
    if len(excerpt) > 12000:
        excerpt = excerpt[:12000] + "\n…代码上下文已截断…"
    return context_key, (
        f"活动文件：{source.relative_path}（{source.language}）\n"
        f"活动范围：{start_line}-{end_line}\n"
        f"附近源码：\n{excerpt or '空文件'}"
        f"{finding_section}"
    )

class HybridReviewService:
    def __init__(
        self,
        inference_service: ReviewInferencePort,
        store: ReviewStorePort,
        analyzer: StaticAnalyzer | None = None,
        settings: GatewaySettings | None = None,
        model_profiles: dict[str, ModelSelection] | None = None,
        available_model_profile_ids: frozenset[str] | None = None,
        model_context_tokens: dict[str, int] | None = None,
    ) -> None:
        self._inference_service = inference_service
        self._store = store
        self._analyzer = analyzer or StaticAnalyzer()
        self._settings = settings or GatewaySettings()
        self._planner = ReviewPlanner()
        local_profile = ModelSelection(
            profile_id="local-qwen3-8b",
            provider="local",
            model=self._settings.model_name,
            display_name="\u672c\u5730 Qwen3-8B",
        )
        self._model_profiles = model_profiles or {local_profile.profile_id: local_profile}
        self._available_model_profile_ids = (
            available_model_profile_ids
            if available_model_profile_ids is not None
            else frozenset(self._model_profiles)
        )
        context_by_profile = model_context_tokens or {
            profile_id: self._settings.model_context_tokens for profile_id in self._model_profiles
        }
        self._executors = {
            profile_id: ChunkReviewService(
                inference_service=inference_service,
                store=store,
                planner=self._planner,
                prompt_builder=ChunkPromptBuilder(
                    ContextBudgeter(
                        context_tokens=context_by_profile.get(
                            profile_id,
                            self._settings.model_context_tokens,
                        ),
                        safety_tokens=self._settings.context_safety_tokens,
                        minimum_output_tokens=self._settings.minimum_output_tokens,
                        maximum_output_tokens=self._settings.max_output_tokens,
                    )
                ),
                max_split_depth=self._settings.max_chunk_split_depth,
            )
            for profile_id in self._model_profiles
        }
        default_executor = self._executors.get(self._settings.default_model_profile_id)
        if default_executor is None:
            default_executor = next(iter(self._executors.values()))
        self._executor = default_executor
        self._aggregator = ReviewAggregator()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False

    async def create(
        self,
        mode: ReviewMode,
        files: list[SourceFile],
        *,
        owner_id: str,
        origin: ReviewOrigin | None = None,
        model_profile_id: str = "local-qwen3-8b",
        model: ModelSelection | None = None,
    ) -> ReviewSession:
        if not files:
            raise ValueError("至少需要一个待审查文件")
        if model is not None:
            if (
                model.profile_id != "deepseek-api"
                or model.provider != "deepseek"
                or model.selection_source not in {"auto", "manual"}
            ):
                raise ValueError("无效的动态模型配置")
            model_profile_id = model.profile_id
        else:
            model = self._model_profiles.get(model_profile_id)
        if model is None:
            raise ValueError("\u672a\u77e5\u7684\u6a21\u578b\u914d\u7f6e")
        if model_profile_id not in self._available_model_profile_ids:
            raise ValueError(
                f"{model.display_name} "
                "\u5c1a\u672a\u914d\u7f6e API Key\uff0c\u5f53\u524d\u4e0d\u53ef\u7528"
            )
        session = ReviewSession.create(
            review_id=f"review-{uuid.uuid4().hex}",
            owner_id=owner_id,
            mode=mode,
            files=files,
            origin=origin,
            model=model,
            retention_hours=self._settings.review_retention_hours,
        )
        await self._store.create(session)
        return session

    async def get(self, review_id: str, owner_id: str) -> ReviewSession | None:
        return await self._store.get(review_id, owner_id)

    async def list_sessions(self, owner_id: str, limit: int, offset: int) -> list[ReviewSession]:
        return await self._store.list_sessions(owner_id, limit, offset)

    async def rename(self, review_id: str, owner_id: str, title: str) -> ReviewSession:
        normalized = title.strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("标题长度必须为 1–100 个字符")
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        renamed = await self._store.update_title(review_id, owner_id, normalized)
        # Title is updated atomically so a running review cannot overwrite it.
        return renamed

    async def delete(self, review_id: str, owner_id: str) -> bool:
        if await self._store.get(review_id, owner_id) is None:
            return False
        await self.cancel(review_id, owner_id)
        self._tasks.pop(review_id, None)
        return await self._store.delete(review_id, owner_id)

    async def _restart_review_in_place(
        self,
        session: ReviewSession,
    ) -> ReviewSession:
        restarted = session.model_copy(
            update={
                "status": "queued",
                "findings": [],
                "coverage": [],
                "summary": _finding_summary([]),
                "error": None,
            }
        )
        await self._store.reset_review(restarted)
        await self.start(restarted.review_id, restarted.owner_id)
        return restarted

    async def revisions(self, review_id: str, owner_id: str) -> list[ReviewRevision]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        return list(reversed(session.revisions))

    async def undo_revision(
        self,
        review_id: str,
        owner_id: str,
        revision_id: str,
    ) -> ReviewSession:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status not in {"completed", "failed"}:
            raise ValueError("只有已结束的审查可以撤销修改")
        active_revisions = [item for item in session.revisions if item.undone_at is None]
        if not active_revisions or active_revisions[-1].revision_id != revision_id:
            raise ValueError("请从最新一次未撤销的修改开始撤销")
        revision = active_revisions[-1]
        source = next((item for item in session.files if item.file_id == revision.file_id), None)
        if source is None:
            raise ValueError("修改对应的文件已不存在")
        if source.sha256 != revision.after_sha256:
            raise ValueError("当前文件已发生后续变化，无法安全撤销该修改")
        restored_files = [
            SourceFile.from_content(
                file_id=item.file_id,
                relative_path=item.relative_path,
                language=item.language,
                content=(
                    revision.before_content if item.file_id == revision.file_id else item.content
                ),
            )
            for item in session.files
        ]
        restored_revisions = [
            item.model_copy(update={"undone_at": datetime.now(tz=UTC)})
            if item.revision_id == revision_id
            else item
            for item in session.revisions
        ]
        restored_origin = session.origin
        if (
            restored_origin is not None
            and restored_origin.changed_ranges is not None
            and revision.before_changed_ranges is not None
        ):
            changed_ranges = dict(restored_origin.changed_ranges)
            changed_ranges[revision.relative_path] = revision.before_changed_ranges
            restored_origin = restored_origin.model_copy(update={"changed_ranges": changed_ranges})
        decisions = dict(session.finding_decisions)
        decisions.pop(revision.finding_id, None)
        restored = session.model_copy(
            update={
                "files": restored_files,
                "origin": restored_origin,
                "revisions": restored_revisions,
                "finding_decisions": decisions,
            }
        )
        await self._store.save(restored)
        return await self._restart_review_in_place(restored)

    async def decide_finding(
        self,
        review_id: str,
        owner_id: str,
        finding_id: str,
        decision: Literal["apply", "keep"],
    ) -> tuple[ReviewSession, ReviewSession | None, str | None]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status not in {"completed", "failed"}:
            raise ValueError("只有已结束的审查可以处理修复建议")
        finding = next(
            (item for item in session.findings if item.finding_id == finding_id),
            None,
        )
        if finding is None:
            raise LookupError(finding_id)

        decisions = dict(session.finding_decisions)
        decisions[finding_id] = decision
        ignored = set(session.ignored_finding_fingerprints)
        if decision == "keep":
            ignored.update(finding_fingerprints(finding, session.files))
            remaining = [item for item in session.findings if item.finding_id != finding_id]
            decided = session.model_copy(
                update={
                    "finding_decisions": decisions,
                    "ignored_finding_fingerprints": sorted(ignored),
                    "findings": remaining,
                    "summary": _finding_summary(remaining),
                }
            )
            await self._store.save(decided)
            if remaining:
                return decided, None, None
            revised = await self._restart_review_in_place(decided)
            return decided, revised, None

        source = next(item for item in session.files if item.file_id == finding.file_id)
        source_lines = source.content.splitlines(keepends=True)
        is_python_syntax_fix = (
            source.language == "python" and finding.rule_id == "python.syntax-error"
        )
        replace_whole_file = (
            is_python_syntax_fix
            and len(source.content) <= _MAX_FULL_FILE_SYNTAX_FIX_CHARS
            and len(source_lines) <= _MAX_FULL_FILE_SYNTAX_FIX_LINES
        )
        if replace_whole_file:
            fix_start = 1
            fix_end = len(source_lines)
            excerpt_start = 0
            excerpt_end = len(source_lines)
        elif is_python_syntax_fix:
            fix_start, fix_end = _python_syntax_fix_scope(source_lines, finding)
            excerpt_start = max(0, fix_start - 11)
            excerpt_end = min(len(source_lines), fix_end + 10)
        else:
            fix_start = finding.start_line
            fix_end = finding.end_line
            excerpt_start = max(0, finding.start_line - 21)
            excerpt_end = min(len(source_lines), finding.end_line + 20)

        excerpt = "".join(
            f"{index + 1}: {source_lines[index]}" for index in range(excerpt_start, excerpt_end)
        )
        parser_detail = ""
        original_syntax_error: SyntaxError | None = None
        if is_python_syntax_fix:
            try:
                ast.parse(source.content, filename=source.relative_path)
            except SyntaxError as syntax_error:
                original_syntax_error = syntax_error
                parser_detail = (
                    f"\nPython 解析器报错：{syntax_error.msg}"
                    f"（第 {syntax_error.lineno or finding.start_line} 行，"
                    f"第 {syntax_error.offset or finding.start_column} 列）"
                )

        if replace_whole_file:
            scope_instruction = (
                "这是 Python 语法错误。replacement 必须是修复后的完整文件，"
                "不得包含行号或代码围栏，并且必须能通过 Python 语法解析；"
            )
        else:
            scope_instruction = (
                f"replacement 只包含替换第 {fix_start}-{fix_end} 行的完整代码，"
                "不得包含行号或代码围栏；"
            )

        proposal = await self._inference_service.propose_fix(
            InferenceRequest(
                request_id=f"{review_id}:fix:{finding_id}:{uuid.uuid4().hex}",
                messages=[
                    ChatMessage(
                        role="system",
                        content=(
                            "你是代码修复助手。只修复指定问题，不改变无关行为。"
                            f"{scope_instruction}"
                            "\u53ea\u8fd4\u56de\u7b26\u5408 FixProposal schema \u7684 "
                            "JSON \u5bf9\u8c61\uff0c"
                            "\u4e0d\u8981\u4f7f\u7528 Markdown\uff1b"
                            "explanation 使用简洁中文。"
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            f"文件：{source.relative_path}\n"
                            f"问题：{finding.title}\n"
                            f"修复范围：{fix_start}-{fix_end}\n"
                            f"证据：{finding.evidence}\n"
                            f"建议：{finding.suggestion}"
                            f"{parser_detail}\n"
                            f"上下文：\n{excerpt}"
                        ),
                    ),
                ],
                max_output_tokens=8192,
                temperature=0.0,
                response_format="fix",
                model_profile_id=session.model.profile_id,
            )
        )

        newline = "\r\n" if "\r\n" in source.content else "\n"
        replacement = proposal.replacement.rstrip("\r\n")
        if replace_whole_file:
            if not replacement.strip():
                raise ReviewOutputError("生成的修复为空，已保留原代码和问题。")
            revised_content = replacement
            if source.content.endswith(("\n", "\r")):
                revised_content += newline
        else:
            if fix_end < len(source_lines) or source.content.endswith(("\n", "\r")):
                replacement += newline
            revised_content = (
                "".join(source_lines[: fix_start - 1])
                + replacement
                + "".join(source_lines[fix_end:])
            )

        if revised_content == source.content:
            raise ReviewOutputError("生成的修复与原代码相同，未应用修复，问题已保留。")
        advanced_to_later_error = False
        if source.language == "python":
            try:
                ast.parse(revised_content, filename=source.relative_path)
            except SyntaxError as syntax_error:
                replacement_line_count = len(replacement.splitlines())
                revised_fix_end = fix_start + replacement_line_count - 1
                advanced_to_later_error = (
                    not replace_whole_file
                    and original_syntax_error is not None
                    and original_syntax_error.lineno is not None
                    and fix_start <= original_syntax_error.lineno <= fix_end
                    and syntax_error.lineno is not None
                    and syntax_error.lineno > revised_fix_end
                )
                if not advanced_to_later_error:
                    raise ReviewOutputError(
                        f"生成的修复仍有 Python 语法错误：{syntax_error.msg}"
                        f"（第 {syntax_error.lineno or 1} 行），未应用修复，问题已保留。"
                    ) from syntax_error

        candidate_source = SourceFile.from_content(
            file_id=source.file_id,
            relative_path=source.relative_path,
            language=source.language,
            content=revised_content,
        )
        baseline_analysis = await self._analyzer.analyze([source])
        candidate_analysis = await self._analyzer.analyze([candidate_source])

        def static_identity(item: Finding) -> tuple[str, str]:
            return item.rule_id, " ".join(item.evidence.casefold().split())

        baseline_identities = {static_identity(item) for item in baseline_analysis.findings}
        introduced = [
            item
            for item in candidate_analysis.findings
            if static_identity(item) not in baseline_identities
            and not (advanced_to_later_error and item.rule_id == "python.syntax-error")
        ]
        if introduced:
            titles = "\uff1b".join(item.title for item in introduced[:3])
            raise ReviewOutputError(
                "\u751f\u6210\u7684\u4fee\u590d\u5f15\u5165\u4e86\u65b0\u7684\u9759\u6001\u95ee\u9898\uff1a"
                f"{titles}\u3002\u672a\u5e94\u7528\u4fee\u590d\uff0c\u539f\u95ee\u9898\u5df2\u4fdd\u7559\u3002"
            )
        target_still_present = any(
            item.rule_id == finding.rule_id
            and (
                (finding.rule_id == "python.syntax-error" and not advanced_to_later_error)
                or static_identity(item)[1] == static_identity(finding)[1]
            )
            for item in candidate_analysis.findings
        )
        if finding.source == "static" and target_still_present:
            raise ReviewOutputError(
                "\u751f\u6210\u7684\u4fee\u590d\u672a\u89e3\u51b3\u76ee\u6807\u95ee\u9898\uff0c"
                "\u672a\u5e94\u7528\u4fee\u590d\uff0c\u539f\u95ee\u9898\u5df2\u4fdd\u7559\u3002"
            )

        revised_files = [
            SourceFile.from_content(
                file_id=item.file_id,
                relative_path=item.relative_path,
                language=item.language,
                content=revised_content if item.file_id == source.file_id else item.content,
            )
            for item in session.files
        ]

        replacement_line_count = len(proposal.replacement.rstrip("\r\n").splitlines())
        replaced_line_count = fix_end - fix_start + 1
        line_delta = replacement_line_count - replaced_line_count
        remaining_findings: list[Finding] = []
        for item in session.findings:
            if item.finding_id == finding_id:
                continue
            if item.file_id != finding.file_id or item.end_line < fix_start:
                remaining_findings.append(item)
                continue
            if item.start_line > fix_end:
                remaining_findings.append(
                    item.model_copy(
                        update={
                            "start_line": item.start_line + line_delta,
                            "end_line": item.end_line + line_delta,
                        }
                    )
                )
            # Overlapping findings are stale and will be checked in the batch re-review.

        before_changed_ranges = None
        revised_origin = session.origin
        if (
            revised_origin is not None
            and revised_origin.changed_ranges is not None
            and source.relative_path in revised_origin.changed_ranges
        ):
            before_changed_ranges = list(revised_origin.changed_ranges[source.relative_path])
            adjusted_ranges = []
            replacement_end = fix_start + max(1, replacement_line_count) - 1
            for changed in before_changed_ranges:
                if changed.end_line < fix_start:
                    adjusted_ranges.append(changed)
                elif changed.start_line > fix_end:
                    adjusted_ranges.append(
                        changed.model_copy(
                            update={
                                "start_line": changed.start_line + line_delta,
                                "end_line": changed.end_line + line_delta,
                            }
                        )
                    )
                else:
                    adjusted_ranges.append(
                        changed.model_copy(
                            update={
                                "start_line": min(changed.start_line, fix_start),
                                "end_line": max(replacement_end, changed.end_line + line_delta),
                            }
                        )
                    )
            changed_ranges = dict(revised_origin.changed_ranges)
            changed_ranges[source.relative_path] = adjusted_ranges
            revised_origin = revised_origin.model_copy(update={"changed_ranges": changed_ranges})
        unified_diff = "".join(
            difflib.unified_diff(
                source.content.splitlines(keepends=True),
                revised_content.splitlines(keepends=True),
                fromfile=f"a/{source.relative_path}",
                tofile=f"b/{source.relative_path}",
            )
        )
        revision = ReviewRevision(
            revision_id=f"revision-{uuid.uuid4().hex}",
            finding_id=finding_id,
            file_id=source.file_id,
            relative_path=source.relative_path,
            created_at=datetime.now(tz=UTC),
            before_content=source.content,
            before_changed_ranges=before_changed_ranges,
            after_sha256=next(
                item.sha256 for item in revised_files if item.file_id == source.file_id
            ),
            diff=unified_diff,
            explanation=proposal.explanation,
        )
        decided = session.model_copy(
            update={
                "finding_decisions": decisions,
                "files": revised_files,
                "origin": revised_origin,
                "findings": remaining_findings,
                "summary": _finding_summary(remaining_findings),
                "revisions": [*session.revisions, revision][-_MAX_REVISIONS:],
            }
        )
        await self._store.save(decided)
        if remaining_findings:
            return decided, None, proposal.explanation

        revised = await self._restart_review_in_place(decided)
        return decided, revised, proposal.explanation

    async def followups(
        self,
        review_id: str,
        owner_id: str,
        context: dict[str, object] | None = None,
    ) -> list[FollowupMessage]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        context_key, _ = _followup_code_context(session, context)
        return await self._store.followups(review_id, owner_id, context_key)

    async def ask_followup(
        self,
        review_id: str,
        owner_id: str,
        question: str,
        context: dict[str, object] | None = None,
    ) -> list[FollowupMessage]:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        if session.status != "completed":
            raise ValueError("只有已完成的审查可以继续追问")
        context_key, code_context = _followup_code_context(session, context)
        previous = await self._store.followups(review_id, owner_id, context_key)
        history_lines = [
            f"{'用户' if item.role == 'user' else '助手'}：{item.content}"
            for item in previous[-8:]
        ]
        previous_context = "\n".join(history_lines)
        if len(previous_context) > 6000:
            previous_context = "…较早内容已省略…\n" + previous_context[-6000:]
        prompt = (
            f"当前代码上下文：\n{code_context}\n"
            f"审查摘要：{session.summary.text}\n"
            f"当前上下文的此前追问：\n{previous_context or '无'}\n"
            f"本次问题：{question.strip()}"
        )
        request_id = f"{review_id}:followup:{uuid.uuid4().hex}"
        answer_text = await self._inference_service.answer_followup(
            InferenceRequest(
                request_id=request_id,
                messages=[
                    ChatMessage(
                        role="system",
                        content=(
                            "你是代码审查追问助手。仅根据当前代码上下文、审查摘要"
                            "和当前上下文的对话历史回答。不得虚构代码事实；使用简洁、可读的中文。"
                        ),
                    ),
                    ChatMessage(role="user", content=prompt),
                ],
                max_output_tokens=4096,
                temperature=0.1,
                response_format="text",
                model_profile_id=session.model.profile_id,
            )
        )
        created_at = datetime.now(tz=UTC)
        exchange_id = uuid.uuid4().hex
        user_message = FollowupMessage(
            message_id=f"followup-question-{exchange_id}", review_id=review_id,
            role="user", context_key=context_key, content=question.strip(), created_at=created_at,
        )
        assistant_message = FollowupMessage(
            message_id=f"followup-answer-{exchange_id}", review_id=review_id,
            role="assistant", context_key=context_key, content=answer_text,
            created_at=created_at + timedelta(microseconds=1),
        )
        await self._store.append_followup_exchange(user_message, assistant_message, owner_id, context_key)
        return [user_message, assistant_message]
    async def start(self, review_id: str, owner_id: str) -> None:
        if await self._store.get(review_id, owner_id) is None:
            raise KeyError(review_id)
        existing = self._tasks.get(review_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self.run(review_id, owner_id))
        self._tasks[review_id] = task

        def cleanup(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(review_id) is completed:
                self._tasks.pop(review_id, None)

        task.add_done_callback(cleanup)

    async def run(self, review_id: str, owner_id: str) -> None:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            raise KeyError(review_id)
        owner_id = session.owner_id
        if session.status in {"completed", "cancelled"}:
            return
        executor = self._executors.get(session.model.profile_id, self._executor)
        try:
            planning = session.model_copy(update={"status": "planning", "error": None})
            await self._store.transition_review(
                planning,
                "stage",
                {
                    "stage": "planning",
                    "progress": 5,
                    "message": "正在规划完整审查范围",
                },
            )
            static_result = await self._analyzer.analyze(planning.files)
            ignored = set(planning.ignored_finding_fingerprints)
            scoped_static_findings = [
                finding
                for finding in static_result.findings
                if _finding_in_review_scope(finding, planning)
            ]
            visible_static_findings = [
                finding
                for finding in scoped_static_findings
                if ignored.isdisjoint(finding_fingerprints(finding, planning.files))
            ]
            planning = planning.model_copy(
                update={
                    "findings": visible_static_findings,
                    "coverage": static_result.coverage,
                }
            )
            await self._store.save(planning)
            syntax_blockers = [
                finding
                for finding in scoped_static_findings
                if finding.rule_id == "python.syntax-error"
            ]
            visible_syntax_blockers = [
                finding
                for finding in visible_static_findings
                if finding.rule_id == "python.syntax-error"
            ]
            if syntax_blockers:
                for finding in visible_static_findings:
                    await self._store.publish(
                        review_id,
                        owner_id,
                        "finding",
                        finding.model_dump(mode="json"),
                    )
                summary = _finding_summary(visible_static_findings, completed=True)
                if visible_syntax_blockers:
                    summary_text = (
                        f"语法检查发现 {len(visible_syntax_blockers)} 个阻断问题；"
                        "请先修复 Python 语法，再进行完整模型审查。"
                    )
                else:
                    summary_text = (
                        "已选择暂不处理该文件的 Python 语法问题；代码仍无法解析，已跳过模型审查。"
                    )
                summary = summary.model_copy(update={"text": summary_text})
                completed = planning.model_copy(
                    update={
                        "status": "completed",
                        "findings": visible_static_findings,
                        "summary": summary,
                        "error": None,
                    }
                )
                await self._store.transition_review(
                    completed,
                    "complete",
                    {
                        "review_id": review_id,
                        "summary": summary.model_dump(mode="json"),
                        "coverage_percent": 100.0,
                        "semantic_review_deferred": True,
                    },
                )
                return

            existing_chunks = await self._store.chunks(review_id, owner_id)
            if existing_chunks:
                chunks = existing_chunks
                significant = {item.file_id: significant_lines(item) for item in planning.files}
            else:
                plan = self._planner.plan(review_id, planning.files)
                chunks = plan.chunks
                significant = plan.significant_lines
                await self._store.save_chunks(chunks, owner_id)
            for finding in visible_static_findings:
                await self._store.publish(
                    review_id,
                    owner_id,
                    "finding",
                    finding.model_dump(mode="json"),
                )

            reviewing = planning.model_copy(update={"status": "reviewing"})
            await self._store.transition_review(
                reviewing,
                "stage",
                {
                    "stage": "reviewing",
                    "progress": 15,
                    "message": "正在分块进行语义审查",
                },
            )
            pending = [item for item in chunks if item.status == ChunkStatus.PENDING]
            while pending:
                batch = pending[: self._settings.review_queue_limit]
                results = await asyncio.gather(
                    *(
                        executor.execute(
                            item,
                            reviewing.files,
                            visible_static_findings,
                            owner_id=owner_id,
                            model_profile_id=reviewing.model.profile_id,
                            model_name=reviewing.model.model,
                        )
                        for item in batch
                    )
                )
                children = [child for result in results for child in result]
                current_chunks = await self._store.chunks(review_id, owner_id)
                queued_ids = {item.chunk_id for item in children}
                pending = children + [
                    item
                    for item in current_chunks
                    if item.status == ChunkStatus.PENDING and item.chunk_id not in queued_ids
                ]
                await self._publish_progress(review_id, owner_id, current_chunks)

            current_chunks = await self._store.chunks(review_id, owner_id)
            aggregating = reviewing.model_copy(update={"status": "aggregating"})
            await self._store.transition_review(
                aggregating,
                "stage",
                {
                    "stage": "aggregating",
                    "progress": 95,
                    "message": "正在校验覆盖率并合并问题",
                },
            )
            result = self._aggregator.aggregate(
                aggregating,
                current_chunks,
                static_findings=visible_static_findings,
                chunk_findings=await self._store.chunk_findings(review_id, owner_id),
                significant_lines=significant,
            )
            ignored = set(aggregating.ignored_finding_fingerprints)
            visible_findings = [
                finding
                for finding in result.findings
                if _finding_in_review_scope(finding, aggregating)
                and ignored.isdisjoint(finding_fingerprints(finding, aggregating.files))
            ]
            completed_summary = result.summary
            if len(visible_findings) != len(result.findings):
                completed_summary = _finding_summary(visible_findings, completed=True)
            completed = aggregating.model_copy(
                update={
                    "status": "completed",
                    "findings": visible_findings,
                    "summary": completed_summary,
                    "error": None,
                }
            )
            await self._store.transition_review(
                completed,
                "complete",
                {
                    "review_id": review_id,
                    "summary": completed.summary.model_dump(mode="json"),
                    "coverage_percent": result.coverage_percent,
                },
            )
        except asyncio.CancelledError:
            if self._shutting_down:
                raise
            current = await self._store.get(review_id, owner_id)
            if current is not None and current.status != "cancelled":
                cancelled = current.model_copy(update={"status": "cancelled"})
                await self._store.transition_review(
                    cancelled,
                    "cancelled",
                    {"review_id": review_id},
                )
            raise
        except Exception as error:
            current = await self._store.get(review_id, owner_id)
            if current is not None:
                chunks = await self._store.chunks(review_id, owner_id)
                overflow = any(str(item.error_code) == "context_overflow" for item in chunks)
                if overflow:
                    message = "输入代码过长，超出模型上下文范围，请减少文件或分批审查。"
                    retryable = False
                elif isinstance(error, ReviewPlanningError):
                    message = f"无法规划代码审查：{error}"
                    retryable = False
                else:
                    message = "模型审查失败，已保留完成分块，可继续未完成审查。"
                    retryable = True
                failed = current.model_copy(update={"status": "failed", "error": message})
                code = error.code if isinstance(error, ReviewPlanningError) else "review_failed"
                await self._store.transition_review(
                    failed,
                    "error",
                    {
                        "code": code,
                        "message": message,
                        "retryable": retryable,
                    },
                )

    async def _publish_progress(
        self,
        review_id: str,
        owner_id: str,
        chunks: list[ReviewChunk],
    ) -> None:
        leaves = [item for item in chunks if item.status != ChunkStatus.SUPERSEDED]
        completed = sum(item.status == ChunkStatus.COMPLETED for item in leaves)
        failed = sum(item.status == ChunkStatus.FAILED for item in leaves)
        running = sum(
            item.status in {ChunkStatus.RUNNING, ChunkStatus.VALIDATING} for item in leaves
        )
        queued = sum(item.status in {ChunkStatus.PENDING, ChunkStatus.QUEUED} for item in leaves)
        coverage = 100.0 if not leaves else completed * 100.0 / len(leaves)
        await self._store.publish(
            review_id,
            owner_id,
            "progress",
            {
                "total": len(leaves),
                "completed": completed,
                "failed": failed,
                "running": running,
                "queued": queued,
                "coverage_percent": coverage,
            },
        )

    async def resume(self, review_id: str, owner_id: str) -> bool:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            return False
        if session.status == "completed":
            return True
        chunks = await self._store.chunks(review_id, owner_id)
        reset = [
            item.model_copy(
                update={
                    "status": ChunkStatus.PENDING,
                    "error_code": None,
                    "error_message": None,
                }
            )
            if item.status == ChunkStatus.FAILED
            else item
            for item in chunks
        ]
        await self._store.save_chunks(reset, owner_id)
        await self._store.save(session.model_copy(update={"status": "queued", "error": None}))
        await self.start(review_id, owner_id)
        return True

    async def recover(self) -> list[str]:
        recovered: list[str] = []
        for review_id, owner_id in await self._store.recoverable_reviews():
            await self.start(review_id, owner_id)
            recovered.append(review_id)
        return recovered

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def cancel(self, review_id: str, owner_id: str) -> bool:
        session = await self._store.get(review_id, owner_id)
        if session is None:
            return False
        if session.status in {"completed", "failed", "cancelled"}:
            return True
        task = self._tasks.get(review_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        current = await self._store.get(review_id, owner_id)
        if current is not None and current.status != "cancelled":
            cancelled = current.model_copy(update={"status": "cancelled"})
            await self._store.transition_review(
                cancelled,
                "cancelled",
                {"review_id": review_id},
            )
        return True

    async def events(
        self,
        review_id: str,
        owner_id: str,
        after_sequence: int = 0,
    ) -> AsyncIterator[ReviewEvent]:
        sequence = after_sequence
        while True:
            session = await self._store.get(review_id, owner_id)
            if session is None:
                return
            events = await self._store.events_after(review_id, owner_id, sequence)
            for event in events:
                sequence = event.sequence
                yield event
            if session.status in {"completed", "cancelled", "failed"} and not events:
                return
            await asyncio.sleep(0.05)

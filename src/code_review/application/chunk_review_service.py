from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import uuid4

import httpx

from code_review.application.chunk_prompt import ChunkPromptBuilder
from code_review.application.evidence_validation import EvidenceValidator
from code_review.application.finding_verifier import FindingVerifier
from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.recheck_context import current_recheck_attempt
from code_review.application.review_planner import ReviewPlanner
from code_review.application.semantic_judge import SemanticJudgePort
from code_review.domain.model_protocol import ContextWindowExceededError
from code_review.domain.review_chunks import (
    ChunkAttempt,
    ChunkErrorCode,
    ChunkStatus,
    ReviewChunk,
    ReviewPlanningError,
)
from code_review.domain.review_models import Finding, SourceFile
from code_review.domain.review_ports import ReviewInferencePort, ReviewStorePort
from code_review.infrastructure.sglang.registry import AllInstancesUnavailable


class ChunkReviewService:
    def __init__(
        self,
        *,
        inference_service: ReviewInferencePort,
        store: ReviewStorePort,
        planner: ReviewPlanner,
        prompt_builder: ChunkPromptBuilder,
        max_split_depth: int,
        finding_verifier: FindingVerifier | None = None,
        metrics: PipelineMetrics | None = None,
        semantic_judge: SemanticJudgePort | None = None,
    ) -> None:
        self._inference = inference_service
        self._store = store
        self._planner = planner
        self._prompt_builder = prompt_builder
        self._max_split_depth = max_split_depth
        self._validator = EvidenceValidator(finding_verifier)
        self._metrics = metrics or PipelineMetrics()
        self._semantic_judge = semantic_judge
        self._semantic_judge_calls: dict[str, int] = {}
        self._semantic_judge_call_limit = 8

    async def _ensure_current_recheck(self, review_id: str, owner_id: str) -> None:
        attempt_id = current_recheck_attempt.get()
        if attempt_id is None:
            return
        if not await self._store.is_recheck_attempt_current(
            review_id, owner_id, attempt_id
        ):
            raise asyncio.CancelledError

    async def execute(
        self,
        chunk: ReviewChunk,
        files: list[SourceFile],
        static_findings: list[Finding],
        *,
        owner_id: str,
        model_profile_id: str = "local-qwen3-8b",
        model_name: str = "qwen3-8b",
    ) -> list[ReviewChunk]:
        last_error: Exception | None = None
        strategies: tuple[Literal["original", "trim_context"], ...] = ("original", "trim_context")
        for number, strategy in enumerate(strategies, start=1):
            await self._ensure_current_recheck(chunk.review_id, owner_id)
            working = (
                chunk
                if strategy == "original"
                else chunk.model_copy(update={"context_references": []})
            )
            running = working.model_copy(
                update={
                    "status": ChunkStatus.RUNNING,
                    "attempt_count": number,
                    "error_code": None,
                    "error_message": None,
                }
            )
            await self._store.transition_chunk(
                running,
                owner_id,
                "chunk",
                {
                    "chunk_id": running.chunk_id,
                    "status": ChunkStatus.RUNNING,
                    "attempt": number,
                    "strategy": strategy,
                },
            )
            attempt_id = f"attempt-{uuid4().hex}"
            started = perf_counter()
            attempt = ChunkAttempt(
                attempt_id=attempt_id,
                review_id=chunk.review_id,
                chunk_id=chunk.chunk_id,
                attempt_number=number,
                strategy=strategy,
                request_id=f"{chunk.review_id}:{chunk.chunk_id}:{attempt_id}",
            )
            await self._store.record_attempt(attempt, owner_id)
            try:
                request = self._prompt_builder.build(running, attempt_id).model_copy(
                    update={"model_profile_id": model_profile_id}
                )
                self._metrics.increment("model_review_calls")
                response = await self._inference.review(request)
                await self._ensure_current_recheck(chunk.review_id, owner_id)
                validating = running.model_copy(update={"status": ChunkStatus.VALIDATING})
                await self._store.transition_chunk(
                    validating,
                    owner_id,
                    "chunk",
                    {"chunk_id": chunk.chunk_id, "status": ChunkStatus.VALIDATING},
                )
                findings: list[Finding] = []
                for draft in response.findings:
                    finding = self._validator.validate(
                        draft,
                        files,
                        static_findings,
                        target_chunk=chunk,
                        analyzer_name=model_name,
                    )
                    if finding is not None:
                        verdict = None
                        calls = self._semantic_judge_calls.get(chunk.review_id, 0)
                        if (
                            finding.source == "llm"
                            and self._semantic_judge is not None
                            and calls < self._semantic_judge_call_limit
                        ):
                            verdict = await self._semantic_judge.verify(finding)
                            self._semantic_judge_calls[chunk.review_id] = calls + 1
                            self._metrics.increment("semantic_judge_calls")
                        if verdict is None or (
                            verdict.accepted and verdict.confidence >= 0.7
                        ):
                            findings.append(finding)
                await self._ensure_current_recheck(chunk.review_id, owner_id)
                await self._store.save_chunk_findings(chunk.chunk_id, owner_id, findings)
                completed = running.model_copy(
                    update={
                        "status": ChunkStatus.COMPLETED,
                        "error_code": None,
                        "error_message": None,
                    }
                )
                await self._store.record_attempt(
                    attempt.model_copy(
                        update={
                            "output_tokens_budget": request.max_output_tokens,
                            "latency_ms": (perf_counter() - started) * 1000,
                            "completed_at": datetime.now(tz=UTC),
                        }
                    ),
                    owner_id,
                )
                await self._store.transition_chunk(
                    completed,
                    owner_id,
                    "chunk",
                    {
                        "chunk_id": chunk.chunk_id,
                        "status": ChunkStatus.COMPLETED,
                        "finding_count": len(findings),
                    },
                )
                return []
            except Exception as error:
                await self._ensure_current_recheck(chunk.review_id, owner_id)
                last_error = error
                await self._store.record_attempt(
                    attempt.model_copy(
                        update={
                            "latency_ms": (perf_counter() - started) * 1000,
                            "completed_at": datetime.now(tz=UTC),
                            "error_code": self._error_code(error, model_profile_id),
                            "error_message": self._sanitize_error(error, model_profile_id),
                        }
                    ),
                    owner_id,
                )

        if chunk.split_depth < self._max_split_depth:
            await self._ensure_current_recheck(chunk.review_id, owner_id)
            children = self._planner.split(chunk)
            if children:
                await self._store.replace_chunk(chunk, children, owner_id)
                await self._store.publish(
                    chunk.review_id,
                    owner_id,
                    "chunk",
                    {
                        "chunk_id": chunk.chunk_id,
                        "status": ChunkStatus.SUPERSEDED,
                        "children": [item.chunk_id for item in children],
                    },
                )
                return children

        await self._ensure_current_recheck(chunk.review_id, owner_id)
        failed = chunk.model_copy(
            update={
                "status": ChunkStatus.FAILED,
                "attempt_count": len(strategies),
                "error_code": self._error_code(last_error, model_profile_id),
                "error_message": self._sanitize_error(last_error, model_profile_id),
            }
        )
        await self._store.transition_chunk(
            failed,
            owner_id,
            "chunk",
            {
                "chunk_id": failed.chunk_id,
                "status": ChunkStatus.FAILED,
                "code": self._error_code(last_error, model_profile_id),
                "retryable": True,
            },
        )
        return []

    @staticmethod
    def _sanitize_error(error: Exception | None, model_profile_id: str) -> str:
        code = ChunkReviewService._error_code(error, model_profile_id)
        if code in {
            ChunkErrorCode.LOCAL_MODEL_CONNECTION_REFUSED,
            ChunkErrorCode.LOCAL_MODEL_TIMEOUT,
            ChunkErrorCode.LOCAL_MODEL_CIRCUIT_OPEN,
        }:
            return "本地模型服务未运行或正在恢复。"
        if error is None:
            return "模型审查失败。"
        return f"模型审查失败（{type(error).__name__}）。"

    @staticmethod
    def _error_code(error: Exception | None, model_profile_id: str) -> ChunkErrorCode:
        if isinstance(error, ContextWindowExceededError):
            return ChunkErrorCode.CONTEXT_OVERFLOW
        if isinstance(error, ReviewPlanningError) and error.code == "context_overflow":
            return ChunkErrorCode.CONTEXT_OVERFLOW
        cause = error
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if model_profile_id.startswith("local-"):
                if isinstance(cause, AllInstancesUnavailable):
                    return ChunkErrorCode.LOCAL_MODEL_CIRCUIT_OPEN
                if isinstance(cause, httpx.TimeoutException):
                    return ChunkErrorCode.LOCAL_MODEL_TIMEOUT
                if isinstance(cause, httpx.ConnectError):
                    return ChunkErrorCode.LOCAL_MODEL_CONNECTION_REFUSED
            cause = cause.__cause__ or cause.__context__
        return ChunkErrorCode.MODEL_ERROR

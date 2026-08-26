import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from code_review.application.structured_output import decode_review_response
from code_review.domain.model_ports import InstanceRegistryPort, ModelClient
from code_review.domain.model_protocol import (
    ChatMessage,
    ContextWindowExceededError,
    FixProposal,
    InferenceRequest,
    RawInferenceResult,
    ReviewResponse,
)

MAX_OUTPUT_TOKENS = 32768
STREAM_INITIAL_OUTPUT_TOKENS = 8192
OUTPUT_TOKEN_BUDGETS = (MAX_OUTPUT_TOKENS,)
CHINESE_REVIEW_SYSTEM_PROMPT = (
    "\u8bf7\u4f5c\u4e3a\u4ee3\u7801\u5ba1\u67e5\u52a9\u624b\u8f93\u51fa\u4e2d\u6587\u5ba1\u67e5\u7ed3\u679c\u3002"
    "\u53ea\u8fd4\u56de\u7b26\u5408 ReviewResponse schema "
    "\u7684 JSON \u5bf9\u8c61\uff0c"
    "\u4e0d\u8981\u4f7f\u7528 Markdown\u3002"
    "summary\u3001title\u3001evidence\u3001impact\u3001suggestion\u548c uncovered "
    "\u7b49\u81ea\u7136\u8bed\u8a00\u5b57\u6bb5\u5fc5\u987b\u4f7f\u7528\u4e2d\u6587\uff1b"
    "\u6587\u4ef6\u540d\u3001\u4ee3\u7801\u6807\u8bc6\u7b26"
    "\u548c\u89c4\u5219 ID \u53ef\u4fdd\u7559\u539f\u6587\u3002"
    "findings \u6700\u591a\u8f93\u51fa 50 \u9879\uff1b"
    "\u76f8\u540c\u6839\u56e0\u6216\u76f8\u540c\u4f4d\u7f6e\u7684\u95ee\u9898\u5fc5\u987b\u5408\u5e76\uff0c\u7981\u6b62\u91cd\u590d\u8f93\u51fa\uff1b"
    "\u5176\u4f59\u8303\u56f4\u5728 uncovered \u4e2d\u7b80\u8981\u6982\u62ec\u3002"
    "\u8bf7\u4e3a\u6bcf\u4e2a finding \u586b\u5199 impact_level\uff08critical/high/medium/low\uff09\u3001"  # noqa: E501
    "exploitability\uff08high/medium/low\uff09\u3001exposure\uff08internet/authenticated/internal/local/unknown\uff09\uff0c"
    "\u5e76\u586b\u5199 severity_reason\u3002severity \u4ec5\u4f5c\u4e3a\u5efa\u8bae\uff0c\u670d\u52a1\u7aef\u4f1a\u6309\u89c4\u5219\u91cd\u65b0\u8ba1\u7b97\u3002"  # noqa: E501
)
COMPRESSED_REVIEW_SYSTEM_PROMPT = (
    CHINESE_REVIEW_SYSTEM_PROMPT + "\u4e4b\u524d\u7684\u8f93\u51fa\u56e0\u4e3a"
    "\u8fc7\u957f\u88ab\u622a\u65ad\u3002"
    "\u8bf7\u538b\u7f29\u7ed3\u679c\uff1a"
    "\u4fdd\u7559\u9ad8\u98ce\u9669\u548c\u9ad8\u7f6e\u4fe1\u5ea6\u95ee\u9898\uff0c\u5408\u5e76\u91cd\u590d\u9879\uff0c"
    "\u7f29\u77ed evidence\u3001impact \u548c suggestion\uff0c"
    "\u5fc5\u8981\u65f6\u51cf\u5c11\u4f4e\u4f18\u5148\u7ea7 finding\uff0c"
    "\u4f46\u5fc5\u987b\u8f93\u51fa\u5b8c\u6574 JSON\u3002"
)


class ReviewOutputError(RuntimeError):
    """The model did not produce a complete, valid review response."""


FixFailureCode = Literal[
    "finding_not_actionable",
    "already_compliant",
    "no_effective_diff",
    "model_output_invalid",
    "scope_mismatch",
    "syntax_invalid",
    "stale_revision",
    "needs_intent",
    "speculative_finding",
    "ambiguous_symbol",
    "replacement_indent_invalid",
    "root_cause_unverified",
]


class FixCandidateError(ReviewOutputError):
    def __init__(
        self,
        code: FixFailureCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class ReviewStreamEvent:
    event: Literal["status", "delta", "reset", "final"]
    data: dict[str, object]


class InferenceService:
    def __init__(
        self,
        client: ModelClient,
        registry: InstanceRegistryPort,
    ) -> None:
        self._client = client
        self._registry = registry

    @property
    def registry(self) -> InstanceRegistryPort:
        return self._registry

    async def answer_followup(self, request: InferenceRequest) -> str:
        lease = await self._registry.acquire(
            self._estimate_tokens(request),
            self._estimate_prompt_tokens(request),
        )
        try:
            raw = await self._client.complete(lease.endpoint, request)
        except asyncio.CancelledError:
            await self._registry.release_neutral(lease)
            raise
        except ContextWindowExceededError:
            await self._registry.release_neutral(lease)
            raise
        except Exception:
            await self._registry.release_failure(lease)
            raise

        await self._registry.release_success(lease)
        answer = raw.content.strip()
        if self._is_truncated(raw) or not answer:
            raise ReviewOutputError("follow-up output was empty or truncated")
        return self._plain_text_answer(answer)

    async def propose_fix(self, request: InferenceRequest) -> FixProposal:
        lease = await self._registry.acquire(
            self._estimate_tokens(request),
            self._estimate_prompt_tokens(request),
        )
        try:
            raw = await self._client.complete(lease.endpoint, request)
        except asyncio.CancelledError:
            await self._registry.release_neutral(lease)
            raise
        except ContextWindowExceededError:
            await self._registry.release_neutral(lease)
            raise
        except Exception:
            await self._registry.release_failure(lease)
            raise
        await self._registry.release_success(lease)
        if self._is_truncated(raw) or not raw.content.strip():
            raise ReviewOutputError("fix proposal was empty or truncated")
        try:
            return FixProposal.model_validate_json(raw.content)
        except ValidationError as error:
            raise ReviewOutputError("fix proposal was not valid JSON") from error

    @staticmethod
    def _plain_text_answer(answer: str) -> str:
        fence = chr(96) * 3
        if answer.startswith(fence) and answer.endswith(fence):
            lines = answer.splitlines()
            if len(lines) >= 3:
                answer = "\n".join(lines[1:-1]).strip()
        try:
            parsed: object = json.loads(answer)
        except json.JSONDecodeError:
            return answer
        if isinstance(parsed, dict):
            for key in ("answer", "content", "message", "response", "summary"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return answer

    async def review(self, request: InferenceRequest) -> ReviewResponse:
        last_error: Exception | None = None
        incomplete_output_seen = False
        base_request = self._with_system_prompt(request, CHINESE_REVIEW_SYSTEM_PROMPT)

        for budget in OUTPUT_TOKEN_BUDGETS:
            attempt = base_request.model_copy(update={"max_output_tokens": budget})
            for _ in range(2):
                try:
                    result, truncated = await self._attempt_review(attempt)
                except ContextWindowExceededError:
                    raise
                except (ValueError, ValidationError) as error:
                    incomplete_output_seen = True
                    last_error = error
                    break
                except Exception as error:
                    last_error = error
                    continue
                if result is not None:
                    return result
                if truncated:
                    incomplete_output_seen = True
                    last_error = ReviewOutputError(f"model output was truncated at {budget} tokens")
                break

        if incomplete_output_seen:
            compressed = self._with_system_prompt(
                request,
                COMPRESSED_REVIEW_SYSTEM_PROMPT,
            ).model_copy(update={"max_output_tokens": MAX_OUTPUT_TOKENS})
            try:
                result, truncated = await self._attempt_review(compressed)
            except ContextWindowExceededError:
                raise
            except Exception as error:
                last_error = error
            else:
                if result is not None:
                    return result
                if truncated:
                    last_error = ReviewOutputError("compressed model output was still truncated")
            raise ReviewOutputError(
                "model output was truncated or invalid after bounded retries"
            ) from last_error

        raise RuntimeError("all model attempts failed") from last_error

    async def review_stream(
        self,
        request: InferenceRequest,
    ) -> AsyncIterator[ReviewStreamEvent]:
        yield ReviewStreamEvent(
            event="status",
            data={"stage": "generating"},
        )
        events: asyncio.Queue[ReviewStreamEvent] = asyncio.Queue()
        worker = asyncio.create_task(self._review_stream_result(request, events))
        try:
            while not worker.done() or not events.empty():
                try:
                    event = await asyncio.wait_for(events.get(), timeout=0.1)
                except TimeoutError:
                    continue
                yield event
            result = await worker
            yield ReviewStreamEvent(event="final", data=result.model_dump())
        finally:
            if not worker.done():
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    async def _review_stream_result(
        self,
        request: InferenceRequest,
        events: asyncio.Queue[ReviewStreamEvent],
    ) -> ReviewResponse:
        last_error: Exception | None = None
        incomplete_output_seen = False
        attempt = self._with_system_prompt(
            request,
            CHINESE_REVIEW_SYSTEM_PROMPT,
        ).model_copy(update={"max_output_tokens": STREAM_INITIAL_OUTPUT_TOKENS})

        for _ in range(2):
            try:
                result, truncated = await self._attempt_review_stream(attempt, events)
            except ContextWindowExceededError:
                raise
            except (ValueError, ValidationError) as error:
                incomplete_output_seen = True
                last_error = error
                break
            except Exception as error:
                last_error = error
                continue
            if result is not None:
                return result
            if truncated:
                incomplete_output_seen = True
                last_error = ReviewOutputError(
                    f"model output was truncated at {STREAM_INITIAL_OUTPUT_TOKENS} tokens"
                )
            break

        if incomplete_output_seen:
            await events.put(
                ReviewStreamEvent(
                    event="reset",
                    data={
                        "reason": "output_incomplete",
                        "message": "结果较长，正在精简后重新生成",
                    },
                )
            )
            compressed = self._with_system_prompt(
                request,
                COMPRESSED_REVIEW_SYSTEM_PROMPT,
            ).model_copy(update={"max_output_tokens": MAX_OUTPUT_TOKENS})
            try:
                result, truncated = await self._attempt_review_stream(
                    compressed,
                    events,
                )
            except ContextWindowExceededError:
                raise
            except Exception as error:
                last_error = error
            else:
                if result is not None:
                    return result
                if truncated:
                    last_error = ReviewOutputError("compressed model output was still truncated")
            raise ReviewOutputError(
                "model output was truncated or invalid after bounded retries"
            ) from last_error

        raise RuntimeError("all streaming model attempts failed") from last_error

    async def _attempt_review_stream(
        self,
        request: InferenceRequest,
        events: asyncio.Queue[ReviewStreamEvent],
    ) -> tuple[ReviewResponse | None, bool]:
        lease = await self._registry.acquire(
            self._estimate_tokens(request),
            self._estimate_prompt_tokens(request),
        )

        async def on_delta(content: str) -> None:
            await events.put(
                ReviewStreamEvent(
                    event="delta",
                    data={"content": content},
                )
            )

        try:
            raw = await self._client.complete_stream(
                lease.endpoint,
                request,
                on_delta,
            )
            if self._is_truncated(raw):
                await self._registry.release_success(lease)
                return None, True
            try:
                result = decode_review_response(raw.content)
            except (ValueError, ValidationError):
                await events.put(
                    ReviewStreamEvent(
                        event="reset",
                        data={
                            "reason": "invalid_json",
                            "message": "正在整理审查结果",
                        },
                    )
                )
                repair = self._repair_request(request, raw.content)
                repaired = await self._client.complete_stream(
                    lease.endpoint,
                    repair,
                    on_delta,
                )
                if self._is_truncated(repaired):
                    await self._registry.release_success(lease)
                    return None, True
                result = decode_review_response(repaired.content)
            await self._registry.release_success(lease)
            return result, False
        except asyncio.CancelledError:
            await self._registry.release_neutral(lease)
            raise
        except ContextWindowExceededError:
            await self._registry.release_neutral(lease)
            raise
        except Exception:
            await self._registry.release_failure(lease)
            raise

    async def _attempt_review(
        self,
        request: InferenceRequest,
    ) -> tuple[ReviewResponse | None, bool]:
        lease = await self._registry.acquire(
            self._estimate_tokens(request),
            self._estimate_prompt_tokens(request),
        )
        try:
            raw = await self._client.complete(lease.endpoint, request)
            if self._is_truncated(raw):
                await self._registry.release_success(lease)
                return None, True
            try:
                result = decode_review_response(raw.content)
            except (ValueError, ValidationError):
                repair = self._repair_request(request, raw.content)
                repaired = await self._client.complete(lease.endpoint, repair)
                if self._is_truncated(repaired):
                    await self._registry.release_success(lease)
                    return None, True
                result = decode_review_response(repaired.content)
            await self._registry.release_success(lease)
            return result, False
        except asyncio.CancelledError:
            await self._registry.release_neutral(lease)
            raise
        except ContextWindowExceededError:
            await self._registry.release_neutral(lease)
            raise
        except Exception:
            await self._registry.release_failure(lease)
            raise

    @staticmethod
    def _repair_request(request: InferenceRequest, invalid_content: str) -> InferenceRequest:
        return request.model_copy(
            update={
                "messages": [
                    *request.messages,
                    ChatMessage(role="assistant", content=invalid_content),
                    ChatMessage(
                        role="user",
                        content=(
                            "\u4e0a\u4e00\u6761\u8f93\u51fa\u4e0d\u662f"
                            "\u6709\u6548\u7684 ReviewResponse JSON\u3002"
                            "\u53ea\u8fd4\u56de\u4e00\u4e2a\u7b26\u5408"
                            "\u65e2\u5b9a Schema \u7684\u5b8c\u6574 JSON "
                            "\u5bf9\u8c61\uff0c"
                            "\u4e0d\u8981\u4f7f\u7528 Markdown\uff0c"
                            "\u6240\u6709\u81ea\u7136\u8bed\u8a00\u5b57\u6bb5"
                            "\u4f7f\u7528\u4e2d\u6587\u3002"
                        ),
                    ),
                ]
            }
        )

    @staticmethod
    def _with_system_prompt(request: InferenceRequest, prompt: str) -> InferenceRequest:
        existing_constraints = [
            message.content for message in request.messages if message.role == "system"
        ]
        combined_prompt = "\n\n".join([prompt, *existing_constraints])
        return request.model_copy(
            update={
                "messages": [
                    ChatMessage(role="system", content=combined_prompt),
                    *[message for message in request.messages if message.role != "system"],
                ]
            }
        )

    @classmethod
    def _is_truncated(cls, result: RawInferenceResult) -> bool:
        return result.finish_reason == "length" or cls._looks_like_truncated_json(result.content)

    @staticmethod
    def _looks_like_truncated_json(content: str) -> bool:
        stripped = content.strip()
        return stripped.startswith("{") and not stripped.endswith("}")

    @staticmethod
    def _estimate_tokens(request: InferenceRequest) -> int:
        character_count = sum(len(message.content) for message in request.messages)
        return int(max(1, character_count // 3) + request.max_output_tokens)

    @staticmethod
    def _estimate_prompt_tokens(request: InferenceRequest) -> int:
        character_count = sum(len(message.content) for message in request.messages)
        return max(1, character_count // 3)

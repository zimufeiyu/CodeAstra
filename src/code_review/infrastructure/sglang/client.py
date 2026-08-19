import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

import httpx

from code_review.domain.model_protocol import (
    ContextWindowExceededError,
    FixProposal,
    InferenceRequest,
    RawInferenceResult,
    ReviewResponse,
)

CONTEXT_SAFETY_TOKENS = 512
MIN_CONTEXT_OUTPUT_TOKENS = 128
_CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"maximum context length of\s+(\d+)\s+tokens.*?"
    r"(\d+)\s+tokens from the input messages",
    re.IGNORECASE | re.DOTALL,
)


class SGLangClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        model_name: str,
        timeout_seconds: int,
    ) -> None:
        self._http_client = http_client
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _response_format(request: InferenceRequest) -> dict[str, object] | None:
        if request.response_format == "text":
            return None
        schema_model = FixProposal if request.response_format == "fix" else ReviewResponse
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_model.__name__,
                "strict": True,
                "schema": schema_model.model_json_schema(),
            },
        }

    async def complete(
        self,
        endpoint: str,
        request: InferenceRequest,
    ) -> RawInferenceResult:
        payload = {
            "model": self._model_name,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "rid": self._new_inference_id(request.request_id),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response_format = self._response_format(request)
        if response_format is not None:
            payload["response_format"] = response_format
        started = perf_counter()
        response_format_retried = False
        context_retried = False
        try:
            while True:
                response = await self._http_client.post(
                    f"{endpoint.rstrip('/')}/v1/chat/completions",
                    json=payload,
                    timeout=self._timeout_seconds,
                )
                if (
                    response.status_code in {400, 422}
                    and "response_format" in response.text
                    and not response_format_retried
                ):
                    payload.pop("response_format", None)
                    response_format_retried = True
                    payload["rid"] = self._new_inference_id(request.request_id)
                    continue
                max_tokens = payload["max_tokens"]
                if not isinstance(max_tokens, int):
                    raise TypeError("max_tokens must be an integer")
                adjusted_budget = self._context_retry_budget(
                    response.text,
                    max_tokens,
                )
                if adjusted_budget is not None and not context_retried:
                    payload["max_tokens"] = adjusted_budget
                    payload["rid"] = self._new_inference_id(request.request_id)
                    context_retried = True
                    continue
                break
        except asyncio.CancelledError:
            await self._abort_request(endpoint, str(payload["rid"]))
            raise
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage", {})
        choice = body["choices"][0]
        return RawInferenceResult(
            request_id=request.request_id,
            instance_id=endpoint,
            content=choice["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
            latency_ms=int((perf_counter() - started) * 1000),
        )

    async def complete_stream(
        self,
        endpoint: str,
        request: InferenceRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> RawInferenceResult:
        payload = {
            "model": self._model_name,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "rid": self._new_inference_id(request.request_id),
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response_format = self._response_format(request)
        if response_format is not None:
            payload["response_format"] = response_format
        started = perf_counter()
        parts: list[str] = []
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        response_format_retried = False
        context_retried = False

        try:
            while True:
                async with self._http_client.stream(
                    "POST",
                    f"{endpoint.rstrip('/')}/v1/chat/completions",
                    json=payload,
                    timeout=self._timeout_seconds,
                ) as response:
                    if response.status_code in {400, 422}:
                        error_body = (await response.aread()).decode(errors="replace")
                        if (
                            "response_format" in error_body
                            and "response_format" in payload
                            and not response_format_retried
                        ):
                            payload.pop("response_format")
                            payload["rid"] = self._new_inference_id(request.request_id)
                            response_format_retried = True
                            continue
                        max_tokens = payload["max_tokens"]
                        if not isinstance(max_tokens, int):
                            raise TypeError("max_tokens must be an integer")
                        adjusted_budget = self._context_retry_budget(
                            error_body,
                            max_tokens,
                        )
                        if adjusted_budget is not None and not context_retried:
                            payload["max_tokens"] = adjusted_budget
                            payload["rid"] = self._new_inference_id(request.request_id)
                            context_retried = True
                            continue
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        chunk = json.loads(data)
                        usage = chunk.get("usage") or {}
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get(
                            "completion_tokens",
                            completion_tokens,
                        )
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            parts.append(delta)
                            await on_delta(delta)
                        finish_reason = choice.get("finish_reason") or finish_reason
                    break
        except asyncio.CancelledError:
            await self._abort_request(endpoint, str(payload["rid"]))
            raise

        return RawInferenceResult(
            request_id=request.request_id,
            instance_id=endpoint,
            content="".join(parts),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
            latency_ms=int((perf_counter() - started) * 1000),
        )

    @staticmethod
    def _new_inference_id(request_id: str) -> str:
        return f"{request_id}-{uuid4().hex}"

    @staticmethod
    def _context_retry_budget(error_body: str, current_budget: int) -> int | None:
        match = _CONTEXT_OVERFLOW_PATTERN.search(error_body)
        if match is None:
            return None
        context_limit = int(match.group(1))
        prompt_tokens = int(match.group(2))
        available = context_limit - prompt_tokens - CONTEXT_SAFETY_TOKENS
        if available < MIN_CONTEXT_OUTPUT_TOKENS:
            raise ContextWindowExceededError(context_limit, prompt_tokens)
        if available >= current_budget:
            return None
        return available

    async def _abort_request(self, endpoint: str, request_id: str) -> None:
        """Tell SGLang to stop the matching scheduler request immediately."""
        try:
            response = await self._http_client.post(
                f"{endpoint.rstrip('/')}/abort_request",
                json={"rid": request_id},
                timeout=min(self._timeout_seconds, 2),
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # The HTTP stream is already being cancelled; preserve that cancellation
            # even if an older model server does not expose /abort_request.
            return

    async def health(self, endpoint: str) -> bool:
        for path in ("/health", "/health_generate", "/v1/models"):
            try:
                response = await self._http_client.get(
                    f"{endpoint.rstrip('/')}{path}",
                    timeout=min(self._timeout_seconds, 5),
                )
            except httpx.HTTPError:
                return False
            if response.is_success:
                return True
            if response.status_code not in {404, 405}:
                return False
        return False

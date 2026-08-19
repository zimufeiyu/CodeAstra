from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import perf_counter

import httpx
from pydantic import SecretStr

from code_review.domain.model_protocol import (
    ContextWindowExceededError,
    InferenceRequest,
    RawInferenceResult,
)
from code_review.infrastructure.deepseek.context import (
    DeepSeekRuntimeContext,
    require_deepseek_context,
)


class DeepSeekAPIError(RuntimeError):
    """A sanitized DeepSeek API failure that is safe to surface to the service layer."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class DeepSeekClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        api_key: str | None = None,
        model_name: str | None = None,
        timeout_seconds: int,
        max_retries: int = 2,
        thinking_enabled: bool = False,
    ) -> None:
        self._http_client = http_client
        if (api_key is None) != (model_name is None):
            raise ValueError("api_key and model_name must be provided together")
        self._fallback_context = (
            DeepSeekRuntimeContext(api_key=SecretStr(api_key), model_name=model_name)
            if api_key is not None and model_name is not None
            else None
        )
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._thinking_enabled = thinking_enabled

    def _runtime_context(self) -> DeepSeekRuntimeContext:
        try:
            return require_deepseek_context()
        except LookupError:
            if self._fallback_context is None:
                raise
            return self._fallback_context

    @staticmethod
    def _response_format(request: InferenceRequest) -> dict[str, str] | None:
        if request.response_format == "text":
            return None
        return {"type": "json_object"}

    def _payload(self, request: InferenceRequest, *, stream: bool) -> dict[str, object]:
        model_name = self._runtime_context().model_name
        payload: dict[str, object] = {
            "model": model_name,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }
        response_format = self._response_format(request)
        if response_format is not None:
            payload["response_format"] = response_format
        if model_name.startswith("deepseek-v4"):
            payload["thinking"] = {"type": "enabled" if self._thinking_enabled else "disabled"}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": ("Bearer " + self._runtime_context().api_key.get_secret_value()),
            "Content-Type": "application/json",
        }

    @staticmethod
    def _usage_token(usage: object, key: str) -> int | None:
        if not isinstance(usage, dict):
            return None
        value = usage.get(key)
        return value if isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _url(endpoint: str) -> str:
        return f"{endpoint.rstrip('/')}/chat/completions"

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return float(min(10.0, max(0.0, float(retry_after))))
                except ValueError:
                    pass
        return float(min(8.0, 0.5 * (2**attempt)))

    @staticmethod
    def _retryable(response: httpx.Response) -> bool:
        return response.status_code == 429 or response.status_code >= 500

    @staticmethod
    def _raise_api_error(response: httpx.Response) -> None:
        status = response.status_code
        if status == 401:
            message = "DeepSeek API Key \u65e0\u6548\u6216\u5df2\u8fc7\u671f\u3002"
        elif status == 402:
            message = "DeepSeek API \u8d26\u6237\u4f59\u989d\u4e0d\u8db3\u3002"
        elif status == 429:
            message = (
                "DeepSeek API \u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c"
                "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"
            )
        elif status >= 500:
            message = "DeepSeek API \u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528\u3002"
        else:
            detail = response.text.lower()
            if "context" in detail and ("length" in detail or "token" in detail):
                raise ContextWindowExceededError(1, 1)
            message = f"DeepSeek API \u8bf7\u6c42\u5931\u8d25\uff08HTTP {status}\uff09\u3002"
        raise DeepSeekAPIError(message, status_code=status)

    async def _post(self, endpoint: str, payload: dict[str, object]) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http_client.post(
                    self._url(endpoint),
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    message = (
                        "\u8fde\u63a5 DeepSeek API \u8d85\u65f6\u6216\u7f51\u7edc"
                        "\u4e0d\u53ef\u7528\u3002"
                    )
                    raise DeepSeekAPIError(message) from error
                await asyncio.sleep(self._retry_delay(None, attempt))
                continue
            if self._retryable(response) and attempt < self._max_retries:
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue
            return response
        raise DeepSeekAPIError("DeepSeek API \u8bf7\u6c42\u672a\u5b8c\u6210\u3002")

    async def complete(
        self,
        endpoint: str,
        request: InferenceRequest,
    ) -> RawInferenceResult:
        started = perf_counter()
        response = await self._post(endpoint, self._payload(request, stream=False))
        if not response.is_success:
            self._raise_api_error(response)
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise DeepSeekAPIError(
                "DeepSeek API \u8fd4\u56de\u4e86\u65e0\u6cd5\u89e3\u6790\u7684\u54cd\u5e94\u3002"
            ) from error
        usage = body.get("usage") or {}
        content = message.get("content") or ""
        if not content.strip():
            raise DeepSeekAPIError("DeepSeek API returned an empty response.")
        return RawInferenceResult(
            request_id=request.request_id,
            instance_id=endpoint,
            content=content,
            prompt_tokens=self._usage_token(usage, "prompt_tokens"),
            completion_tokens=self._usage_token(usage, "completion_tokens"),
            finish_reason=choice.get("finish_reason"),
            latency_ms=int((perf_counter() - started) * 1000),
            provider="deepseek",
            model=str(body.get("model") or self._runtime_context().model_name),
            prompt_cache_hit_tokens=self._usage_token(usage, "prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=self._usage_token(usage, "prompt_cache_miss_tokens"),
        )

    async def complete_stream(
        self,
        endpoint: str,
        request: InferenceRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> RawInferenceResult:
        started = perf_counter()
        payload = self._payload(request, stream=True)
        parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, object] = {}
        response_model = self._runtime_context().model_name
        for attempt in range(self._max_retries + 1):
            try:
                async with self._http_client.stream(
                    "POST",
                    self._url(endpoint),
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout_seconds,
                ) as response:
                    if self._retryable(response) and attempt < self._max_retries:
                        await response.aread()
                        await asyncio.sleep(self._retry_delay(response, attempt))
                        continue
                    if not response.is_success:
                        await response.aread()
                        self._raise_api_error(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        chunk = json.loads(data)
                        response_model = str(chunk.get("model") or response_model)
                        usage.update(chunk.get("usage") or {})
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
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if parts or attempt >= self._max_retries:
                    raise DeepSeekAPIError(
                        "DeepSeek API \u6d41\u5f0f\u8fde\u63a5\u4e2d\u65ad\u3002"
                    ) from error
                await asyncio.sleep(self._retry_delay(None, attempt))
        return RawInferenceResult(
            request_id=request.request_id,
            instance_id=endpoint,
            content="".join(parts),
            prompt_tokens=self._usage_token(usage, "prompt_tokens"),
            completion_tokens=self._usage_token(usage, "completion_tokens"),
            finish_reason=finish_reason,
            latency_ms=int((perf_counter() - started) * 1000),
            provider="deepseek",
            model=response_model,
            prompt_cache_hit_tokens=self._usage_token(usage, "prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=self._usage_token(usage, "prompt_cache_miss_tokens"),
        )

    async def health(self, endpoint: str) -> bool:
        try:
            response = await self._http_client.get(
                f"{endpoint.rstrip('/')}/models",
                headers=self._headers(),
                timeout=min(self._timeout_seconds, 10),
            )
        except httpx.HTTPError:
            return False
        return response.is_success

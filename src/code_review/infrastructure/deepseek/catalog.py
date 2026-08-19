from __future__ import annotations

import asyncio

import httpx
from pydantic import SecretStr


class DeepSeekCatalogError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class DeepSeekModelCatalog:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        base_url: str,
        timeout_seconds: int,
        *,
        max_retries: int = 2,
    ) -> None:
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = min(timeout_seconds, 10)
        self._max_retries = max_retries

    @staticmethod
    def _error_for_status(status: int) -> DeepSeekCatalogError:
        if status == 401:
            message = "DeepSeek API Key 无效或已过期。"
        elif status == 402:
            message = "DeepSeek API 账户余额不足。"
        elif status == 403:
            message = "当前 DeepSeek 账号没有访问权限。"
        elif status == 429:
            message = "DeepSeek API 请求过于频繁，请稍后重试。"
        elif status >= 500:
            message = "DeepSeek API 服务暂时不可用。"
        else:
            message = f"DeepSeek 模型列表请求失败（HTTP {status}）。"
        return DeepSeekCatalogError(message, status_code=status)

    async def list_models(self, api_key: SecretStr) -> list[str]:
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http_client.get(
                    f"{self._base_url}/models",
                    headers={"Authorization": ("Bearer " + api_key.get_secret_value())},
                    timeout=self._timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise DeepSeekCatalogError("连接 DeepSeek API 超时或网络不可用。") from error
                await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
                continue
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < self._max_retries:
                await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
                continue
            break

        if response is None:
            raise DeepSeekCatalogError("DeepSeek 模型列表请求未完成。")
        if not response.is_success:
            raise self._error_for_status(response.status_code)
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError) as error:
            raise DeepSeekCatalogError("DeepSeek API 没有返回可用模型。") from error
        if not isinstance(data, list):
            raise DeepSeekCatalogError("DeepSeek API 没有返回可用模型。")
        models = sorted(
            {
                item["id"].strip()
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
            }
        )
        if not models:
            raise DeepSeekCatalogError("DeepSeek API 没有返回可用模型。")
        return models

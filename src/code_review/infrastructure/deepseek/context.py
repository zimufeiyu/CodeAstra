from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True)
class DeepSeekRuntimeContext:
    api_key: SecretStr
    model_name: str


_current_deepseek_context: ContextVar[DeepSeekRuntimeContext | None] = ContextVar(
    "current_deepseek_context",
    default=None,
)


@contextmanager
def bind_deepseek_context(api_key: str, model_name: str) -> Iterator[None]:
    normalized_key = api_key.strip()
    normalized_model = model_name.strip()
    if not normalized_key:
        raise ValueError("DeepSeek API Key 不能为空")
    if not normalized_model:
        raise ValueError("DeepSeek 模型不能为空")
    token = _current_deepseek_context.set(
        DeepSeekRuntimeContext(
            api_key=SecretStr(normalized_key),
            model_name=normalized_model,
        )
    )
    try:
        yield
    finally:
        _current_deepseek_context.reset(token)


def require_deepseek_context() -> DeepSeekRuntimeContext:
    current = _current_deepseek_context.get()
    if current is None:
        raise LookupError("DeepSeek 请求上下文不存在")
    return current

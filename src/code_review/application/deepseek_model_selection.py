from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from code_review.domain.review_models import ReviewMode

DeepSeekSelectionMode = Literal["auto", "manual"]


@dataclass(frozen=True)
class ReviewShape:
    mode: ReviewMode
    file_count: int
    total_lines: int
    estimated_input_tokens: int

    @property
    def complex(self) -> bool:
        return (
            self.mode == "project"
            or self.file_count > 1
            or self.total_lines >= 500
            or self.estimated_input_tokens >= 12_000
        )


def select_deepseek_model(
    selection_mode: DeepSeekSelectionMode,
    manual_model: str | None,
    available_models: Sequence[str],
    shape: ReviewShape,
) -> str:
    normalized = sorted({item.strip() for item in available_models if item.strip()})
    if not normalized:
        raise ValueError("当前 DeepSeek 账号没有可用模型")
    if selection_mode == "manual":
        if manual_model not in normalized:
            raise ValueError("所选 DeepSeek 模型当前账号不可用")
        return manual_model

    preferred = ("pro", "flash") if shape.complex else ("flash", "pro")
    for marker in preferred:
        match = next((item for item in normalized if marker in item.lower()), None)
        if match is not None:
            return match
    return normalized[0]

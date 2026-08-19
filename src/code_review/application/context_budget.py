from __future__ import annotations

import math
from dataclasses import dataclass

from code_review.domain.review_chunks import ContextReference, ReviewChunk


class ConservativeTokenEstimator:
    def estimate_text(self, content: str) -> int:
        return max(1, math.ceil(len(content.encode("utf-8")) / 2))


@dataclass(frozen=True)
class BudgetDecision:
    action: str
    target_code: str
    references: list[ContextReference]
    prompt_tokens: int
    output_tokens: int
    total_tokens: int


class ContextBudgeter:
    def __init__(
        self,
        *,
        context_tokens: int = 40960,
        safety_tokens: int = 1024,
        minimum_output_tokens: int = 512,
        maximum_output_tokens: int = 32768,
        fixed_prompt_tokens: int = 2048,
        estimator: ConservativeTokenEstimator | None = None,
    ) -> None:
        self.context_tokens = context_tokens
        self.safety_tokens = safety_tokens
        self.minimum_output_tokens = minimum_output_tokens
        self.maximum_output_tokens = maximum_output_tokens
        self.fixed_prompt_tokens = fixed_prompt_tokens
        self.estimator = estimator or ConservativeTokenEstimator()

    def fit(self, chunk: ReviewChunk) -> BudgetDecision:
        available = self.context_tokens - self.safety_tokens
        target_tokens = self.estimator.estimate_text(chunk.target_code) + 32
        base = self.fixed_prompt_tokens + target_tokens
        if base + self.minimum_output_tokens > available:
            return BudgetDecision(
                action="split_target",
                target_code=chunk.target_code,
                references=[],
                prompt_tokens=base,
                output_tokens=0,
                total_tokens=base,
            )

        references = sorted(
            chunk.context_references,
            key=lambda item: (item.rank, item.path, item.start_line),
        )
        original_count = len(references)

        def reference_tokens(reference: ContextReference) -> int:
            metadata = f"{reference.path}:{reference.start_line}-{reference.end_line}"
            return self.estimator.estimate_text(metadata + reference.code) + 32

        prompt = base + sum(reference_tokens(item) for item in references)
        while references and prompt + self.minimum_output_tokens > available:
            removed = references.pop()
            prompt -= reference_tokens(removed)
        output = min(self.maximum_output_tokens, available - prompt)
        return BudgetDecision(
            action="trim_context" if len(references) < original_count else "fits",
            target_code=chunk.target_code,
            references=references,
            prompt_tokens=prompt,
            output_tokens=output,
            total_tokens=prompt + output,
        )

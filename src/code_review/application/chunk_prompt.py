from __future__ import annotations

import json

from code_review.application.context_budget import ContextBudgeter
from code_review.domain.model_protocol import ChatMessage, InferenceRequest, ReviewResponse
from code_review.domain.review_chunks import ReviewChunk, ReviewPlanningError

SYSTEM_PROMPT = """你是严谨的代码审查助手。只输出符合给定 JSON Schema 的 JSON，不要输出 Markdown。
所有自然语言内容必须为中文。只审查 TARGET_CODE，CONTEXT_REFERENCES 仅用于理解依赖关系。
不得报告目标范围之外的问题，包括其他文件或其他行号；证据必须逐字存在于目标代码中。
风险等级必须结合影响、可利用性和暴露面，不能仅按关键词判断。"""


class ChunkPromptBuilder:
    def __init__(self, budgeter: ContextBudgeter) -> None:
        self._budgeter = budgeter

    def build(self, chunk: ReviewChunk, attempt_id: str) -> InferenceRequest:
        decision = self._budgeter.fit(chunk)
        if decision.action == "split_target":
            raise ReviewPlanningError(
                code="context_overflow",
                message="目标代码超过上下文容量，需要继续拆分。",
                chunk_ids=[chunk.chunk_id],
            )
        references = "\n\n".join(
            f"[{item.path}:{item.start_line}-{item.end_line}]\n{item.code}"
            for item in decision.references
        )
        schema = json.dumps(
            ReviewResponse.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        user = (
            f"TARGET_CODE [{chunk.target_path}:{chunk.target_start_line}-"
            f"{chunk.target_end_line}]\n{decision.target_code}\n\n"
            f"CONTEXT_REFERENCES\n{references or '无'}\n\n"
            f"RESPONSE_SCHEMA\n{schema}"
        )
        return InferenceRequest(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=user),
            ],
            max_output_tokens=decision.output_tokens,
            temperature=0.1,
            request_id=f"{chunk.review_id}:{chunk.chunk_id}:{attempt_id}",
        )

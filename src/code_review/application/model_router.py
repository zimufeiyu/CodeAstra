from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Protocol

from code_review.application.inference_service import ReviewStreamEvent
from code_review.domain.model_protocol import FixProposal, InferenceRequest, ReviewResponse


class InferenceServiceAdapter(Protocol):
    async def review(self, request: InferenceRequest) -> ReviewResponse: ...
    def review_stream(
        self,
        request: InferenceRequest,
    ) -> AsyncIterator[ReviewStreamEvent]: ...
    async def propose_fix(self, request: InferenceRequest) -> FixProposal: ...
    async def answer_followup(self, request: InferenceRequest) -> str: ...


class ModelProfileUnavailable(ValueError):
    pass


class RoutedInferenceService:
    """Route every model operation through the profile pinned to the review session."""

    def __init__(
        self,
        services: Mapping[str, InferenceServiceAdapter],
        *,
        default_profile_id: str = "local-qwen3-8b",
    ) -> None:
        if default_profile_id not in services:
            raise ValueError("default model profile must have an inference service")
        self._services = dict(services)
        self._default_profile_id = default_profile_id

    @property
    def available_profile_ids(self) -> frozenset[str]:
        return frozenset(self._services)

    def _for(self, request: InferenceRequest) -> InferenceServiceAdapter:
        profile_id = request.model_profile_id or self._default_profile_id
        service = self._services.get(profile_id)
        if service is None:
            raise ModelProfileUnavailable(
                f"\u6a21\u578b\u914d\u7f6e {profile_id} \u5f53\u524d\u4e0d\u53ef\u7528"
            )
        return service

    async def review(self, request: InferenceRequest) -> ReviewResponse:
        return await self._for(request).review(request)

    async def review_stream(
        self,
        request: InferenceRequest,
    ) -> AsyncIterator[ReviewStreamEvent]:
        async for event in self._for(request).review_stream(request):
            yield event

    async def propose_fix(self, request: InferenceRequest) -> FixProposal:
        return await self._for(request).propose_fix(request)

    async def answer_followup(self, request: InferenceRequest) -> str:
        return await self._for(request).answer_followup(request)

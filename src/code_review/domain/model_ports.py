from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from code_review.domain.model_protocol import InferenceRequest, RawInferenceResult


class ModelClient(Protocol):
    async def complete(
        self,
        endpoint: str,
        request: InferenceRequest,
    ) -> RawInferenceResult: ...

    async def complete_stream(
        self,
        endpoint: str,
        request: InferenceRequest,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> RawInferenceResult: ...

    async def health(self, endpoint: str) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class InstanceLease:
    lease_id: str
    endpoint: str
    estimated_tokens: int


@dataclass(frozen=True)
class InstanceSnapshot:
    endpoint: str
    inflight_requests: int
    inflight_tokens: int
    circuit_open: bool


class InstanceRegistryPort(Protocol):
    async def acquire(
        self,
        estimated_tokens: int,
        prompt_tokens: int = 0,
    ) -> InstanceLease: ...

    async def release_success(self, lease: InstanceLease) -> None: ...

    async def release_failure(self, lease: InstanceLease) -> None: ...

    async def release_neutral(self, lease: InstanceLease) -> None: ...

    def snapshot(self) -> list[InstanceSnapshot]: ...

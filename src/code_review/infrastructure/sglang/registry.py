import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from code_review.domain.model_ports import Clock, InstanceLease, InstanceSnapshot
from code_review.infrastructure.sglang.capacity import CapacityProfile, EndpointCapacity


class AllInstancesUnavailable(RuntimeError):
    pass


@dataclass
class AsyncInstanceState:
    endpoint: str
    inflight_requests: int = 0
    inflight_prompt_tokens: int = 0
    inflight_total_tokens: int = 0
    consecutive_failures: int = 0
    circuit_opened_at: datetime | None = None


class AsyncInstanceRegistry:
    def __init__(
        self,
        endpoints: list[str],
        failure_threshold: int,
        cooldown_seconds: int,
        clock: Clock,
        capacity_profile: CapacityProfile | None = None,
    ) -> None:
        self._states = {endpoint: AsyncInstanceState(endpoint) for endpoint in endpoints}
        self._failure_threshold = failure_threshold
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._clock = clock
        self._condition = asyncio.Condition()
        self._leases: dict[str, tuple[InstanceLease, int]] = {}
        self._capacity = capacity_profile or CapacityProfile(
            {
                endpoint: EndpointCapacity(
                    max_concurrency=64,
                    max_inflight_prompt_tokens=1_000_000_000,
                    max_inflight_total_tokens=1_000_000_000,
                )
                for endpoint in endpoints
            }
        )

    async def acquire(
        self,
        estimated_tokens: int,
        prompt_tokens: int = 0,
    ) -> InstanceLease:
        async with self._condition:
            while True:
                candidates = [state for state in self._states.values() if self._available(state)]
                if not candidates:
                    raise AllInstancesUnavailable("all SGLang instances are unavailable")
                eligible = [
                    state
                    for state in candidates
                    if self._has_capacity(state, prompt_tokens, estimated_tokens)
                ]
                if eligible:
                    selected = min(
                        eligible,
                        key=lambda state: (
                            state.inflight_total_tokens,
                            state.inflight_requests,
                            state.endpoint,
                        ),
                    )
                    selected.inflight_requests += 1
                    selected.inflight_prompt_tokens += prompt_tokens
                    selected.inflight_total_tokens += estimated_tokens
                    lease = InstanceLease(
                        lease_id=uuid4().hex,
                        endpoint=selected.endpoint,
                        estimated_tokens=estimated_tokens,
                    )
                    self._leases[lease.lease_id] = (lease, prompt_tokens)
                    return lease
                await self._condition.wait()

    async def release_success(self, lease: InstanceLease) -> None:
        async with self._condition:
            state = self._states[lease.endpoint]
            self._release_reservation(state, lease)
            state.consecutive_failures = 0
            state.circuit_opened_at = None
            self._condition.notify_all()

    async def release_failure(self, lease: InstanceLease) -> None:
        async with self._condition:
            state = self._states[lease.endpoint]
            self._release_reservation(state, lease)
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._failure_threshold:
                state.circuit_opened_at = self._clock.now()
            self._condition.notify_all()

    async def release_neutral(self, lease: InstanceLease) -> None:
        async with self._condition:
            state = self._states[lease.endpoint]
            self._release_reservation(state, lease)
            self._condition.notify_all()

    def snapshot(self) -> list[InstanceSnapshot]:
        return [
            InstanceSnapshot(
                endpoint=state.endpoint,
                inflight_requests=state.inflight_requests,
                inflight_tokens=state.inflight_total_tokens,
                circuit_open=state.circuit_opened_at is not None,
            )
            for state in self._states.values()
        ]

    def _available(self, state: AsyncInstanceState) -> bool:
        if state.circuit_opened_at is None:
            return True
        if self._clock.now() - state.circuit_opened_at > self._cooldown:
            state.circuit_opened_at = None
            state.consecutive_failures = 0
            return True
        return False

    def _has_capacity(
        self,
        state: AsyncInstanceState,
        prompt_tokens: int,
        total_tokens: int,
    ) -> bool:
        capacity = self._capacity.for_endpoint(state.endpoint)
        return (
            state.inflight_requests < capacity.max_concurrency
            and state.inflight_prompt_tokens + prompt_tokens <= capacity.max_inflight_prompt_tokens
            and state.inflight_total_tokens + total_tokens <= capacity.max_inflight_total_tokens
        )

    def _release_reservation(
        self,
        state: AsyncInstanceState,
        lease: InstanceLease,
    ) -> None:
        stored = self._leases.pop(lease.lease_id, None)
        if stored is None or stored[0].endpoint != lease.endpoint:
            raise ValueError(f"unknown instance lease: {lease.lease_id}")
        stored_lease, prompt_tokens = stored
        state.inflight_requests = max(0, state.inflight_requests - 1)
        state.inflight_prompt_tokens = max(0, state.inflight_prompt_tokens - prompt_tokens)
        state.inflight_total_tokens = max(
            0, state.inflight_total_tokens - stored_lease.estimated_tokens
        )


InstanceRegistry = AsyncInstanceRegistry

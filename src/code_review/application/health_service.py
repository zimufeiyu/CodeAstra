from dataclasses import dataclass

from code_review.domain.model_ports import InstanceRegistryPort


@dataclass(frozen=True)
class PublicInstanceHealth:
    endpoint_id: str
    inflight_requests: int
    inflight_tokens: int
    circuit_open: bool


class GatewayHealthService:
    def __init__(self, registry: InstanceRegistryPort) -> None:
        self._registry = registry

    def snapshot(self) -> list[PublicInstanceHealth]:
        return [
            PublicInstanceHealth(
                endpoint_id=f"instance-{index}",
                inflight_requests=state.inflight_requests,
                inflight_tokens=state.inflight_tokens,
                circuit_open=state.circuit_open,
            )
            for index, state in enumerate(self._registry.snapshot())
        ]

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from code_review.domain.model_ports import InstanceRegistryPort, InstanceSnapshot


@dataclass(frozen=True)
class PublicInstanceHealth:
    endpoint_id: str
    inflight_requests: int
    inflight_tokens: int
    circuit_open: bool
    available: bool
    reason_code: str | None = None


class GatewayHealthService:
    def __init__(
        self,
        registries: Mapping[str, InstanceRegistryPort],
        http_client: httpx.AsyncClient,
        probe_timeout_seconds: float = 0.75,
    ) -> None:
        self._registries = dict(registries)
        self._http_client = http_client
        self._probe_timeout_seconds = probe_timeout_seconds

    async def snapshot(self) -> list[PublicInstanceHealth]:
        pending = []
        for profile_id, registry in sorted(self._registries.items()):
            safe_profile = re.sub(r"[^a-z0-9-]+", "-", profile_id.casefold()).strip("-")
            for index, state in enumerate(registry.snapshot()):
                pending.append(
                    self._snapshot_state(profile_id, safe_profile, index, state)
                )
        return list(await asyncio.gather(*pending))

    async def _snapshot_state(
        self,
        profile_id: str,
        safe_profile: str,
        index: int,
        state: InstanceSnapshot,
    ) -> PublicInstanceHealth:
        available = not state.circuit_open
        reason_code = "circuit_open" if state.circuit_open else None
        if profile_id.startswith("local-"):
            try:
                response = await self._http_client.get(
                    f"{state.endpoint.rstrip('/')}/health",
                    timeout=self._probe_timeout_seconds,
                )
                response.raise_for_status()
            except httpx.ConnectError:
                available = False
                reason_code = "connection_refused"
            except httpx.TimeoutException:
                available = False
                reason_code = "timeout"
            except httpx.HTTPStatusError:
                available = False
                reason_code = "health_check_failed"
            except httpx.NetworkError:
                available = False
                reason_code = "unreachable"
        return PublicInstanceHealth(
            endpoint_id=f"{safe_profile}-{index}",
            inflight_requests=state.inflight_requests,
            inflight_tokens=state.inflight_tokens,
            circuit_open=state.circuit_open,
            available=available,
            reason_code=reason_code,
        )

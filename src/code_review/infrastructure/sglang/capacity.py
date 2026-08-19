from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EndpointCapacity:
    max_concurrency: int
    max_inflight_prompt_tokens: int
    max_inflight_total_tokens: int


class CapacityProfile:
    def __init__(self, capacities: dict[str, EndpointCapacity]) -> None:
        self._capacities = capacities

    @classmethod
    def load(
        cls,
        path: Path,
        endpoints: list[str],
        *,
        fallback_concurrency: int = 1,
        fallback_prompt_tokens: int = 40960,
        fallback_total_tokens: int = 81920,
    ) -> CapacityProfile:
        payload: dict[str, object] = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            LOGGER.warning("容量配置不存在，使用保守回退值：%s", path)
        raw_endpoints = payload.get("endpoints", {})
        if not isinstance(raw_endpoints, dict):
            raw_endpoints = {}
        capacities: dict[str, EndpointCapacity] = {}
        for endpoint in endpoints:
            raw = raw_endpoints.get(endpoint, {})
            if not isinstance(raw, dict):
                raw = {}
            capacities[endpoint] = EndpointCapacity(
                max_concurrency=max(1, int(raw.get("max_concurrency", fallback_concurrency))),
                max_inflight_prompt_tokens=max(
                    1,
                    int(
                        raw.get(
                            "max_inflight_prompt_tokens",
                            fallback_prompt_tokens,
                        )
                    ),
                ),
                max_inflight_total_tokens=max(
                    1,
                    int(raw.get("max_inflight_total_tokens", fallback_total_tokens)),
                ),
            )
        return cls(capacities)

    def for_endpoint(self, endpoint: str) -> EndpointCapacity:
        return self._capacities[endpoint]

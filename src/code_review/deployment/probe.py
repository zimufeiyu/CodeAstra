from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path

import httpx

from code_review.deployment.models import (
    CapabilityCheck,
    CapabilityReport,
    CapabilityStatus,
    DeploymentMode,
)


class ServerCapabilityProbe:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        python_executable: Path,
        sdk_path: Path,
        device_paths: list[Path],
        model_path: Path | None,
        endpoints: list[str],
        state_dir: Path,
        expected_model_name: str,
    ) -> None:
        self._http_client = http_client
        self._python_executable = python_executable
        self._sdk_path = sdk_path
        self._device_paths = device_paths
        self._model_path = model_path
        self._endpoints = endpoints
        self._state_dir = state_dir
        self._expected_model_name = expected_model_name

    async def _healthy_endpoints(self) -> list[str]:
        async def healthy(endpoint: str) -> str | None:
            normalized = endpoint.rstrip("/")
            try:
                health = await self._http_client.get(f"{normalized}/health", timeout=2)
                if not health.is_success:
                    return None
                models = await self._http_client.get(f"{normalized}/v1/models", timeout=2)
                if not models.is_success:
                    return None
                data = models.json().get("data")
            except (httpx.HTTPError, AttributeError, ValueError):
                return None
            if not isinstance(data, list):
                return None
            served_models = {item.get("id") for item in data if isinstance(item, dict)}
            return normalized if self._expected_model_name in served_models else None

        results = await asyncio.gather(*(healthy(item) for item in self._endpoints))
        return [item for item in results if item is not None]

    @staticmethod
    def _writable_parent(path: Path) -> bool:
        candidate = path.expanduser()
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate.is_dir() and os.access(candidate, os.W_OK)

    async def probe(self) -> CapabilityReport:
        system = platform.system()
        healthy = await self._healthy_endpoints()
        python_ok = self._python_executable.is_file()
        sdk_ok = self._sdk_path.is_dir()
        device_ok = any(path.exists() for path in self._device_paths)
        model_ok = self._model_path is not None and self._model_path.is_dir()
        state_ok = self._writable_parent(self._state_dir)
        linux_ok = system == "Linux"
        checks = [
            CapabilityCheck(name="supported_platform", ok=linux_ok, detail=system),
            CapabilityCheck(
                name="python_runtime", ok=python_ok, detail=str(self._python_executable)
            ),
            CapabilityCheck(name="ppu_sdk", ok=sdk_ok, detail=str(self._sdk_path)),
            CapabilityCheck(
                name="ppu_device",
                ok=device_ok,
                detail=", ".join(str(path) for path in self._device_paths),
            ),
            CapabilityCheck(
                name="model_directory",
                ok=model_ok,
                detail=str(self._model_path) if self._model_path else "not configured",
            ),
            CapabilityCheck(name="state_directory", ok=state_ok, detail=str(self._state_dir)),
        ]
        warnings: list[str] = []
        if healthy:
            status = CapabilityStatus.ALREADY_RUNNING
        elif not linux_ok:
            status = CapabilityStatus.UNSUPPORTED
            warnings.append(
                "Managed local Qwen deployment is supported on Linux; use API mode here."
            )
        elif not state_ok:
            status = CapabilityStatus.READY_WITH_WARNINGS
            warnings.append("The configured state directory is not writable.")
        elif not python_ok or not sdk_ok:
            status = CapabilityStatus.MISSING_RUNTIME
        elif not device_ok:
            status = CapabilityStatus.MISSING_DEVICE
        elif not model_ok:
            status = CapabilityStatus.MISSING_MODEL
        else:
            status = CapabilityStatus.READY
        can_manage = linux_ok and python_ok and sdk_ok and device_ok and model_ok and state_ok
        return CapabilityReport(
            status=status,
            platform=f"{system} {platform.release()} ({platform.machine()})",
            checks=checks,
            detected_endpoints=healthy,
            recommended_mode=(
                DeploymentMode.PPU_LOCAL if healthy or can_manage else DeploymentMode.DEEPSEEK_ONLY
            ),
            can_manage_local_model=can_manage,
            warnings=warnings,
        )

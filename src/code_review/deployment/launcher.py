from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import asdict
from time import monotonic
from urllib.parse import urlparse

import httpx

from code_review.config.settings import GatewaySettings
from code_review.deployment.models import DeploymentPlan
from code_review.deployment.sglang_process import (
    SGLangInstanceConfig,
    SGLangLaunchConfig,
    build_instance_command,
    validate_instances,
)


class LocalModelLauncher:
    def __init__(self, settings: GatewaySettings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client

    @staticmethod
    def _instance(endpoint: str, device_id: str, index: int) -> SGLangInstanceConfig:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("managed PPU endpoints must use local HTTP addresses")
        if parsed.port is None or parsed.path not in {"", "/"}:
            raise ValueError("managed PPU endpoint must contain only a host and port")
        return SGLangInstanceConfig(f"instance-{index}", device_id, parsed.port)

    @staticmethod
    def _port_available(host: str, port: int) -> bool:
        with socket.socket() as listener:
            try:
                listener.bind((host, port))
            except OSError:
                return False
        return True

    async def _wait_ready(
        self,
        endpoints: list[str],
        processes: list[asyncio.subprocess.Process],
        timeout_seconds: int,
    ) -> None:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if any(process.returncode is not None for process in processes):
                raise RuntimeError("a model process exited before readiness")
            results = await asyncio.gather(
                *(
                    self._http_client.get(f"{endpoint.rstrip('/')}/health", timeout=2)
                    for endpoint in endpoints
                ),
                return_exceptions=True,
            )
            if all(isinstance(result, httpx.Response) and result.is_success for result in results):
                return
            await asyncio.sleep(1)
        raise TimeoutError("model readiness deadline exceeded")

    @staticmethod
    async def _terminate(processes: list[asyncio.subprocess.Process]) -> None:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        for process in processes:
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def launch(self, plan: DeploymentPlan, *, timeout_seconds: int = 300) -> None:
        if os.name != "posix":
            raise RuntimeError("managed local Qwen deployment is supported only on Linux")
        if plan.model_path is None:
            raise ValueError("local deployment requires a model path")
        if len(plan.endpoints) != len(plan.device_ids):
            raise ValueError("each local endpoint must have one device ID")
        instances = [
            self._instance(endpoint, device_id, index)
            for index, (endpoint, device_id) in enumerate(
                zip(plan.endpoints, plan.device_ids, strict=True)
            )
        ]
        validate_instances(instances)
        occupied = [
            item.port for item in instances if not self._port_available("127.0.0.1", item.port)
        ]
        if occupied:
            raise RuntimeError(f"ports are already occupied: {occupied}")
        base = SGLangLaunchConfig(
            python_executable=str(self._settings.ppu_python_executable),
            launch_module="sglang.launch_server",
            model_path=plan.model_path,
            served_model_name=self._settings.model_name,
            host="127.0.0.1",
            device_environment_variable=self._settings.ppu_device_environment_variable,
            runtime_environment=(
                ("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")),
                ("CUDA_PATH", str(self._settings.ppu_cuda_path)),
                ("PPU_SDK", str(self._settings.ppu_sdk_path)),
            ),
            extra_args=(
                "--dtype",
                "bfloat16",
                "--context-length",
                str(self._settings.model_context_tokens),
                "--reasoning-parser",
                "qwen3",
            ),
        )
        runtime_dir = self._settings.ppu_runtime_dir
        runtime_dir.mkdir(parents=True, exist_ok=True)
        processes: list[asyncio.subprocess.Process] = []
        logs = []
        try:
            for item in instances:
                argv, overlay = build_instance_command(base, item)
                log = (runtime_dir / f"{item.instance_id}.log").open("ab")
                logs.append(log)
                processes.append(
                    await asyncio.create_subprocess_exec(
                        *argv,
                        env={**os.environ, **overlay},
                        stdout=log,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                )
            await self._wait_ready(plan.endpoints, processes, timeout_seconds)
            state = {
                "model_path": str(plan.model_path),
                "instances": [
                    {**asdict(item), "pid": process.pid}
                    for item, process in zip(instances, processes, strict=True)
                ],
            }
            temporary = runtime_dir / ".instances.json.tmp"
            temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            temporary.replace(runtime_dir / "instances.json")
        except BaseException:
            await self._terminate(processes)
            raise
        finally:
            for log in logs:
                log.close()

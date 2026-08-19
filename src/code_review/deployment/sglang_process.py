from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_RESERVED_ARGS = {
    "--host",
    "--model-path",
    "--port",
    "--served-model-name",
    "--tensor-parallel-size",
}


@dataclass(frozen=True)
class SGLangInstanceConfig:
    instance_id: str
    device_id: str
    port: int


@dataclass(frozen=True)
class SGLangLaunchConfig:
    python_executable: str
    launch_module: str
    model_path: Path
    served_model_name: str
    host: str
    device_environment_variable: str
    runtime_environment: tuple[tuple[str, str], ...] = ()
    extra_args: tuple[str, ...] = ()


def default_ppu_launch_config() -> SGLangLaunchConfig:
    """Return the launch settings verified on the local two-card PPU host."""
    return SGLangLaunchConfig(
        python_executable="/usr/local/bin/python3",
        launch_module="sglang.launch_server",
        model_path=Path("/LLM/qwen3"),
        served_model_name="Qwen3-8B",
        host="127.0.0.1",
        device_environment_variable="CUDA_VISIBLE_DEVICES",
        runtime_environment=(
            ("PATH", "/usr/local/bin:/usr/bin:/bin"),
            ("CUDA_PATH", "/usr/local/cuda"),
            ("PPU_SDK", "/usr/local/PPU_SDK"),
        ),
        extra_args=(
            "--dtype",
            "bfloat16",
            "--context-length",
            "40960",
            "--reasoning-parser",
            "qwen3",
        ),
    )


def validate_instances(instances: Sequence[SGLangInstanceConfig]) -> None:
    if not instances:
        raise ValueError("at least one SGLang instance is required")
    if len({item.instance_id for item in instances}) != len(instances):
        raise ValueError("instance IDs must be unique")
    if len({item.device_id for item in instances}) != len(instances):
        raise ValueError("device IDs must be unique")
    if len({item.port for item in instances}) != len(instances):
        raise ValueError("instance ports must be unique")
    if any(item.port < 1 or item.port > 65535 for item in instances):
        raise ValueError("instance port is outside the valid range")


def build_instance_command(
    base: SGLangLaunchConfig,
    instance: SGLangInstanceConfig,
) -> tuple[list[str], dict[str, str]]:
    if not base.device_environment_variable.strip():
        raise ValueError("device environment variable must not be empty")
    conflicting = _RESERVED_ARGS.intersection(base.extra_args)
    if conflicting:
        names = ", ".join(sorted(conflicting))
        raise ValueError(f"extra arguments contain reserved options: {names}")
    argv = [
        base.python_executable,
        "-m",
        base.launch_module,
        "--model-path",
        base.model_path.as_posix(),
        "--served-model-name",
        base.served_model_name,
        "--host",
        base.host,
        "--port",
        str(instance.port),
        "--tensor-parallel-size",
        "1",
        *base.extra_args,
    ]
    environment = dict(base.runtime_environment)
    environment[base.device_environment_variable] = instance.device_id
    return argv, environment

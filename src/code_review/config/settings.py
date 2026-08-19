import os
import platform
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field, TypeAdapter, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from code_review.config.deployment_manifest import DeploymentManifest
from code_review.deployment.models import DeploymentMode


def _default_deployment_mode() -> DeploymentMode:
    return (
        DeploymentMode.DEEPSEEK_ONLY if platform.system() == "Windows" else DeploymentMode.PPU_LOCAL
    )


def _default_endpoints() -> list[AnyHttpUrl]:
    if platform.system() == "Windows":
        return []
    return TypeAdapter(list[AnyHttpUrl]).validate_python(
        ["http://127.0.0.1:30000", "http://127.0.0.1:30001"]
    )


def _default_state_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.cwd())) / "CheckCode"
    state_home = os.environ.get("XDG_STATE_HOME")
    return (Path(state_home) if state_home else Path("/var/tmp")) / "check-code"


def _state_path(*parts: str) -> Path:
    return _default_state_dir().joinpath(*parts)


def _default_model_search_roots() -> list[Path]:
    if platform.system() == "Windows":
        return []
    roots = [Path("/LLM"), Path("/models")]
    for variable in ("HF_HOME", "HF_HUB_CACHE"):
        if value := os.environ.get(variable):
            roots.append(Path(value))
    return roots


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODE_REVIEW_",
        env_file=".env",
        extra="ignore",
        validate_default=True,
        populate_by_name=True,
    )

    deployment_mode: DeploymentMode = Field(default_factory=_default_deployment_mode)
    default_model_profile_id: str = "local-qwen3-8b"
    state_dir: Path = Field(default_factory=_default_state_dir)
    deployment_manifest_path: Path = Field(
        default_factory=lambda: _state_path("deployment", "manifest.json")
    )
    deployment_apply_enabled: bool = False
    deployment_plan_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    sglang_endpoints: list[AnyHttpUrl] = Field(default_factory=_default_endpoints, max_length=16)
    model_name: str = "Qwen3-8B"
    qwen3_32b_endpoints: list[AnyHttpUrl] = Field(default_factory=list, max_length=16)
    qwen3_32b_model_name: str = "Qwen3-32B"
    qwen3_32b_context_tokens: int = Field(default=40960, ge=4096)
    model_search_roots: list[Path] = Field(
        default_factory=_default_model_search_roots, max_length=16
    )
    ppu_python_executable: Path = Field(
        default_factory=lambda: (
            Path(sys.executable)
            if platform.system() == "Windows"
            else Path("/usr/local/bin/python3")
        )
    )
    ppu_sdk_path: Path = Path("/usr/local/PPU_SDK")
    ppu_cuda_path: Path = Path("/usr/local/cuda")
    ppu_model_path: Path | None = Field(
        default_factory=lambda: None if platform.system() == "Windows" else Path("/LLM/qwen3")
    )
    ppu_device_ids: list[str] = Field(
        default_factory=lambda: [] if platform.system() == "Windows" else ["0", "1"],
        max_length=16,
    )
    ppu_device_paths: list[Path] = Field(
        default_factory=lambda: [Path("/dev/ppu0"), Path("/dev/davinci0")], max_length=32
    )
    ppu_runtime_dir: Path = Field(default_factory=lambda: _state_path("ppu-model"))
    ppu_device_environment_variable: str = "CUDA_VISIBLE_DEVICES"
    request_timeout_seconds: int = Field(default=600, ge=1, le=600)
    failure_threshold: int = Field(default=3, ge=1, le=10)
    circuit_cooldown_seconds: int = Field(default=30, ge=1, le=600)
    max_output_tokens: int = Field(default=32768, ge=128, le=32768)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    database_path: Path = Field(default_factory=lambda: _state_path("reviews.sqlite3"))
    model_context_tokens: int = Field(default=40960, ge=4096)
    context_safety_tokens: int = Field(default=1024, ge=256)
    minimum_output_tokens: int = Field(default=512, ge=128)
    max_chunk_split_depth: int = Field(default=3, ge=1, le=8)
    review_queue_limit: int = Field(default=64, ge=1, le=4096)
    review_retention_hours: int | None = Field(default=None, ge=1, le=876000)
    capacity_profile_path: Path = Field(
        default_factory=lambda: _state_path("ppu-model", "capacity.json")
    )
    deepseek_base_url: AnyHttpUrl = AnyHttpUrl("https://api.deepseek.com")
    deepseek_model_name: str = "deepseek-v4-flash"
    deepseek_context_tokens: int = Field(default=1_000_000, ge=4096)
    deepseek_max_concurrency: int = Field(default=8, ge=1, le=100)
    deepseek_timeout_seconds: int = Field(default=180, ge=1, le=600)
    deepseek_max_retries: int = Field(default=2, ge=0, le=5)
    admin_username: str | None = None
    admin_password: str | None = Field(default=None, repr=False)

    @model_validator(mode="before")
    @classmethod
    def derive_state_paths(cls, data: object) -> object:
        if not isinstance(data, dict) or data.get("state_dir") is None:
            return data
        values = dict(data)
        state_dir = Path(values["state_dir"])
        values.setdefault("deployment_manifest_path", state_dir / "deployment" / "manifest.json")
        values.setdefault("ppu_runtime_dir", state_dir / "ppu-model")
        values.setdefault("database_path", state_dir / "reviews.sqlite3")
        values.setdefault("capacity_profile_path", state_dir / "ppu-model" / "capacity.json")
        return values

    @model_validator(mode="after")
    def validate_deployment(self) -> "GatewaySettings":
        allowed = {
            DeploymentMode.PPU_LOCAL: {"local-qwen3-8b", "local-qwen3-32b"},
            DeploymentMode.DEEPSEEK_ONLY: {"deepseek-api"},
            DeploymentMode.HYBRID: {
                "local-qwen3-8b", "local-qwen3-32b", "deepseek-api"
            },
        }
        if self.deployment_mode == DeploymentMode.DEEPSEEK_ONLY:
            self.default_model_profile_id = "deepseek-api"
            self.sglang_endpoints = []
            self.qwen3_32b_endpoints = []
        elif self.deployment_mode == DeploymentMode.PPU_LOCAL:
            self.default_model_profile_id = "local-qwen3-8b"
        if self.default_model_profile_id not in allowed[self.deployment_mode]:
            raise ValueError("default model profile is disabled by the deployment mode")
        return self

    @property
    def local_provider_enabled(self) -> bool:
        return self.deployment_mode in {DeploymentMode.PPU_LOCAL, DeploymentMode.HYBRID}

    @property
    def deepseek_provider_enabled(self) -> bool:
        return self.deployment_mode in {DeploymentMode.DEEPSEEK_ONLY, DeploymentMode.HYBRID}


SGLangEndpointSettings = AnyHttpUrl


@lru_cache
def get_settings() -> GatewaySettings:
    settings = GatewaySettings()
    manifest = DeploymentManifest.load(settings.deployment_manifest_path)
    if manifest is None:
        return settings
    return GatewaySettings.model_validate(
        {
            **settings.model_dump(),
            "deployment_mode": manifest.deployment_mode,
            "default_model_profile_id": manifest.default_model_profile_id,
            "sglang_endpoints": TypeAdapter(list[AnyHttpUrl]).validate_python(
                manifest.sglang_endpoints
            ),
            "qwen3_32b_endpoints": TypeAdapter(list[AnyHttpUrl]).validate_python(
                manifest.qwen3_32b_endpoints
            ),
            "ppu_model_path": manifest.ppu_model_path,
            "ppu_device_ids": manifest.ppu_device_ids,
        }
    )

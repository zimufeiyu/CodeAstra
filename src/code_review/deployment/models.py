from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DeploymentMode(StrEnum):
    PPU_LOCAL = "ppu_local"
    DEEPSEEK_ONLY = "deepseek_only"
    HYBRID = "hybrid"


class CapabilityStatus(StrEnum):
    READY = "ready"
    READY_WITH_WARNINGS = "ready_with_warnings"
    MISSING_RUNTIME = "missing_runtime"
    MISSING_DEVICE = "missing_device"
    MISSING_MODEL = "missing_model"
    UNSUPPORTED = "unsupported"
    ALREADY_RUNNING = "already_running"


class CapabilityCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class CapabilityReport(BaseModel):
    status: CapabilityStatus
    platform: str
    checks: list[CapabilityCheck]
    detected_endpoints: list[str] = Field(default_factory=list)
    recommended_mode: DeploymentMode
    can_manage_local_model: bool = False
    warnings: list[str] = Field(default_factory=list)


class ModelCandidate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    path: Path
    architecture: str
    torch_dtype: str
    context_tokens: int = Field(ge=1)
    shard_count: int = Field(ge=1)
    supported: bool = True
    unavailable_reason: str | None = None


class DeploymentPlanRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    mode: DeploymentMode
    model_path: Path | None = None
    endpoints: list[str] = Field(default_factory=list, max_length=16)
    device_ids: list[str] = Field(default_factory=list, max_length=16)


class DeploymentPlan(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    plan_id: str
    mode: DeploymentMode
    default_profile_id: str
    action: str
    model_path: Path | None = None
    endpoints: list[str] = Field(default_factory=list)
    device_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    restart_required: bool = True


class DeploymentApplyRequest(BaseModel):
    plan: DeploymentPlan
    confirm: bool = False


class DeploymentApplyResponse(BaseModel):
    mode: DeploymentMode
    manifest_path: Path
    restart_required: bool
    backup_path: Path | None = None


class DeploymentStatus(BaseModel):
    mode: DeploymentMode
    default_profile_id: str
    local_enabled: bool
    deepseek_enabled: bool
    configured_endpoints: list[str]
    apply_enabled: bool
    manifest_path: Path

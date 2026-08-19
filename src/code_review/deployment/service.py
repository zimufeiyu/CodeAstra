from __future__ import annotations

import asyncio
import secrets
from time import monotonic

import httpx

from code_review.config.deployment_manifest import DeploymentManifest
from code_review.config.settings import GatewaySettings
from code_review.deployment.launcher import LocalModelLauncher
from code_review.deployment.model_discovery import ModelDiscovery
from code_review.deployment.models import (
    CapabilityReport,
    CapabilityStatus,
    DeploymentApplyRequest,
    DeploymentApplyResponse,
    DeploymentMode,
    DeploymentPlan,
    DeploymentPlanRequest,
    DeploymentStatus,
    ModelCandidate,
)
from code_review.deployment.probe import ServerCapabilityProbe


class MigrationService:
    def __init__(
        self,
        settings: GatewaySettings,
        http_client: httpx.AsyncClient,
        launcher: LocalModelLauncher | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._discovery = ModelDiscovery(settings.model_search_roots)
        self._launcher = launcher or LocalModelLauncher(settings, http_client)
        self._plans: dict[str, tuple[DeploymentPlan, float]] = {}
        self._apply_lock = asyncio.Lock()

    def status(self) -> DeploymentStatus:
        return DeploymentStatus(
            mode=self._settings.deployment_mode,
            default_profile_id=self._settings.default_model_profile_id,
            local_enabled=self._settings.local_provider_enabled,
            deepseek_enabled=self._settings.deepseek_provider_enabled,
            apply_enabled=self._settings.deployment_apply_enabled,
            configured_endpoints=[
                str(item).rstrip("/") for item in self._settings.sglang_endpoints
            ],
            manifest_path=self._settings.deployment_manifest_path,
        )

    async def probe(self) -> CapabilityReport:
        return await ServerCapabilityProbe(
            self._http_client,
            python_executable=self._settings.ppu_python_executable,
            sdk_path=self._settings.ppu_sdk_path,
            device_paths=self._settings.ppu_device_paths,
            model_path=self._settings.ppu_model_path,
            endpoints=[str(item).rstrip("/") for item in self._settings.sglang_endpoints],
            state_dir=self._settings.state_dir,
            expected_model_name=self._settings.model_name,
        ).probe()

    def discover_models(self) -> list[ModelCandidate]:
        return self._discovery.discover()

    def _remember(self, draft: DeploymentPlan) -> DeploymentPlan:
        plan = draft.model_copy(update={"plan_id": secrets.token_urlsafe(18)})
        self._plans[plan.plan_id] = (
            plan,
            monotonic() + self._settings.deployment_plan_ttl_seconds,
        )
        return plan

    async def plan(self, request: DeploymentPlanRequest) -> DeploymentPlan:
        if request.mode == DeploymentMode.DEEPSEEK_ONLY:
            return self._remember(
                DeploymentPlan(
                    plan_id="pending",
                    mode=request.mode,
                    default_profile_id="deepseek-api",
                    action="configure_deepseek",
                )
            )

        report = await self.probe()
        requested = [item.rstrip("/") for item in request.endpoints]
        healthy = set(report.detected_endpoints)
        if requested and not set(requested).issubset(healthy):
            raise ValueError("only detected healthy model endpoints may be reused")
        detected = requested or report.detected_endpoints
        if detected:
            return self._remember(
                DeploymentPlan(
                    plan_id="pending",
                    mode=request.mode,
                    default_profile_id="local-qwen3-8b",
                    action="reuse_endpoints",
                    model_path=self._settings.ppu_model_path,
                    endpoints=[item.rstrip("/") for item in detected],
                    device_ids=self._settings.ppu_device_ids,
                    warnings=["Existing model services will be reused and will not be restarted."],
                )
            )
        if report.status == CapabilityStatus.UNSUPPORTED:
            raise ValueError("managed local Qwen deployment requires a supported Linux PPU server")
        blocked_statuses = {
            CapabilityStatus.MISSING_RUNTIME,
            CapabilityStatus.MISSING_DEVICE,
            CapabilityStatus.READY_WITH_WARNINGS,
        }
        if report.status in blocked_statuses:
            raise ValueError(f"server is not ready for managed local launch: {report.status.value}")

        model_path = request.model_path or self._settings.ppu_model_path
        if model_path is None:
            raise ValueError("select a supported local model before creating the plan")
        candidate = self._discovery.validate_manual_path(model_path)
        device_ids = request.device_ids or self._settings.ppu_device_ids
        endpoints = [str(item).rstrip("/") for item in self._settings.sglang_endpoints]
        if not device_ids or len(endpoints) != len(device_ids):
            raise ValueError(
                "local endpoints and device IDs must be non-empty and have equal length"
            )
        return self._remember(
            DeploymentPlan(
                plan_id="pending",
                mode=request.mode,
                default_profile_id="local-qwen3-8b",
                action="launch_local",
                model_path=candidate.path,
                endpoints=endpoints,
                device_ids=device_ids,
            )
        )

    def _validate_plan(self, plan: DeploymentPlan) -> None:
        configured_endpoints = [str(item).rstrip("/") for item in self._settings.sglang_endpoints]
        expected_default = (
            "deepseek-api" if plan.mode == DeploymentMode.DEEPSEEK_ONLY else "local-qwen3-8b"
        )
        if plan.default_profile_id != expected_default:
            raise ValueError("deployment plan has an invalid default provider")
        if plan.action == "configure_deepseek":
            if (
                plan.mode != DeploymentMode.DEEPSEEK_ONLY
                or plan.endpoints
                or plan.device_ids
                or plan.model_path is not None
            ):
                raise ValueError("DeepSeek-only plan contains local deployment settings")
        elif plan.action == "reuse_endpoints":
            if plan.mode == DeploymentMode.DEEPSEEK_ONLY or not plan.endpoints:
                raise ValueError("endpoint reuse is invalid for this deployment mode")
            if not set(plan.endpoints).issubset(set(configured_endpoints)):
                raise ValueError("deployment plan contains an unconfigured endpoint")
        elif plan.action == "launch_local":
            if plan.mode == DeploymentMode.DEEPSEEK_ONLY or plan.model_path is None:
                raise ValueError("local launch is invalid for this deployment mode")
            self._discovery.validate_manual_path(plan.model_path)
            if plan.endpoints != configured_endpoints:
                raise ValueError("local launch endpoints differ from server configuration")
            if plan.device_ids != self._settings.ppu_device_ids:
                raise ValueError("local launch devices differ from server configuration")
        else:
            raise ValueError("deployment plan contains an unsupported action")

    async def apply(self, request: DeploymentApplyRequest) -> DeploymentApplyResponse:
        if not request.confirm:
            raise ValueError("deployment plan confirmation is required")
        if not self._settings.deployment_apply_enabled:
            raise ValueError(
                "deployment changes are disabled; set "
                "CODE_REVIEW_DEPLOYMENT_APPLY_ENABLED=true for an authorized migration"
            )

        async with self._apply_lock:
            issued = self._plans.get(request.plan.plan_id)
            if issued is None:
                raise ValueError("deployment plan is unknown or has already been applied")
            issued_plan, expires_at = issued
            if monotonic() > expires_at:
                self._plans.pop(request.plan.plan_id, None)
                raise ValueError("deployment plan expired; create a new preview")
            if issued_plan != request.plan:
                raise ValueError("deployment plan was modified after preview")
            self._validate_plan(request.plan)

            manifest_path = self._settings.deployment_manifest_path
            backup = DeploymentManifest.backup_existing(manifest_path)
            manifest = DeploymentManifest(
                deployment_mode=request.plan.mode,
                default_model_profile_id=request.plan.default_profile_id,
                sglang_endpoints=request.plan.endpoints,
                ppu_model_path=request.plan.model_path,
                ppu_device_ids=request.plan.device_ids,
            )
            try:
                manifest.write_atomic(manifest_path)
                if request.plan.action == "launch_local":
                    await self._launcher.launch(request.plan)
            except BaseException:
                DeploymentManifest.restore(manifest_path, backup)
                raise

            self._plans.pop(request.plan.plan_id, None)
            return DeploymentApplyResponse(
                mode=request.plan.mode,
                manifest_path=manifest_path,
                restart_required=True,
                backup_path=backup,
            )

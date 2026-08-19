from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from code_review.api.dependencies import get_migration_service
from code_review.deployment.models import (
    CapabilityReport,
    DeploymentApplyRequest,
    DeploymentApplyResponse,
    DeploymentPlan,
    DeploymentPlanRequest,
    DeploymentStatus,
    ModelCandidate,
)
from code_review.deployment.service import MigrationService


def require_local_operator(request: Request) -> None:
    if request.client is None:
        raise HTTPException(status_code=403, detail="deployment management is local-only")
    try:
        address = ip_address(request.client.host)
    except ValueError as error:
        raise HTTPException(
            status_code=403, detail="deployment management is local-only"
        ) from error
    mapped = getattr(address, "ipv4_mapped", None)
    if not address.is_loopback and not (mapped is not None and mapped.is_loopback):
        raise HTTPException(status_code=403, detail="deployment management is local-only")


router = APIRouter(
    prefix="/v1/deployment",
    tags=["deployment"],
    dependencies=[Depends(require_local_operator)],
)
MigrationDependency = Annotated[MigrationService, Depends(get_migration_service)]


@router.get("/status", response_model=DeploymentStatus)
async def deployment_status(service: MigrationDependency) -> DeploymentStatus:
    return service.status()


@router.post("/probe", response_model=CapabilityReport)
async def probe_server(service: MigrationDependency) -> CapabilityReport:
    return await service.probe()


@router.get("/models", response_model=list[ModelCandidate])
async def discover_models(service: MigrationDependency) -> list[ModelCandidate]:
    return service.discover_models()


@router.post("/plan", response_model=DeploymentPlan)
async def create_deployment_plan(
    payload: DeploymentPlanRequest,
    service: MigrationDependency,
) -> DeploymentPlan:
    try:
        return await service.plan(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/apply", response_model=DeploymentApplyResponse)
async def apply_deployment_plan(
    payload: DeploymentApplyRequest,
    service: MigrationDependency,
) -> DeploymentApplyResponse:
    try:
        return await service.apply(payload)
    except (RuntimeError, TimeoutError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

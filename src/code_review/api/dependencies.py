from datetime import UTC, datetime
from functools import lru_cache

import httpx

from code_review.application.auth_service import AuthService
from code_review.application.health_service import GatewayHealthService
from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.inference_service import InferenceService
from code_review.application.model_router import RoutedInferenceService
from code_review.config.settings import get_settings
from code_review.deployment.service import MigrationService
from code_review.domain.model_protocol import ModelSelection
from code_review.infrastructure.deepseek.catalog import DeepSeekModelCatalog
from code_review.infrastructure.deepseek.client import DeepSeekClient
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore
from code_review.infrastructure.persistence.sqlite_review_store import SQLiteReviewStore
from code_review.infrastructure.sglang.capacity import CapacityProfile, EndpointCapacity
from code_review.infrastructure.sglang.client import SGLangClient
from code_review.infrastructure.sglang.registry import InstanceRegistry


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


@lru_cache
def get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@lru_cache
def get_deepseek_model_catalog() -> DeepSeekModelCatalog:
    settings = get_settings()
    return DeepSeekModelCatalog(
        get_http_client(),
        str(settings.deepseek_base_url),
        settings.deepseek_timeout_seconds,
        max_retries=settings.deepseek_max_retries,
    )


@lru_cache
def get_instance_registry() -> InstanceRegistry:
    settings = get_settings()
    endpoints = (
        [str(endpoint).rstrip("/") for endpoint in settings.sglang_endpoints]
        if settings.local_provider_enabled
        else []
    )
    return InstanceRegistry(
        endpoints=endpoints,
        failure_threshold=settings.failure_threshold,
        cooldown_seconds=settings.circuit_cooldown_seconds,
        clock=SystemClock(),
        capacity_profile=CapacityProfile.load(settings.capacity_profile_path, endpoints),
    )


@lru_cache
def get_inference_service() -> RoutedInferenceService:
    settings = get_settings()
    services: dict[str, InferenceService] = {}
    if settings.local_provider_enabled and settings.sglang_endpoints:
        services["local-qwen3-8b"] = InferenceService(
            client=SGLangClient(
                http_client=get_http_client(),
                model_name=settings.model_name,
                timeout_seconds=settings.request_timeout_seconds,
            ),
            registry=get_instance_registry(),
        )
    if settings.local_provider_enabled and settings.qwen3_32b_endpoints:
        qwen32_endpoints = [
            str(endpoint).rstrip("/") for endpoint in settings.qwen3_32b_endpoints
        ]
        services["local-qwen3-32b"] = InferenceService(
            client=SGLangClient(
                http_client=get_http_client(),
                model_name=settings.qwen3_32b_model_name,
                timeout_seconds=settings.request_timeout_seconds,
            ),
            registry=InstanceRegistry(
                endpoints=qwen32_endpoints,
                failure_threshold=settings.failure_threshold,
                cooldown_seconds=settings.circuit_cooldown_seconds,
                clock=SystemClock(),
                capacity_profile=CapacityProfile.load(
                    settings.capacity_profile_path, qwen32_endpoints
                ),
            ),
        )
    if settings.deepseek_provider_enabled:
        endpoint = str(settings.deepseek_base_url).rstrip("/")
        capacity = CapacityProfile(
            {
                endpoint: EndpointCapacity(
                    max_concurrency=settings.deepseek_max_concurrency,
                    max_inflight_prompt_tokens=settings.deepseek_context_tokens
                    * settings.deepseek_max_concurrency,
                    max_inflight_total_tokens=(
                        settings.deepseek_context_tokens + settings.max_output_tokens
                    )
                    * settings.deepseek_max_concurrency,
                )
            }
        )
        registry = InstanceRegistry(
            endpoints=[endpoint],
            failure_threshold=settings.failure_threshold,
            cooldown_seconds=settings.circuit_cooldown_seconds,
            clock=SystemClock(),
            capacity_profile=capacity,
        )
        services["deepseek-api"] = InferenceService(
            client=DeepSeekClient(
                http_client=get_http_client(),
                timeout_seconds=settings.deepseek_timeout_seconds,
                max_retries=settings.deepseek_max_retries,
            ),
            registry=registry,
        )
    return RoutedInferenceService(services, default_profile_id=settings.default_model_profile_id)


@lru_cache
def get_gateway_health_service() -> GatewayHealthService:
    return GatewayHealthService(
        get_inference_service().profile_registries,
        get_http_client(),
    )


@lru_cache
def get_migration_service() -> MigrationService:
    return MigrationService(get_settings(), get_http_client())


@lru_cache
def get_review_store() -> SQLiteReviewStore:
    return SQLiteReviewStore(get_settings().database_path)


@lru_cache
def get_auth_service() -> AuthService:
    settings = get_settings()
    if settings.admin_username and settings.admin_password:
        migrate_auth_schema(
            settings.database_path,
            admin_username=settings.admin_username,
            admin_password=settings.admin_password,
        )
    return AuthService(SQLiteAuthStore(settings.database_path))


@lru_cache
def get_hybrid_review_service() -> HybridReviewService:
    settings = get_settings()
    all_profiles = {
        "local-qwen3-8b": ModelSelection(
            profile_id="local-qwen3-8b",
            provider="local",
            model=settings.model_name,
            display_name="本地 Qwen3-8B",
        ),
        "local-qwen3-32b": ModelSelection(
            profile_id="local-qwen3-32b",
            provider="local",
            model=settings.qwen3_32b_model_name,
            display_name="\u672c\u5730 Qwen3-32B",
        ),
        "deepseek-api": ModelSelection(
            profile_id="deepseek-api",
            provider="deepseek",
            model=settings.deepseek_model_name,
            display_name="DeepSeek API",
        ),
    }
    inference = get_inference_service()
    profiles = {
        key: value for key, value in all_profiles.items() if key in inference.available_profile_ids
    }
    return HybridReviewService(
        inference,
        get_review_store(),
        settings=settings,
        model_profiles=profiles,
        available_model_profile_ids=inference.available_profile_ids,
        model_context_tokens={
            key: value
            for key, value in {
                "local-qwen3-8b": settings.model_context_tokens,
                "local-qwen3-32b": settings.qwen3_32b_context_tokens,
                "deepseek-api": settings.deepseek_context_tokens,
            }.items()
            if key in profiles
        },
    )


async def close_dependencies() -> None:
    if get_hybrid_review_service.cache_info().currsize:
        await get_hybrid_review_service().shutdown()
    if get_http_client.cache_info().currsize:
        await get_http_client().aclose()
    if get_review_store.cache_info().currsize:
        await get_review_store().close()
    for dependency in (
        get_http_client,
        get_inference_service,
        get_deepseek_model_catalog,
        get_gateway_health_service,
        get_instance_registry,
        get_migration_service,
        get_hybrid_review_service,
        get_review_store,
        get_auth_service,
    ):
        dependency.cache_clear()

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from code_review.api import dependencies
from code_review.api.auth_routes import create_auth_router
from code_review.application.auth_service import AuthService
from code_review.application.health_service import GatewayHealthService
from code_review.config.settings import GatewaySettings
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


@pytest.mark.asyncio
async def test_me_returns_real_account_state_and_password_timestamp(tmp_path):
    database = tmp_path / "auth.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="current-password",
    )
    service = AuthService(SQLiteAuthStore(database))
    login = await service.login("admin", "current-password")
    app = FastAPI()
    app.include_router(create_auth_router(service))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": login.session_token, "csrf": login.csrf_token},
    ) as client:
        response = await client.get("/v1/auth/me")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_active"] is True
    assert payload["password_changed_at"]


def local_settings() -> GatewaySettings:
    return GatewaySettings(
        deployment_mode="hybrid",
        default_model_profile_id="local-qwen3-8b",
        sglang_endpoints=["http://127.0.0.1:30000"],
        qwen3_32b_endpoints=["http://127.0.0.1:30001"],
    )


def test_hybrid_settings_keep_both_local_model_endpoints():
    settings = local_settings()
    assert [str(item).rstrip("/") for item in settings.sglang_endpoints] == [
        "http://127.0.0.1:30000"
    ]
    assert [str(item).rstrip("/") for item in settings.qwen3_32b_endpoints] == [
        "http://127.0.0.1:30001"
    ]


@pytest.mark.asyncio
async def test_router_binds_each_local_profile_to_its_own_model(monkeypatch):
    settings = local_settings()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    dependencies.get_inference_service.cache_clear()
    service = dependencies.get_inference_service()
    assert {"local-qwen3-8b", "local-qwen3-32b"} <= service.available_profile_ids
    qwen8 = service._services["local-qwen3-8b"]
    qwen32 = service._services["local-qwen3-32b"]
    assert qwen8._registry.snapshot()[0].endpoint == "http://127.0.0.1:30000"
    assert qwen32._registry.snapshot()[0].endpoint == "http://127.0.0.1:30001"
    lease = await qwen8._registry.acquire(estimated_tokens=10, prompt_tokens=4)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        states = await GatewayHealthService(service.profile_registries, client).snapshot()
    by_id = {item.endpoint_id: item for item in states}
    assert {"local-qwen3-8b-0", "local-qwen3-32b-0"} <= set(by_id)
    assert "deepseek-api-0" in by_id
    assert by_id["local-qwen3-8b-0"].inflight_requests == 1
    assert by_id["local-qwen3-32b-0"].inflight_requests == 0
    await qwen8._registry.release_neutral(lease)


@pytest.mark.asyncio
async def test_health_marks_an_unlistening_local_profile_unavailable(monkeypatch):
    settings = local_settings()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    dependencies.get_inference_service.cache_clear()
    service = dependencies.get_inference_service()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 30000:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        states = await GatewayHealthService(service.profile_registries, client).snapshot()

    by_id = {item.endpoint_id: item for item in states}
    assert by_id["local-qwen3-8b-0"].available is False
    assert by_id["local-qwen3-8b-0"].reason_code == "connection_refused"
    assert by_id["local-qwen3-32b-0"].available is True

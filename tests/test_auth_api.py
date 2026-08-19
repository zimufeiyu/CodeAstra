from __future__ import annotations

import httpx
import pytest

from code_review.application.auth_service import AuthService
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


async def _service(tmp_path) -> AuthService:
    database = tmp_path / "auth.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="current-password",
    )
    return AuthService(SQLiteAuthStore(database))


@pytest.mark.asyncio
async def test_change_password_accepts_current_and_new_password_and_clears_cookies(tmp_path):
    service = await _service(tmp_path)
    login = await service.login("admin", "current-password")
    from code_review.api.auth_routes import create_auth_router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_auth_router(service))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": login.session_token},
    ) as client:
        response = await client.post(
            "/v1/auth/change-password",
            json={
                "current_password": "current-password",
                "new_password": "replacement-password",
            },
            headers={"X-CSRF-Token": login.csrf_token},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith("session=") and "Max-Age=0" in cookie for cookie in cookies)
    assert any(cookie.startswith("csrf=") and "Max-Age=0" in cookie for cookie in cookies)
    assert await service.current_user(login.session_token) is None


@pytest.mark.asyncio
async def test_logout_all_endpoint_clears_callers_cookies_and_revokes_all_sessions(tmp_path):
    service = await _service(tmp_path)
    first = await service.login("admin", "current-password")
    second = await service.login("admin", "current-password")
    from code_review.api.auth_routes import create_auth_router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_auth_router(service))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": second.session_token},
    ) as client:
        response = await client.post(
            "/v1/auth/logout-all",
            headers={"X-CSRF-Token": second.csrf_token},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith("session=") and "Max-Age=0" in cookie for cookie in cookies)
    assert any(cookie.startswith("csrf=") and "Max-Age=0" in cookie for cookie in cookies)
    assert await service.current_user(first.session_token) is None
    assert await service.current_user(second.session_token) is None


@pytest.mark.asyncio
async def test_must_change_user_is_blocked_from_business_and_admin_but_can_use_allowed_auth_routes(
    monkeypatch, tmp_path
):
    service = await _service(tmp_path)
    login = await service.login("admin", "current-password")
    from code_review.api import main

    monkeypatch.setattr(main, "get_auth_service", lambda: service)
    app = main.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": login.session_token, "csrf": login.csrf_token},
    ) as client:
        assert (await client.get("/v1/auth/me")).status_code == 200
        assert (await client.get("/v1/reviews")).status_code == 403
        assert (await client.get("/v1/admin/users")).status_code == 403
        response = await client.post(
            "/v1/auth/logout-all",
            headers={"X-CSRF-Token": login.csrf_token},
        )

    assert response.status_code == 200
    assert await service.current_user(login.session_token) is None
@pytest.mark.asyncio
async def test_sessions_endpoint_reports_only_the_current_device(tmp_path):
    service = await _service(tmp_path)
    first = await service.login("admin", "current-password")
    second = await service.login("admin", "current-password")
    from code_review.api.auth_routes import create_auth_router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_auth_router(service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"session": second.session_token},
    ) as client:
        response = await client.get("/v1/auth/sessions")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["session_id"] == second.session_id
    assert response.json()["items"][0]["current"] is True
    assert await service.current_user(first.session_token) is None

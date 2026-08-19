from __future__ import annotations

import httpx
import pytest

from code_review.api.admin_routes import create_admin_router
from code_review.application.auth_service import AuthService
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


@pytest.mark.asyncio
async def test_admin_create_uses_fixed_temporary_password_and_accepts_username_only(tmp_path):
    database = tmp_path / "admin.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="current-password",
    )
    service = AuthService(SQLiteAuthStore(database))
    login = await service.login("admin", "current-password")

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(service))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": login.session_token},
    ) as client:
        response = await client.post(
            "/v1/admin/users",
            json={"username": "alice"},
            headers={"X-CSRF-Token": login.csrf_token},
        )

    assert response.status_code == 201
    assert response.json()["temporary_password"] == "12345678"
    created_login = await service.login("alice", "12345678")
    assert created_login.user.must_change_password is True


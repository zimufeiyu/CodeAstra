from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from code_review.api.admin_routes import create_admin_router
from code_review.application.auth_service import AuthService
from code_review.application.user_admin_service import UserAdminService
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


def services(tmp_path):
    database = tmp_path / "admin.sqlite3"
    migrate_auth_schema(database, admin_username="admin", admin_password="current-password")
    store = SQLiteAuthStore(database)
    auth = AuthService(store)
    admin = UserAdminService(store, password_hasher=auth.hash_password)
    return store, auth, admin


@pytest.mark.asyncio
async def test_reset_password_and_session_revocation_roll_back_together(tmp_path):
    store, auth, admin = services(tmp_path)
    alice = await admin.create_user("alice")
    await store.set_password(alice.user_id, auth.hash_password("changed-password"), False)
    session = await auth.login("alice", "changed-password")
    store.connection.execute(
        """CREATE TRIGGER block_session_delete BEFORE DELETE ON auth_sessions
           BEGIN SELECT RAISE(ABORT, 'blocked'); END"""
    )
    store.connection.commit()

    with pytest.raises(Exception, match="blocked"):
        await admin.reset_password(alice.user_id)

    assert await auth.current_user(session.session_token) is not None
    store.connection.execute("DROP TRIGGER block_session_delete")
    store.connection.commit()
    assert (await auth.login("alice", "changed-password")).user.must_change_password is False


@pytest.mark.asyncio
async def test_admin_reads_require_admin_and_mutations_require_csrf(tmp_path):
    _, auth, admin = services(tmp_path)
    normal = await admin.create_user("alice")
    admin_login = await auth.login("admin", "current-password")
    user_login = await auth.login(normal.username, "12345678")
    app = FastAPI()
    app.include_router(create_admin_router(auth, admin))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("session", admin_login.session_token)
        assert (await client.get("/v1/admin/users")).status_code == 200
        assert (await client.post("/v1/admin/users", json={"username": "bob"})).status_code == 403
        created = await client.post(
            "/v1/admin/users", json={"username": "bob"},
            headers={"X-CSRF-Token": admin_login.csrf_token},
        )
        assert created.status_code == 201
        client.cookies.set("session", user_login.session_token)
        assert (await client.get("/v1/admin/users")).status_code == 403

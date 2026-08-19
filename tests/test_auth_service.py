from __future__ import annotations

import pytest

from code_review.application.auth_service import AuthService, InvalidCredentialsError
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
async def test_new_login_revokes_the_previous_device_and_password_change_revokes_current(tmp_path):
    service = await _service(tmp_path)
    first = await service.login("admin", "current-password")
    second = await service.login("admin", "current-password")

    assert await service.current_user(first.session_token) is None
    assert await service.current_user(second.session_token) is not None
    current_id, sessions = await service.active_sessions(second.session_token)
    assert current_id == second.session_id
    assert [item.session_id for item in sessions] == [second.session_id]

    with pytest.raises(InvalidCredentialsError, match="invalid username or password"):
        await service.change_password(
            second.session_token,
            second.csrf_token,
            "not-the-current-password",
            "replacement-password",
        )

    await service.change_password(
        second.session_token,
        second.csrf_token,
        "current-password",
        "replacement-password",
    )

    assert await service.current_user(second.session_token) is None
    with pytest.raises(InvalidCredentialsError, match="invalid username or password"):
        await service.login("admin", "current-password")
    replacement = await service.login("admin", "replacement-password")
    assert replacement.user.must_change_password is False

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_password", "message"),
    [
        ("short", "at least 8"),
        ("current-password", "different from current"),
        ("12345678", "must not be 12345678"),
    ],
)
async def test_password_change_rejects_disallowed_new_passwords(tmp_path, new_password, message):
    service = await _service(tmp_path)
    login = await service.login("admin", "current-password")

    with pytest.raises(ValueError, match=message):
        await service.change_password(
            login.session_token,
            login.csrf_token,
            "current-password",
            new_password,
        )

    assert await service.current_user(login.session_token) is not None


@pytest.mark.asyncio
async def test_logout_all_requires_csrf_and_revokes_the_current_session(tmp_path):
    service = await _service(tmp_path)
    login = await service.login("admin", "current-password")

    with pytest.raises(PermissionError, match="invalid CSRF token"):
        await service.logout_all(login.session_token, "wrong-csrf")

    assert await service.current_user(login.session_token) is not None
    await service.logout_all(login.session_token, login.csrf_token)
    assert await service.current_user(login.session_token) is None

from __future__ import annotations

import pytest

from code_review.application.auth_service import AuthService, InvalidCredentialsError
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


async def _services(tmp_path) -> tuple[AuthService, SQLiteAuthStore]:
    database = tmp_path / "auth.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="current-password",
    )
    store = SQLiteAuthStore(database)
    return AuthService(store), store


@pytest.mark.asyncio
async def test_password_change_requires_the_current_password_and_revokes_every_session(tmp_path):
    service, store = await _services(tmp_path)
    first = await service.login("admin", "current-password")
    legacy_token = "legacy-password-session"
    await store.create_session(first.user.user_id, legacy_token, "legacy-password-csrf")

    with pytest.raises(InvalidCredentialsError, match="invalid username or password"):
        await service.change_password(
            first.session_token,
            first.csrf_token,
            "not-the-current-password",
            "replacement-password",
        )

    assert await service.current_user(first.session_token) is not None
    assert await service.current_user(legacy_token) is not None

    await service.change_password(
        first.session_token,
        first.csrf_token,
        "current-password",
        "replacement-password",
    )

    assert await service.current_user(first.session_token) is None
    assert await service.current_user(legacy_token) is None
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
    service, _ = await _services(tmp_path)
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
async def test_logout_all_requires_csrf_and_revokes_every_session(tmp_path):
    service, store = await _services(tmp_path)
    first = await service.login("admin", "current-password")
    legacy_token = "legacy-logout-session"
    await store.create_session(first.user.user_id, legacy_token, "legacy-logout-csrf")

    with pytest.raises(PermissionError, match="invalid CSRF token"):
        await service.logout_all(first.session_token, "wrong-csrf")

    assert await service.current_user(legacy_token) is not None
    await service.logout_all(first.session_token, first.csrf_token)

    assert await service.current_user(first.session_token) is None
    assert await service.current_user(legacy_token) is None

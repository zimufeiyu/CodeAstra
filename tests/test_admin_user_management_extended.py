from __future__ import annotations

import sqlite3

import pytest

from code_review.application.auth_service import AuthService
from code_review.application.user_admin_service import UserAdminService
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore
from code_review.infrastructure.persistence.user_data_purger import UserDataPurger


def services(tmp_path):
    database = tmp_path / "admin.sqlite3"
    migrate_auth_schema(database, admin_username="admin", admin_password="current-password")
    store = SQLiteAuthStore(database)
    auth = AuthService(store)
    admin = UserAdminService(store, password_hasher=auth.hash_password)
    return store, auth, admin


@pytest.mark.asyncio
async def test_pagination_search_filter_and_stats(tmp_path):
    _, _, admin = services(tmp_path)
    await admin.create_user("alice")
    await admin.create_user("alina")
    bob = await admin.create_user("bob")
    await admin.set_user_active(bob.user_id, False)
    page = await admin.list_users("ali", "active", 1, 20)
    assert page.total == 2
    assert [item.username for item in page.items] == ["alice", "alina"]
    stats = await admin.user_stats()
    assert (stats.total, stats.active, stats.disabled) == (4, 3, 1)
    with pytest.raises(ValueError):
        await admin.list_users(None, "all", 1, 25)


@pytest.mark.asyncio
async def test_reset_revokes_sessions_and_protects_sole_admin(tmp_path):
    store, auth, admin = services(tmp_path)
    alice = await admin.create_user("alice")
    old_session = await auth.login("alice", "12345678")
    await admin.reset_password(alice.user_id)
    assert await auth.current_user(old_session.session_token) is None
    assert (await auth.login("alice", "12345678")).user.must_change_password
    administrator = (await store.get_user_by_username("admin"))[0]
    with pytest.raises(PermissionError):
        await admin.reset_password(administrator.user_id)
    with pytest.raises(PermissionError):
        await admin.set_user_active(administrator.user_id, False)
    with pytest.raises(PermissionError):
        await admin.delete_user(administrator.user_id)


@pytest.mark.asyncio
async def test_batch_status_is_partial_and_disabling_revokes_sessions(tmp_path):
    store, auth, admin = services(tmp_path)
    alice = await admin.create_user("alice")
    bob = await admin.create_user("bob")
    session = await auth.login("alice", "12345678")
    administrator = (await store.get_user_by_username("admin"))[0]
    result = await admin.batch_set_active(
        [alice.user_id, administrator.user_id, "missing", bob.user_id], False
    )
    assert [item.ok for item in result] == [True, False, False, True]
    assert await auth.current_user(session.session_token) is None


class Reviews:
    def __init__(self):
        self.cancelled = []

    async def cancel(self, review_id, owner_id):
        self.cancelled.append((review_id, owner_id))
        return True


@pytest.mark.asyncio
async def test_delete_cancels_quarantines_and_transactionally_purges(tmp_path):
    store, auth, base = services(tmp_path)
    alice = await base.create_user("alice")
    store.connection.execute(
        "INSERT INTO review_sessions(review_id,owner_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?)",
        ("r1", alice.user_id, "pending", "2026-01-01", "2027-01-01", "{}"),
    )
    store.connection.commit()
    root = tmp_path / "user-files"
    source = root / alice.user_id / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("secret", encoding="utf-8")
    reviews = Reviews()
    admin = UserAdminService(
        store, password_hasher=auth.hash_password, review_service=reviews,
        purger=UserDataPurger([root], tmp_path / "quarantine"),
    )
    await admin.delete_user(alice.user_id)
    assert reviews.cancelled == [("r1", alice.user_id)]
    assert not source.exists()
    assert not (tmp_path / "quarantine").exists()
    assert await store.get_user_by_id(alice.user_id) is None


@pytest.mark.asyncio
async def test_delete_restores_files_on_database_failure(tmp_path, monkeypatch):
    store, auth, base = services(tmp_path)
    alice = await base.create_user("alice")
    root = tmp_path / "user-files"
    source = root / alice.user_id / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("secret", encoding="utf-8")
    admin = UserAdminService(
        store, password_hasher=auth.hash_password,
        purger=UserDataPurger([root], tmp_path / "quarantine"),
    )

    async def fail(_user_id):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(store, "delete_user_record", fail)
    with pytest.raises(sqlite3.OperationalError):
        await admin.delete_user(alice.user_id)
    assert source.read_text(encoding="utf-8") == "secret"
    assert await store.get_user_by_id(alice.user_id) is not None

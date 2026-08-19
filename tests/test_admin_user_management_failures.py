from __future__ import annotations

import shutil

import pytest

from code_review.application.auth_service import AuthService
from code_review.application.user_admin_service import UserAdminService
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore
from code_review.infrastructure.persistence.user_data_purger import UserDataPurger


@pytest.mark.asyncio
async def test_delete_aborts_when_an_owned_review_cannot_be_cancelled(tmp_path):
    database = tmp_path / "admin.sqlite3"
    migrate_auth_schema(database, admin_username="admin", admin_password="current-password")
    store = SQLiteAuthStore(database)
    auth = AuthService(store)
    base = UserAdminService(store, password_hasher=auth.hash_password)
    alice = await base.create_user("alice")
    store.connection.execute(
        "INSERT INTO review_sessions(review_id,owner_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?)",
        ("r1", alice.user_id, "pending", "2026-01-01", "2027-01-01", "{}"),
    )
    store.connection.commit()

    class RefusingReviews:
        async def cancel(self, review_id, owner_id):
            return False

    admin = UserAdminService(
        store, password_hasher=auth.hash_password, review_service=RefusingReviews()
    )
    with pytest.raises(RuntimeError, match="could not cancel"):
        await admin.delete_user(alice.user_id)
    assert await store.get_user_by_id(alice.user_id) is not None
    assert await store.review_ids_for_owner(alice.user_id) == ["r1"]


def test_multi_root_stage_restores_prior_moves_if_a_later_move_fails(tmp_path, monkeypatch):
    user_id = "alice-id"
    roots = [tmp_path / "uploads", tmp_path / "exports"]
    for root in roots:
        path = root / user_id
        path.mkdir(parents=True)
        (path / "data.txt").write_text(root.name, encoding="utf-8")
    purger = UserDataPurger(roots, tmp_path / "quarantine")
    real_move = shutil.move
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected move failure")
        return real_move(source, destination)

    monkeypatch.setattr(shutil, "move", fail_second)
    with pytest.raises(OSError, match="injected"):
        purger.stage(user_id)

    assert (roots[0] / user_id / "data.txt").read_text(encoding="utf-8") == "uploads"
    assert (roots[1] / user_id / "data.txt").read_text(encoding="utf-8") == "exports"
    assert not (tmp_path / "quarantine").exists()


def test_purger_rejects_path_traversal(tmp_path):
    purger = UserDataPurger([tmp_path / "uploads"], tmp_path / "quarantine")
    for unsafe in ("", ".", "..", "../alice", "alice/bob", "alice\\bob"):
        with pytest.raises(ValueError):
            purger.stage(unsafe)

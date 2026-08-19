from __future__ import annotations

import asyncio
import sqlite3

import pytest

from code_review.infrastructure.persistence import sqlite_auth_migration as migration
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_review_store import SQLiteReviewStore


def legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE review_sessions (
            review_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('queued', 'running')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(status, payload)
        );
        CREATE TABLE review_chunks (
            chunk_id TEXT PRIMARY KEY,
            review_id TEXT NOT NULL REFERENCES review_sessions(review_id) ON DELETE CASCADE,
            payload TEXT NOT NULL
        );
        CREATE TABLE review_session_audit (review_id TEXT NOT NULL, status TEXT NOT NULL);
        CREATE INDEX idx_legacy_review_created ON review_sessions(created_at);
        CREATE TRIGGER review_session_status_audit
        AFTER UPDATE OF status ON review_sessions
        BEGIN
            INSERT INTO review_session_audit(review_id, status) VALUES (NEW.review_id, NEW.status);
        END;
        CREATE VIEW review_session_summary AS
        SELECT review_id, status FROM review_sessions;
        """
    )
    connection.execute(
        "INSERT INTO review_sessions VALUES (?, ?, ?, ?, ?)",
        ("legacy-review", "queued", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", '{"title":"legacy"}'),
    )
    connection.execute(
        "INSERT INTO review_chunks VALUES (?, ?, ?)",
        ("legacy-chunk", "legacy-review", '{"part":1}'),
    )
    connection.commit()
    return connection


def _owner_index_columns(connection):
    return [
        row[2]
        for row in connection.execute("PRAGMA index_info(idx_review_sessions_owner_created)")
    ]


def _assert_final_owner_schema(connection):
    columns = {
        row[1]: row for row in connection.execute("PRAGMA table_info(review_sessions)")
    }
    owner_foreign_keys = connection.execute("PRAGMA foreign_key_list(review_sessions)").fetchall()

    assert columns["owner_id"][3] == 1
    assert any(
        row[2] == "users" and row[3] == "owner_id" and row[4] == "user_id"
        for row in owner_foreign_keys
    )
    assert _owner_index_columns(connection) == ["owner_id", "created_at"]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _migrate_legacy_database(database):
    connection = legacy_database(database)
    connection.close()
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )


def _validation_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL
        );
        CREATE TABLE review_sessions (
            review_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(user_id),
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_review_sessions_owner_created
            ON review_sessions(owner_id, created_at);
        """
    )
    connection.execute("INSERT INTO users VALUES ('admin-id', 'admin', 'admin')")
    connection.commit()
    return connection


def test_migration_rebuilds_full_legacy_schema_and_preserves_bound_objects(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = legacy_database(database)
    connection.close()

    migrate_auth_schema(database, admin_username="admin", admin_password="correct horse battery staple")

    connection = sqlite3.connect(database)
    _assert_final_owner_schema(connection)
    admin_id = connection.execute("SELECT user_id FROM users WHERE role = 'admin'").fetchone()[0]
    assert connection.execute(
        "SELECT review_id, status, created_at, expires_at, payload, owner_id FROM review_sessions"
    ).fetchall() == [
        ("legacy-review", "queued", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", '{"title":"legacy"}', admin_id)
    ]
    assert connection.execute("SELECT * FROM review_chunks").fetchall() == [
        ("legacy-chunk", "legacy-review", '{"part":1}')
    ]
    assert connection.execute("SELECT review_id, status FROM review_session_summary").fetchall() == [
        ("legacy-review", "queued")
    ]
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_legacy_review_created'"
    ).fetchone() is not None
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'review_session_status_audit'"
    ).fetchone() is not None
    connection.execute("UPDATE review_sessions SET status = 'running' WHERE review_id = 'legacy-review'")
    assert connection.execute("SELECT * FROM review_session_audit").fetchall() == [
        ("legacy-review", "running")
    ]
    connection.close()


def test_migration_preserves_legacy_check_constraint(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    _migrate_legacy_database(database)

    connection = sqlite3.connect(database)
    admin_id = connection.execute(
        "SELECT user_id FROM users WHERE role = 'admin'"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "INSERT INTO review_sessions VALUES (?, ?, ?, ?, ?, ?)",
            (
                "invalid-status",
                "not-a-status",
                "2026-08-03T00:00:00Z",
                "2026-08-04T00:00:00Z",
                '{"title":"different"}',
                admin_id,
            ),
        )
    connection.rollback()
    connection.close()


def test_migration_preserves_legacy_unique_constraint(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    _migrate_legacy_database(database)

    connection = sqlite3.connect(database)
    admin_id = connection.execute(
        "SELECT user_id FROM users WHERE role = 'admin'"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        connection.execute(
            "INSERT INTO review_sessions VALUES (?, ?, ?, ?, ?, ?)",
            (
                "duplicate-status-payload",
                "queued",
                "2026-08-03T00:00:00Z",
                "2026-08-04T00:00:00Z",
                '{"title":"legacy"}',
                admin_id,
            ),
        )
    connection.rollback()
    connection.close()


def test_rebuild_replaces_wrong_shaped_canonical_owner_index(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = legacy_database(database)
    connection.execute(
        "CREATE INDEX idx_review_sessions_owner_created ON review_sessions(status)"
    )
    connection.commit()
    connection.close()

    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )

    connection = sqlite3.connect(database)
    assert _owner_index_columns(connection) == ["owner_id", "created_at"]
    connection.close()


def test_missing_auth_schema_and_user_version_trigger_backup_before_mutation(tmp_path):
    database = tmp_path / "partial.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE review_sessions (
            review_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(user_id),
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX idx_review_sessions_owner_created
            ON review_sessions(owner_id, created_at);
        PRAGMA user_version = 0;
        """
    )
    connection.close()

    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )

    backups = sorted(tmp_path.glob("partial.sqlite3.*.bak"))
    assert len(backups) == 1
    backup_connection = sqlite3.connect(backups[0])
    assert backup_connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert backup_connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone() is None
    backup_connection.close()

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }.issuperset({"users", "auth_sessions", "auth_audit_log"})
    connection.close()


@pytest.mark.parametrize(
    "mutation_sql",
    [
        "ALTER TABLE users RENAME COLUMN password_hash TO password_digest;",
        "ALTER TABLE auth_sessions RENAME COLUMN csrf_hash TO csrf_digest;",
        "DROP INDEX one_admin; CREATE INDEX one_admin ON users(role);",
        "DROP INDEX one_admin; CREATE UNIQUE INDEX one_admin ON users(role) WHERE role='ADMIN';",
        "DROP INDEX one_admin; CREATE UNIQUE INDEX one_admin ON users(role) WHERE role=' ad min ';",
    ],
    ids=[
        "users-columns",
        "auth-session-columns",
        "one-admin-index",
        "one-admin-uppercase-literal",
        "one-admin-spaced-literal",
    ],
)
def test_malformed_same_name_auth_schema_is_backed_up_and_rejected(
    tmp_path, mutation_sql
):
    database = tmp_path / "malformed-auth.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )
    connection = sqlite3.connect(database)
    connection.executescript(mutation_sql)
    before_schema = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name IN ('users', 'one_admin', 'auth_sessions', 'auth_audit_log') "
        "ORDER BY type, name"
    ).fetchall()
    connection.close()

    with pytest.raises(RuntimeError, match="auth schema is not canonical"):
        migrate_auth_schema(
            database,
            admin_username="admin",
            admin_password="correct horse battery staple",
        )

    backups = sorted(tmp_path.glob("malformed-auth.sqlite3.*.bak"))
    assert len(backups) == 1
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name IN ('users', 'one_admin', 'auth_sessions', 'auth_audit_log') "
        "ORDER BY type, name"
    ).fetchall() == before_schema
    connection.close()
    backup_connection = sqlite3.connect(backups[0])
    assert backup_connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name IN ('users', 'one_admin', 'auth_sessions', 'auth_audit_log') "
        "ORDER BY type, name"
    ).fetchall() == before_schema
    backup_connection.close()


def test_current_fast_path_rejects_orphan_foreign_key_without_mutation(tmp_path):
    database = tmp_path / "orphan.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO review_sessions VALUES (?, ?, ?, ?, ?, ?)",
        (
            "orphan-review",
            "missing-user",
            "queued",
            "2026-08-01T00:00:00Z",
            "2026-08-02T00:00:00Z",
            "{}",
        ),
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match="foreign key check failed"):
        migrate_auth_schema(
            database,
            admin_username="admin",
            admin_password="correct horse battery staple",
        )

    assert database.read_bytes() == before
    assert sorted(tmp_path.glob("orphan.sqlite3.*.bak")) == []


def test_newer_schema_version_is_rejected_without_backup_or_downgrade(tmp_path):
    database = tmp_path / "newer.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 4")
    connection.close()
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match="newer database schema version"):
        migrate_auth_schema(
            database,
            admin_username="admin",
            admin_password="correct horse battery staple",
        )

    assert database.read_bytes() == before
    assert sorted(tmp_path.glob("newer.sqlite3.*.bak")) == []
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    connection.close()

def test_migration_creates_final_review_schema_before_review_store_initialization(tmp_path):
    database = tmp_path / "fresh.sqlite3"

    migrate_auth_schema(database, admin_username="admin", admin_password="correct horse battery staple")
    store = SQLiteReviewStore(database)
    asyncio.run(store.close())

    connection = sqlite3.connect(database)
    _assert_final_owner_schema(connection)
    connection.close()


def test_migration_repairs_missing_index_once_and_skips_backup_when_schema_is_current(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = legacy_database(database)
    connection.close()

    migrate_auth_schema(database, admin_username="admin", admin_password="correct horse battery staple")
    first_backups = sorted(tmp_path.glob("legacy.sqlite3.*.bak"))
    assert len(first_backups) == 1

    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX idx_review_sessions_owner_created")
    connection.commit()
    connection.close()
    migrate_auth_schema(database, admin_username="admin", admin_password="correct horse battery staple")
    connection = sqlite3.connect(database)
    assert _owner_index_columns(connection) == ["owner_id", "created_at"]
    connection.close()
    assert len(sorted(tmp_path.glob("legacy.sqlite3.*.bak"))) == 2

    migrate_auth_schema(database, admin_username="admin", admin_password="correct horse battery staple")
    assert len(sorted(tmp_path.glob("legacy.sqlite3.*.bak"))) == 2


def test_validation_rejects_changed_review_count(tmp_path):
    connection = _validation_database(tmp_path / "validation.sqlite3")
    with pytest.raises(RuntimeError, match="review session count changed"):
        migration._validate_migration(connection, 1, "admin-id")
    connection.close()


def test_validation_rejects_multiple_administrators(tmp_path):
    connection = _validation_database(tmp_path / "validation.sqlite3")
    connection.execute(
        "INSERT INTO users VALUES ('second-admin', 'second', 'admin')"
    )
    with pytest.raises(RuntimeError, match="exactly one administrator"):
        migration._validate_migration(connection, 0, "admin-id")
    connection.close()


def test_validation_rejects_wrong_owner_foreign_key(tmp_path):
    connection = _validation_database(tmp_path / "validation.sqlite3")
    connection.execute("DROP TABLE review_sessions")
    connection.execute(
        "CREATE TABLE review_sessions ("
        "review_id TEXT PRIMARY KEY, "
        "owner_id TEXT NOT NULL REFERENCES users(username), "
        "created_at TEXT NOT NULL)"
    )
    with pytest.raises(RuntimeError, match="owner constraint was not created"):
        migration._validate_migration(connection, 0, "admin-id")
    connection.close()


def test_validation_reports_unowned_reviews_before_owner_schema_drift(tmp_path):
    connection = _validation_database(tmp_path / "validation.sqlite3")
    connection.execute("DROP TABLE review_sessions")
    connection.execute(
        "CREATE TABLE review_sessions ("
        "review_id TEXT PRIMARY KEY, owner_id TEXT, created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO review_sessions VALUES ('unowned', NULL, '2026-08-01')"
    )
    with pytest.raises(RuntimeError, match="unowned review"):
        migration._validate_migration(connection, 1, "admin-id")
    connection.close()


def test_validation_rejects_foreign_key_errors(tmp_path):
    connection = _validation_database(tmp_path / "validation.sqlite3")
    connection.execute(
        "INSERT INTO review_sessions VALUES ('review', 'admin-id', '2026-08-01')"
    )
    connection.execute(
        "CREATE TABLE review_chunks ("
        "chunk_id TEXT PRIMARY KEY, "
        "review_id TEXT NOT NULL REFERENCES review_sessions(review_id))"
    )
    connection.execute("INSERT INTO review_chunks VALUES ('orphan', 'missing-review')")
    with pytest.raises(RuntimeError, match="foreign key check failed"):
        migration._validate_migration(connection, 1, "admin-id")
    connection.close()


def test_real_row_count_validation_failure_rolls_back_and_keeps_backup(
    tmp_path, monkeypatch
):
    database = tmp_path / "legacy.sqlite3"
    connection = legacy_database(database)
    original_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_sessions'"
    ).fetchone()[0]
    connection.close()
    original_rebuild = migration._rebuild_review_sessions

    def rebuild_then_drop_row(connection, admin_id):
        original_rebuild(connection, admin_id)
        connection.execute("DELETE FROM review_sessions")

    monkeypatch.setattr(migration, "_rebuild_review_sessions", rebuild_then_drop_row)
    with pytest.raises(RuntimeError, match="review session count changed"):
        migrate_auth_schema(
            database,
            admin_username="admin",
            admin_password="correct horse battery staple",
        )

    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0] == 1
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_sessions'"
    ).fetchone()[0] == original_table_sql
    connection.close()
    backups = sorted(tmp_path.glob("legacy.sqlite3.*.bak"))
    assert len(backups) == 1
    backup_connection = sqlite3.connect(backups[0])
    assert backup_connection.execute(
        "SELECT COUNT(*) FROM review_sessions"
    ).fetchone()[0] == 1
    backup_connection.close()


def test_migration_rolls_back_full_legacy_schema_and_keeps_backup_readable(tmp_path, monkeypatch):
    database = tmp_path / "legacy.sqlite3"
    connection = legacy_database(database)
    original_table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_sessions'"
    ).fetchone()[0]
    original_trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'review_session_status_audit'"
    ).fetchone()[0]
    connection.close()

    monkeypatch.setattr(
        "code_review.infrastructure.persistence.sqlite_auth_migration._validate_migration",
        lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        migrate_auth_schema(database, admin_username="admin", admin_password="correct horse battery staple")

    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0] == 1
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_sessions'"
    ).fetchone()[0] == original_table_sql
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'review_session_status_audit'"
    ).fetchone()[0] == original_trigger_sql
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone() is None
    connection.close()

    backups = sorted(tmp_path.glob("legacy.sqlite3.*.bak"))
    assert len(backups) == 1
    backup_connection = sqlite3.connect(backups[0])
    assert backup_connection.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0] == 1
    assert backup_connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone() is None
    backup_connection.close()


def test_one_admin_index_accepts_keyword_case_and_structural_whitespace(tmp_path):
    database = tmp_path / "canonical-index-format.sqlite3"
    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        DrOp InDeX one_admin;
        CrEaTe UnIqUe InDeX one_admin
            ON users ( role )
            WhErE role = 'admin';
        """
    )
    accepted_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='one_admin'"
    ).fetchone()[0]
    connection.close()

    migrate_auth_schema(
        database,
        admin_username="admin",
        admin_password="correct horse battery staple",
    )

    assert "WhErE role = 'admin'" in accepted_sql
    assert sorted(tmp_path.glob("canonical-index-format.sqlite3.*.bak")) == []

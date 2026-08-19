from __future__ import annotations

import datetime
import sqlite3
import uuid
from pathlib import Path

from argon2 import PasswordHasher

from code_review.infrastructure.persistence.sqlite_schema import (
    REVIEW_SESSIONS_OWNER_CREATED_INDEX_SQL,
)


_SCHEMA_VERSION = 3


_EXPECTED_AUTH_COLUMNS = {
    "users": [
        ("user_id", "TEXT", 0, 1),
        ("username", "TEXT", 1, 0),
        ("password_hash", "TEXT", 1, 0),
        ("role", "TEXT", 1, 0),
        ("is_active", "INTEGER", 1, 0),
        ("must_change_password", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
        ("password_changed_at", "TEXT", 1, 0),
    ],
    "auth_sessions": [
        ("session_id", "TEXT", 0, 1),
        ("user_id", "TEXT", 1, 0),
        ("token_hash", "TEXT", 1, 0),
        ("csrf_hash", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("expires_at", "TEXT", 1, 0),
        ("last_seen_at", "TEXT", 1, 0),
    ],
    "auth_audit_log": [
        ("audit_id", "TEXT", 0, 1),
        ("actor_user_id", "TEXT", 0, 0),
        ("target_user_id", "TEXT", 0, 0),
        ("action", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("success", "INTEGER", 1, 0),
    ],
}
_CANONICAL_OWNER_COLUMN_SQL = (
    "owner_id TEXT NOT NULL REFERENCES users(user_id)"
)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _normalized_sql(sql: str) -> str:
    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            normalized.append(character)
            if character == quote:
                if (
                    quote != "]"
                    and index + 1 < len(sql)
                    and sql[index + 1] == quote
                ):
                    normalized.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in ("'", '"', "`"):
            quote = character
            normalized.append(character)
        elif character == "[":
            quote = "]"
            normalized.append(character)
        elif not character.isspace():
            normalized.append(character.casefold())
        index += 1
    return "".join(normalized).removesuffix(";")


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> list[tuple[str, str, int, int]]:
    return [
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        )
    ]


def _has_unique_index(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    collations: list[str] | None = None,
) -> bool:
    for row in connection.execute(
        f"PRAGMA index_list({_quote_identifier(table)})"
    ):
        if int(row[2]) != 1:
            continue
        index_name = str(row[1])
        key_rows = [
            entry
            for entry in connection.execute(
                f"PRAGMA index_xinfo({_quote_identifier(index_name)})"
            )
            if int(entry[5]) == 1
        ]
        if [str(entry[2]) for entry in key_rows] != columns:
            continue
        if collations is None or [
            str(entry[4]).upper() for entry in key_rows
        ] == collations:
            return True
    return False


def _auth_schema_is_current(connection: sqlite3.Connection) -> bool:
    try:
        table_sql = {
            table: _table_sql(connection, table)
            for table in _EXPECTED_AUTH_COLUMNS
        }
        if any(sql is None for sql in table_sql.values()):
            return False
        if any(
            _table_columns(connection, table) != expected
            for table, expected in _EXPECTED_AUTH_COLUMNS.items()
        ):
            return False

        users_sql = _normalized_sql(table_sql["users"] or "")
        if "usernametextnotnullcollatenocaseunique" not in users_sql:
            return False
        if "roletextnotnullcheck(rolein('admin','user'))" not in users_sql:
            return False
        if not _has_unique_index(
            connection, "users", ["username"], collations=["NOCASE"]
        ):
            return False

        sessions_sql = _normalized_sql(table_sql["auth_sessions"] or "")
        if "token_hashtextnotnullunique" not in sessions_sql:
            return False
        if not _has_unique_index(connection, "auth_sessions", ["token_hash"]):
            return False
        session_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(auth_sessions)"
        ).fetchall()
        if not any(
            row[2] == "users"
            and row[3] == "user_id"
            and row[4] == "user_id"
            and str(row[6]).upper() == "CASCADE"
            for row in session_foreign_keys
        ):
            return False

        one_admin = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='one_admin'"
        ).fetchone()
        if one_admin is None or one_admin[0] is None:
            return False
        index_rows = [
            row
            for row in connection.execute("PRAGMA index_list(users)")
            if str(row[1]) == "one_admin"
        ]
        return (
            len(index_rows) == 1
            and int(index_rows[0][2]) == 1
            and int(index_rows[0][4]) == 1
            and [
                str(row[2])
                for row in connection.execute("PRAGMA index_info(one_admin)")
            ]
            == ["role"]
            and _normalized_sql(str(one_admin[0]))
            == "createuniqueindexone_adminonusers(role)whererole='admin'"
        )
    except sqlite3.DatabaseError:
        return False


def _validate_auth_schema(connection: sqlite3.Connection) -> None:
    if not _auth_schema_is_current(connection):
        raise RuntimeError("auth schema is not canonical")


def _backup_database(database: Path) -> Path | None:
    if not database.exists():
        return None
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = database.with_name(f"{database.name}.{timestamp}.bak")
    source = sqlite3.connect(database)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup


def _migration_needed(database: Path) -> bool:
    if not database.exists():
        return True
    connection = sqlite3.connect(database)
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version > _SCHEMA_VERSION:
            raise RuntimeError("newer database schema version is not supported")
        if schema_version != _SCHEMA_VERSION or not _auth_schema_is_current(connection):
            return True
        try:
            admin_count = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            return True
        if admin_count != 1:
            return True
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='review_sessions'"
        ).fetchone()
        if table is None:
            return True
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(review_sessions)")
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(review_sessions)"
        ).fetchall()
        return not (
            columns.get("owner_id", (None, None, None, 0))[3] == 1
            and any(
                row[2] == "users"
                and row[3] == "owner_id"
                and row[4] == "user_id"
                for row in foreign_keys
            )
            and _owner_index_columns(connection) == ["owner_id", "created_at"]
        )
    finally:
        connection.close()


def _review_session_columns(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(connection.execute("PRAGMA table_xinfo(review_sessions)"))


def _review_sessions_has_final_owner_constraint(connection: sqlite3.Connection) -> bool:
    columns = {row[1]: row for row in _review_session_columns(connection)}
    foreign_keys = connection.execute("PRAGMA foreign_key_list(review_sessions)").fetchall()
    return columns.get("owner_id", (None, None, None, 0))[3] == 1 and any(
        row[2] == "users" and row[3] == "owner_id" and row[4] == "user_id" for row in foreign_keys
    )


def _owner_index_columns(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[2])
        for row in connection.execute(
            "PRAGMA index_info(idx_review_sessions_owner_created)"
        )
    ]


def _ensure_owner_index(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_review_sessions_owner_created")
    connection.execute(REVIEW_SESSIONS_OWNER_CREATED_INDEX_SQL)
    if _owner_index_columns(connection) != ["owner_id", "created_at"]:
        raise RuntimeError("review_sessions owner index was not created")


def _table_body_bounds(table_sql: str) -> tuple[int, int]:
    start = table_sql.find("(")
    if start < 0:
        raise RuntimeError("review_sessions has unsupported table SQL")
    depth = 0
    quote: str | None = None
    index = start
    while index < len(table_sql):
        character = table_sql[index]
        if quote is not None:
            if character == quote:
                if quote != "]" and index + 1 < len(table_sql) and table_sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif character in ("'", '"', "`"):
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return start, index
        index += 1
    raise RuntimeError("review_sessions has unbalanced table SQL")


def _split_table_definitions(body: str) -> list[str]:
    definitions: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(body):
        character = body[index]
        if quote is not None:
            if character == quote:
                if quote != "]" and index + 1 < len(body) and body[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif character in ("'", '"', "`"):
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            definitions.append(body[start:index].strip())
            start = index + 1
        index += 1
    definitions.append(body[start:].strip())
    if any(not definition for definition in definitions):
        raise RuntimeError("review_sessions has unsupported empty table definition")
    return definitions


def _definition_name(definition: str) -> str:
    stripped = definition.lstrip()
    if not stripped:
        return ""
    if stripped[0] in ('"', "`", "["):
        closing = "]" if stripped[0] == "[" else stripped[0]
        end = stripped.find(closing, 1)
        if end < 0:
            raise RuntimeError("review_sessions has unsupported quoted identifier")
        return stripped[1:end].replace(closing * 2, closing)
    return stripped.split(None, 1)[0]


def _review_sessions_new_table_sql(table_sql: str) -> str:
    body_start, body_end = _table_body_bounds(table_sql)
    definitions = _split_table_definitions(table_sql[body_start + 1 : body_end])
    owner_positions = [
        index
        for index, definition in enumerate(definitions)
        if _definition_name(definition).casefold() == "owner_id"
    ]
    if len(owner_positions) > 1:
        raise RuntimeError("review_sessions has multiple owner_id definitions")
    if owner_positions:
        definitions[owner_positions[0]] = _CANONICAL_OWNER_COLUMN_SQL
    else:
        table_constraint_prefixes = (
            "CONSTRAINT ",
            "PRIMARY ",
            "PRIMARY(",
            "UNIQUE ",
            "UNIQUE(",
            "CHECK ",
            "CHECK(",
            "FOREIGN ",
            "FOREIGN(",
        )
        insert_at = next(
            (
                index
                for index, definition in enumerate(definitions)
                if definition.lstrip().upper().startswith(table_constraint_prefixes)
            ),
            len(definitions),
        )
        definitions.insert(insert_at, _CANONICAL_OWNER_COLUMN_SQL)
    suffix = table_sql[body_end + 1 :]
    return f"CREATE TABLE review_sessions_new ({', '.join(definitions)}){suffix}"


def _rebuild_review_sessions(connection: sqlite3.Connection, admin_id: str) -> None:
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_sessions'"
    ).fetchone()
    if table is None:
        connection.execute("CREATE TABLE review_sessions (review_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id), status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT NOT NULL)")
        _ensure_owner_index(connection)
        return
    if _review_sessions_has_final_owner_constraint(connection):
        _ensure_owner_index(connection)
        return

    columns = _review_session_columns(connection)
    names = [
        str(row[1])
        for row in columns
        if row[1] != "owner_id" and (len(row) < 7 or row[6] == 0)
    ]
    if "review_id" not in names or "created_at" not in names:
        raise RuntimeError("review_sessions is missing required legacy columns")

    indexes = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('index', 'trigger') "
            "AND tbl_name = 'review_sessions' AND sql IS NOT NULL"
        )
        if str(row[0]) != "idx_review_sessions_owner_created"
    ]
    views = list(connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql LIKE '%review_sessions%'"))
    column_list = ", ".join(_quote_identifier(name) for name in names)
    owner_expression = "COALESCE(owner_id, ?)" if any(row[1] == "owner_id" for row in columns) else "?"
    original_count = connection.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0]

    connection.execute(
        _review_sessions_new_table_sql(str(table[0]))
    )
    connection.execute(
        f"INSERT INTO review_sessions_new ({column_list}, owner_id) "
        f"SELECT {column_list}, {owner_expression} FROM review_sessions",
        (admin_id,),
    )
    copied_count = connection.execute("SELECT COUNT(*) FROM review_sessions_new").fetchone()[0]
    if copied_count != original_count:
        raise RuntimeError("review session count changed during migration")
    for name, _sql in views:
        connection.execute(f"DROP VIEW {_quote_identifier(str(name))}")
    connection.execute("DROP TABLE review_sessions")
    connection.execute("ALTER TABLE review_sessions_new RENAME TO review_sessions")
    for _name, sql in indexes:
        connection.execute(sql)
    for _name, sql in views:
        connection.execute(str(sql))
    _ensure_owner_index(connection)


def _validate_migration(
    connection: sqlite3.Connection, expected_review_count: int, admin_id: str
) -> None:
    admin_count = connection.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    ).fetchone()[0]
    if admin_count != 1:
        raise RuntimeError("migration requires exactly one administrator")
    if connection.execute(
        "SELECT 1 FROM users WHERE user_id = ? AND role = 'admin'", (admin_id,)
    ).fetchone() is None:
        raise RuntimeError("migration administrator is missing")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_sessions'"
    ).fetchone()
    if table is not None:
        count = connection.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0]
        if count != expected_review_count:
            raise RuntimeError("review session count changed during migration")
        if connection.execute(
            "SELECT 1 FROM review_sessions WHERE owner_id IS NULL"
        ).fetchone() is not None:
            raise RuntimeError("review_sessions has an unowned review")
        if not _review_sessions_has_final_owner_constraint(connection):
            raise RuntimeError("review_sessions owner constraint was not created")
        if _owner_index_columns(connection) != ["owner_id", "created_at"]:
            raise RuntimeError("review_sessions owner index was not created")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError("foreign key check failed")


def _review_session_count(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_sessions'"
    ).fetchone()
    if table is None:
        return 0
    return int(connection.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0])


def migrate_auth_schema(database: Path, *, admin_username: str, admin_password: str) -> None:
    if len(admin_password) < 8:
        raise ValueError("password must contain at least 8 characters")
    migration_needed = _migration_needed(database)
    if not migration_needed:
        connection = sqlite3.connect(database)
        try:
            admin_id = str(
                connection.execute(
                    "SELECT user_id FROM users WHERE role='admin'"
                ).fetchone()[0]
            )
            _validate_migration(
                connection, _review_session_count(connection), admin_id
            )
        finally:
            connection.close()
        return
    _backup_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')), is_active INTEGER NOT NULL, must_change_password INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, password_changed_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_admin ON users(role) WHERE role='admin'"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS auth_sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS auth_audit_log (audit_id TEXT PRIMARY KEY, actor_user_id TEXT, target_user_id TEXT, action TEXT NOT NULL, created_at TEXT NOT NULL, success INTEGER NOT NULL)"
        )
        _validate_auth_schema(connection)
        row = connection.execute("SELECT user_id FROM users WHERE role='admin'").fetchone()
        if row is None:
            now = datetime.datetime.now(datetime.UTC).isoformat()
            admin_id = uuid.uuid4().hex
            connection.execute("INSERT INTO users VALUES (?, ?, ?, 'admin', 1, 1, ?, ?, ?)", (admin_id, admin_username, PasswordHasher().hash(admin_password), now, now, now))
        else:
            admin_id = str(row[0])
        review_count = _review_session_count(connection)
        _rebuild_review_sessions(connection, admin_id)
        _validate_migration(connection, review_count, admin_id)
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

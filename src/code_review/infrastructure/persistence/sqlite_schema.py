from __future__ import annotations

from pathlib import Path


REVIEW_SESSIONS_OWNER_CREATED_INDEX_SQL = (
    "CREATE INDEX idx_review_sessions_owner_created "
    "ON review_sessions(owner_id, created_at)"
)


def migrate_auth_schema(database: Path, *, admin_username: str, admin_password: str) -> None:
    from code_review.infrastructure.persistence.sqlite_auth_migration import (
        migrate_auth_schema as _migrate_auth_schema,
    )

    _migrate_auth_schema(
        database, admin_username=admin_username, admin_password=admin_password
    )


__all__ = ["REVIEW_SESSIONS_OWNER_CREATED_INDEX_SQL", "migrate_auth_schema"]

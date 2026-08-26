from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from code_review.domain.auth_models import AuthSession, AuthUser, UserPage, UserStats


class SQLiteAuthStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    async def get_user_by_username(self, username: str):
        row = self.connection.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            return None
        return self._user(row), str(row["password_hash"])

    async def get_user_by_id(self, user_id: str) -> AuthUser | None:
        row = self.connection.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return self._user(row)

    async def get_session(self, token: str):
        now = datetime.now(UTC)
        row = self.connection.execute(
            """SELECT s.*, u.username, u.role, u.is_active, u.must_change_password,
                      u.created_at AS user_created_at, u.updated_at,
                      u.password_changed_at
               FROM auth_sessions s JOIN users u ON u.user_id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1
            """,
            (self._hash(token), now.isoformat()),
        ).fetchone()
        if row is None:
            return None
        with self.connection:
            self.connection.execute(
                "UPDATE auth_sessions SET last_seen_at=? WHERE session_id=?",
                (now.isoformat(), row["session_id"]),
            )
        session = AuthSession(
            session_id=row["session_id"], user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            last_seen_at=now,
        )
        return session, self._user(row, created_key="user_created_at")

    async def create_session(self, user_id: str, token: str, csrf_token: str) -> AuthSession:
        now = datetime.now(UTC)
        expires = now + timedelta(hours=12)
        session_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO auth_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, self._hash(token), self._hash(csrf_token),
                 now.isoformat(), expires.isoformat(), now.isoformat()),
            )
        return AuthSession(
            session_id=session_id, user_id=user_id, created_at=now,
            expires_at=expires, last_seen_at=now,
        )

    async def create_single_session(
        self, user_id: str, token: str, csrf_token: str
    ) -> AuthSession:
        now = datetime.now(UTC)
        expires = now + timedelta(hours=12)
        session_id = uuid.uuid4().hex
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self.connection.execute(
                "INSERT INTO auth_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    user_id,
                    self._hash(token),
                    self._hash(csrf_token),
                    now.isoformat(),
                    expires.isoformat(),
                    now.isoformat(),
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return AuthSession(
            session_id=session_id,
            user_id=user_id,
            created_at=now,
            expires_at=expires,
            last_seen_at=now,
        )

    async def list_user_sessions(self, user_id: str) -> list[AuthSession]:
        now = datetime.now(UTC)
        with self.connection:
            self.connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at<=?", (now.isoformat(),)
            )
        rows = self.connection.execute(
            """SELECT session_id, user_id, created_at, expires_at, last_seen_at
               FROM auth_sessions WHERE user_id=?
               ORDER BY created_at DESC, session_id DESC""",
            (user_id,),
        ).fetchall()
        return [
            AuthSession(
                session_id=row["session_id"], user_id=row["user_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            )
            for row in rows
        ]
    async def revoke_user_sessions(self, user_id: str) -> None:
        self.connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        self.connection.commit()

    async def revoke_session(self, token: str) -> None:
        self.connection.execute(
            "DELETE FROM auth_sessions WHERE token_hash=?", (self._hash(token),)
        )
        self.connection.commit()

    async def verify_csrf(self, token: str, csrf_token: str):
        record = await self.get_session(token)
        if record is None:
            return None
        row = self.connection.execute(
            "SELECT csrf_hash FROM auth_sessions WHERE session_id=?",
            (record[0].session_id,),
        ).fetchone()
        return record if row is not None and row["csrf_hash"] == self._hash(csrf_token) else None

    async def create_user(self, username: str, password_hash: str) -> AuthUser:
        now = datetime.now(UTC)
        user_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO users VALUES (?, ?, ?, 'user', 1, 1, ?, ?, ?)",
            (user_id, username.strip(), password_hash, now.isoformat(),
             now.isoformat(), now.isoformat()),
        )
        self.connection.commit()
        record = await self.get_user_by_username(username)
        assert record is not None
        return record[0]

    async def list_users(
        self, query: str | None, status: str, page: int, page_size: int
    ) -> UserPage:
        if page < 1 or page_size not in {20, 50}:
            raise ValueError("invalid pagination")
        if status not in {"all", "active", "disabled"}:
            raise ValueError("invalid status")
        clauses: list[str] = []
        parameters: list[object] = []
        normalized_query = (query or "").strip()
        if normalized_query:
            clauses.append("username LIKE ? ESCAPE '\\'")
            escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped}%")
        if status != "all":
            clauses.append("is_active=?")
            parameters.append(1 if status == "active" else 0)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(self.connection.execute(
            f"SELECT COUNT(*) FROM users{where}", parameters
        ).fetchone()[0])
        rows = self.connection.execute(
            f"""SELECT * FROM users{where}
                 ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END,
                          username COLLATE NOCASE
                 LIMIT ? OFFSET ?""",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        return UserPage(
            items=[self._user(row) for row in rows], total=total,
            page=page, page_size=page_size,
        )

    async def user_stats(self) -> UserStats:
        row = self.connection.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END),0) AS active,
                      COALESCE(SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END),0) AS disabled
               FROM users"""
        ).fetchone()
        return UserStats(
            total=int(row["total"]),
            active=int(row["active"]),
            disabled=int(row["disabled"]),
        )

    async def set_user_active(self, user_id: str, active: bool) -> AuthUser:
        self.connection.execute(
            "UPDATE users SET is_active=?, updated_at=? WHERE user_id=? AND role='user'",
            (int(active), datetime.now(UTC).isoformat(), user_id),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None or row["role"] != "user":
            raise KeyError(user_id)
        if not active:
            await self.revoke_user_sessions(user_id)
        return self._user(row)

    async def set_password(self, user_id: str, password_hash: str, must_change: bool) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE users SET password_hash=?, must_change_password=?, updated_at=?,
                      password_changed_at=? WHERE user_id=?""",
            (password_hash, int(must_change), now, now, user_id),
        )
        self.connection.commit()

    async def reset_user_password(self, user_id: str, password_hash: str) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                """UPDATE users SET password_hash=?, must_change_password=1,
                          updated_at=?, password_changed_at=?
                   WHERE user_id=? AND role='user'""",
                (password_hash, now, now, user_id),
            )
            if cursor.rowcount != 1:
                existing = self.connection.execute(
                    "SELECT role FROM users WHERE user_id=?", (user_id,)
                ).fetchone()
                if existing is not None and existing["role"] == "admin":
                    raise PermissionError("the administrator account is protected")
                raise KeyError(user_id)
            self.connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    async def review_ids_for_owner(self, user_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT review_id FROM review_sessions WHERE owner_id=? ORDER BY review_id",
            (user_id,),
        ).fetchall()
        return [str(row["review_id"]) for row in rows]

    async def delete_user_record(self, user_id: str) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            user = self.connection.execute(
                "SELECT role FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if user is None:
                raise KeyError(user_id)
            if user["role"] == "admin":
                raise PermissionError("the administrator account is protected")
            model_runs_exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_runs'"
            ).fetchone()
            if model_runs_exists is not None:
                self.connection.execute(
                    """DELETE FROM model_runs WHERE review_id IN
                       (SELECT review_id FROM review_sessions WHERE owner_id=?)""",
                    (user_id,),
                )
            self.connection.execute("DELETE FROM review_sessions WHERE owner_id=?", (user_id,))
            self.connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            cursor = self.connection.execute(
                "DELETE FROM users WHERE user_id=? AND role='user'", (user_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(user_id)
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError("foreign key check failed")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _user(row: sqlite3.Row, created_key: str = "created_at") -> AuthUser:
        return AuthUser(
            user_id=row["user_id"], username=row["username"], role=row["role"],
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
            created_at=datetime.fromisoformat(row[created_key]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            password_changed_at=datetime.fromisoformat(row["password_changed_at"]),
        )

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from code_review.domain.auth_models import LoginResult
from code_review.domain.auth_ports import AuthStorePort


class InvalidCredentialsError(ValueError):
    pass


class DuplicateAdminError(ValueError):
    pass


class AuthService:
    def __init__(self, store: AuthStorePort) -> None:
        self._store = store
        self._hasher = PasswordHasher()

    @staticmethod
    def hash_password(password: str) -> str:
        if len(password) < 8:
            raise ValueError("password must contain at least 8 characters")
        return PasswordHasher().hash(password)

    async def login(self, username: str, password: str) -> LoginResult:
        record = await self._store.get_user_by_username(username)
        if record is None:
            raise InvalidCredentialsError("invalid username or password")
        user, password_hash = record
        try:
            valid = self._hasher.verify(password_hash, password)
        except VerifyMismatchError as error:
            raise InvalidCredentialsError("invalid username or password") from error
        if not valid or not user.is_active:
            raise InvalidCredentialsError("invalid username or password")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        session = await self._store.create_session(user.user_id, token, csrf)
        return LoginResult(
            user=user,
            session_id=session.session_id,
            session_token=token,
            csrf_token=csrf,
        )

    async def current_user(self, token: str):
        record = await self._store.get_session(token)
        return record[1] if record is not None else None

    async def active_sessions(self, token: str):
        record = await self._store.get_session(token)
        if record is None:
            raise PermissionError("authentication required")
        current, user = record
        sessions = await self._store.list_user_sessions(user.user_id)
        return current.session_id, sessions
    async def logout(self, token: str, csrf: str) -> None:
        if await self._store.verify_csrf(token, csrf) is None:
            raise PermissionError("invalid CSRF token")
        await self._store.revoke_session(token)

    async def logout_all(self, token: str, csrf: str) -> None:
        record = await self._store.verify_csrf(token, csrf)
        if record is None:
            raise PermissionError("invalid CSRF token")
        _, user = record
        await self._store.revoke_user_sessions(user.user_id)

    async def change_password(
        self, token: str, csrf: str, current_password: str, new_password: str
    ) -> None:
        record = await self._store.verify_csrf(token, csrf)
        if record is None:
            raise PermissionError("invalid CSRF token")
        _, user = record
        stored = await self._store.get_user_by_username(user.username)
        if stored is None or stored[0].user_id != user.user_id:
            raise InvalidCredentialsError("invalid username or password")
        try:
            current_password_matches = self._hasher.verify(stored[1], current_password)
        except VerifyMismatchError as error:
            raise InvalidCredentialsError("invalid username or password") from error
        if not current_password_matches:
            raise InvalidCredentialsError("invalid username or password")
        self._validate_new_password(current_password, new_password)
        await self._store.set_password(user.user_id, self.hash_password(new_password), False)
        await self._store.revoke_user_sessions(user.user_id)

    @staticmethod
    def _validate_new_password(current_password: str, new_password: str) -> None:
        if len(new_password) < 8:
            raise ValueError("password must contain at least 8 characters")
        if new_password == current_password:
            raise ValueError("new password must be different from current password")
        if new_password == "12345678":
            raise ValueError("new password must not be 12345678")

    async def create_user(self, username: str, password: str):
        return await self._store.create_user(username, self.hash_password(password))

    async def list_users(self):
        return await self._store.list_users()

    async def set_user_active(self, user_id: str, active: bool):
        return await self._store.set_user_active(user_id, active)

    async def reset_password(self, user_id: str, password: str) -> None:
        await self._store.set_password(user_id, self.hash_password(password), True)
        await self._store.revoke_user_sessions(user_id)

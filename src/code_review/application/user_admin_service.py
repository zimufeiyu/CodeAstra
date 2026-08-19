from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from code_review.domain.auth_models import BatchUserResult, UserPage, UserStats
from code_review.domain.auth_ports import AuthStorePort
from code_review.infrastructure.persistence.user_data_purger import UserDataPurger


class ReviewCancellationPort(Protocol):
    async def cancel(self, review_id: str, owner_id: str) -> bool: ...


class UserAdminService:
    TEMPORARY_PASSWORD = "12345678"

    def __init__(
        self,
        store: AuthStorePort,
        *,
        password_hasher: Callable[[str], str],
        review_service: ReviewCancellationPort | None = None,
        purger: UserDataPurger | None = None,
    ) -> None:
        self._store = store
        self._password_hasher = password_hasher
        self._review_service = review_service
        self._purger = purger

    async def create_user(self, username: str):
        return await self._store.create_user(
            username, self._password_hasher(self.TEMPORARY_PASSWORD)
        )

    async def list_users(
        self, query: str | None, status: str, page: int, page_size: int
    ) -> UserPage:
        return await self._store.list_users(query, status, page, page_size)

    async def user_stats(self) -> UserStats:
        return await self._store.user_stats()

    async def _ordinary_user(self, user_id: str):
        user = await self._store.get_user_by_id(user_id)
        if user is None:
            raise KeyError(user_id)
        if user.role == "admin":
            raise PermissionError("the administrator account is protected")
        return user

    async def set_user_active(self, user_id: str, active: bool):
        await self._ordinary_user(user_id)
        return await self._store.set_user_active(user_id, active)

    async def batch_set_active(
        self, user_ids: list[str], active: bool
    ) -> list[BatchUserResult]:
        results: list[BatchUserResult] = []
        for user_id in dict.fromkeys(user_ids):
            try:
                await self.set_user_active(user_id, active)
            except (KeyError, PermissionError) as error:
                results.append(BatchUserResult(user_id=user_id, ok=False, error=str(error)))
            else:
                results.append(BatchUserResult(user_id=user_id, ok=True))
        return results

    async def reset_password(self, user_id: str) -> None:
        await self._ordinary_user(user_id)
        await self._store.reset_user_password(
            user_id, self._password_hasher(self.TEMPORARY_PASSWORD)
        )

    async def delete_user(self, user_id: str) -> None:
        await self._ordinary_user(user_id)
        review_ids = await self._store.review_ids_for_owner(user_id)
        if self._review_service is not None:
            for review_id in review_ids:
                cancelled = await self._review_service.cancel(review_id, user_id)
                if not cancelled:
                    raise RuntimeError("could not cancel an owned review")
        ticket = self._purger.stage(user_id) if self._purger is not None else None
        try:
            await self._store.delete_user_record(user_id)
        except Exception:
            if ticket is not None:
                self._purger.restore(ticket)
            raise
        if ticket is not None:
            self._purger.finalize(ticket)

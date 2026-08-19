from sqlite3 import IntegrityError

from fastapi import APIRouter, HTTPException, Query, Request

from code_review.api.auth_schemas import BatchStatusRequest, UserCreateRequest
from code_review.application.auth_service import AuthService
from code_review.application.user_admin_service import UserAdminService


def create_admin_router(
    auth_service: AuthService, admin_service: UserAdminService | None = None
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["admin"])
    admin = admin_service or UserAdminService(
        auth_service._store, password_hasher=auth_service.hash_password
    )

    async def require_admin(request: Request, *, mutation: bool = False):
        token = request.cookies.get("session", "")
        user = await auth_service.current_user(token)
        if user is None or user.role != "admin":
            raise HTTPException(403, "administrator authentication required")
        if mutation and await auth_service._store.verify_csrf(
            token, request.headers.get("X-CSRF-Token", "")
        ) is None:
            raise HTTPException(403, "invalid CSRF token")
        return user

    @router.get("/users")
    async def users(
        request: Request,
        q: str | None = None,
        status: str = Query("all", pattern="^(all|active|disabled)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20),
    ):
        await require_admin(request)
        try:
            result = await admin.list_users(q, status, page, page_size)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {
            "items": [_public_user(user) for user in result.items],
            "total": result.total, "page": result.page, "page_size": result.page_size,
        }

    @router.get("/users/stats")
    async def user_stats(request: Request):
        await require_admin(request)
        stats = await admin.user_stats()
        return {"total": stats.total, "active": stats.active, "disabled": stats.disabled}

    @router.post("/users", status_code=201)
    async def create_user(body: UserCreateRequest, request: Request):
        await require_admin(request, mutation=True)
        try:
            user = await admin.create_user(body.username)
        except IntegrityError as error:
            raise HTTPException(409, "username already exists") from error
        return {**_public_user(user), "temporary_password": admin.TEMPORARY_PASSWORD}

    @router.post("/users/batch-status")
    async def batch_status(body: BatchStatusRequest, request: Request):
        await require_admin(request, mutation=True)
        results = await admin.batch_set_active(body.user_ids, body.active)
        return {"results": [result.__dict__ for result in results]}

    async def mutate_user(user_id: str, active: bool, request: Request):
        await require_admin(request, mutation=True)
        try:
            user = await admin.set_user_active(user_id, active)
        except KeyError as error:
            raise HTTPException(404, "user not found") from error
        except PermissionError as error:
            raise HTTPException(409, str(error)) from error
        return _public_user(user)

    @router.post("/users/{user_id}/enable")
    async def enable_user(user_id: str, request: Request):
        return await mutate_user(user_id, True, request)

    @router.post("/users/{user_id}/disable")
    async def disable_user(user_id: str, request: Request):
        return await mutate_user(user_id, False, request)

    @router.post("/users/{user_id}/reset-password")
    async def reset_password(user_id: str, request: Request):
        await require_admin(request, mutation=True)
        try:
            await admin.reset_password(user_id)
        except KeyError as error:
            raise HTTPException(404, "user not found") from error
        except PermissionError as error:
            raise HTTPException(409, str(error)) from error
        return {"temporary_password": admin.TEMPORARY_PASSWORD}

    @router.delete("/users/{user_id}")
    async def delete_user(user_id: str, request: Request):
        await require_admin(request, mutation=True)
        try:
            await admin.delete_user(user_id)
        except KeyError as error:
            raise HTTPException(404, "user not found") from error
        except PermissionError as error:
            raise HTTPException(409, str(error)) from error
        return {"ok": True}

    return router


def _public_user(user) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
    }

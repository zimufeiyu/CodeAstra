from fastapi import APIRouter, HTTPException, Request, Response

from code_review.api.auth_schemas import LoginRequest, PasswordChangeRequest
from code_review.application.auth_service import AuthService, InvalidCredentialsError


def _detail(error_code: str, message: str) -> dict[str, str]:
    return {"error_code": error_code, "message": message}


def create_auth_router(service: AuthService) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["auth"])

    async def authenticated(request: Request):
        token = request.cookies.get("session")
        user = await service.current_user(token or "")
        if user is None:
            raise HTTPException(
                401,
                _detail(
                    "session_expired",
                    "账号已在其他设备登录或会话已过期，请重新登录。",
                ),
            )
        return token, user

    @router.post("/login")
    async def login(body: LoginRequest, response: Response):
        try:
            result = await service.login(body.username, body.password)
        except InvalidCredentialsError as error:
            raise HTTPException(
                401,
                _detail("invalid_credentials", "用户名或密码错误。"),
            ) from error
        response.set_cookie(
            "session", result.session_token, httponly=True, samesite="strict",
            max_age=43200, path="/",
        )
        response.set_cookie(
            "csrf", result.csrf_token, httponly=False, samesite="strict",
            max_age=43200, path="/",
        )
        return _public_user(result.user, result.csrf_token)

    @router.get("/me")
    async def me(request: Request):
        identity = await authenticated(request)
        token, user = identity
        csrf_token = request.cookies.get("csrf", "")
        if await service._store.verify_csrf(token, csrf_token) is None:
            raise HTTPException(
                403,
                _detail("csrf_invalid", "页面安全令牌已过期，请刷新后重试。"),
            )
        return _public_user(user, csrf_token)

    @router.get("/sessions")
    async def sessions(request: Request):
        token, _ = await authenticated(request)
        try:
            current_session_id, active = await service.active_sessions(token)
        except PermissionError as error:
            raise HTTPException(
                401,
                _detail("session_expired", "会话已过期，请重新登录。"),
            ) from error
        return {
            "total": len(active),
            "items": [
                {
                    "session_id": item.session_id,
                    "current": item.session_id == current_session_id,
                    "created_at": item.created_at,
                    "last_seen_at": item.last_seen_at,
                    "expires_at": item.expires_at,
                }
                for item in active
            ],
        }
    def clear_auth_cookies(response: Response) -> None:
        response.delete_cookie("session", path="/")
        response.delete_cookie("csrf", path="/")

    @router.post("/logout")
    async def logout(request: Request, response: Response):
        identity = await authenticated(request)
        token, _ = identity
        try:
            await service.logout(token, request.headers.get("X-CSRF-Token", ""))
        except PermissionError as error:
            raise HTTPException(
                403,
                _detail("csrf_invalid", "页面安全令牌已过期，请刷新后重试。"),
            ) from error
        clear_auth_cookies(response)
        return {"ok": True}

    @router.post("/logout-all")
    async def logout_all(request: Request, response: Response):
        identity = await authenticated(request)
        token, _ = identity
        try:
            await service.logout_all(token, request.headers.get("X-CSRF-Token", ""))
        except PermissionError as error:
            raise HTTPException(
                403,
                _detail("csrf_invalid", "页面安全令牌已过期，请刷新后重试。"),
            ) from error
        clear_auth_cookies(response)
        return {"ok": True}

    @router.post("/change-password")
    async def change_password(
        body: PasswordChangeRequest, request: Request, response: Response
    ):
        identity = await authenticated(request)
        token, _ = identity
        try:
            await service.change_password(
                token,
                request.headers.get("X-CSRF-Token", ""),
                body.current_password,
                body.new_password,
            )
        except (InvalidCredentialsError, PermissionError) as error:
            raise HTTPException(
                403,
                _detail("password_or_csrf_invalid", "当前密码错误或页面安全令牌已过期。"),
            ) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        clear_auth_cookies(response)
        return {"ok": True}

    return router


def _public_user(user, csrf_token: str | None = None) -> dict[str, object]:
    data: dict[str, object] = {
        "user_id": user.user_id, "username": user.username, "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "password_changed_at": user.password_changed_at,
    }
    if csrf_token is not None:
        data["csrf_token"] = csrf_token
    return data

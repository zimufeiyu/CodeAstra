from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AuthUser(BaseModel):
    user_id: str
    username: str
    role: Literal["admin", "user"]
    is_active: bool = True
    must_change_password: bool = False
    created_at: datetime
    updated_at: datetime
    password_changed_at: datetime


class AuthSession(BaseModel):
    session_id: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime


class LoginResult(BaseModel):
    user: AuthUser
    session_id: str
    session_token: str = Field(repr=False)
    csrf_token: str = Field(repr=False)


@dataclass(frozen=True)
class UserPage:
    items: list[AuthUser]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class UserStats:
    total: int
    active: int
    disabled: int


@dataclass(frozen=True)
class BatchUserResult:
    user_id: str
    ok: bool
    error: str | None = None

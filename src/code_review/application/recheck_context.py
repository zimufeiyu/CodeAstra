from __future__ import annotations

from contextvars import ContextVar

current_recheck_attempt: ContextVar[str | None] = ContextVar(
    "current_recheck_attempt", default=None
)

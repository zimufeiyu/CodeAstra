from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic


class LoginRateLimiter:
    def __init__(self, attempts: int = 5, window_seconds: int = 300) -> None:
        self._attempts = attempts
        self._window = window_seconds
        self._failed: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allowed(self, username: str, source: str) -> bool:
        values = self._failed[(username.casefold(), source)]
        now = monotonic()
        while values and values[0] <= now - self._window:
            values.popleft()
        return len(values) < self._attempts

    def failed(self, username: str, source: str) -> None:
        self._failed[(username.casefold(), source)].append(monotonic())

    def succeeded(self, username: str, source: str) -> None:
        self._failed.pop((username.casefold(), source), None)

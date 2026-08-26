from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from code_review.api import main
from code_review.api.body_limit import RequestBodyLimitMiddleware
from code_review.domain.review_models import ReviewSession


@dataclass
class _User:
    user_id: str
    must_change_password: bool = False


class _AuthStore:
    async def verify_csrf(self, token: str, csrf: str) -> str | None:
        return token if token == "audit-user" and csrf == "csrf" else None


class _AuthService:
    def __init__(self) -> None:
        self._store = _AuthStore()

    async def current_user(self, token: str) -> _User | None:
        return _User(token) if token == "audit-user" else None


class _CaptureReviewService:
    def __init__(self) -> None:
        self.create_calls = 0
        self.files = []

    async def create(self, mode, files, *, owner_id, **kwargs):
        self.create_calls += 1
        self.files = list(files)
        return ReviewSession.create(
            review_id="audit-review",
            owner_id=owner_id,
            mode=mode,
            files=files,
        )

    async def start(self, review_id: str, owner_id: str) -> None:
        return None


@pytest.fixture
def api(monkeypatch):
    service = _CaptureReviewService()
    monkeypatch.setattr(main, "get_auth_service", lambda: _AuthService())
    app = main.create_app()
    app.dependency_overrides[main.get_hybrid_review_service] = lambda: service
    return app, service


async def _post_project(app, files):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": "audit-user"},
        headers={"X-CSRF-Token": "csrf"},
    ) as client:
        return await client.post("/v1/reviews/project", json={"files": files})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("pkg/item.py", "pkg/item.py"),
        ("Pkg/Item.py", "pkg/item.py"),
        ("pkg\\item.py", "pkg/item.py"),
        ("pkg/cafe\u0301.py", "pkg/caf\u00e9.py"),
        ("pkg//item.py", "pkg/item.py"),
    ],
)
async def test_duplicate_canonical_paths_are_rejected_before_service(api, first, second):
    app, service = api
    response = await _post_project(
        app,
        [
            {"filename": first, "content": "print('first')\n"},
            {"filename": second, "content": "print('second')\n"},
        ],
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "duplicate_source_path"
    assert response.json()["detail"]["paths"]
    assert service.create_calls == 0


@pytest.mark.asyncio
async def test_distinct_paths_are_normalized_and_started_once(api):
    app, service = api
    response = await _post_project(
        app,
        [
            {"filename": "pkg\\first.py", "content": "print('first')\n"},
            {"filename": "pkg/second.py", "content": "print('second')\n"},
        ],
    )

    assert response.status_code == 202
    assert service.create_calls == 1
    assert [item.relative_path for item in service.files] == ["pkg/first.py", "pkg/second.py"]


@pytest.mark.asyncio
async def test_duplicate_local_diff_base_paths_are_rejected_before_service(api):
    app, service = api
    transport = httpx.ASGITransport(app=app)
    payload = {
        "files": [{"filename": "pkg/item.py", "content": "print('new')\n"}],
        "local_diff_base_files": [
            {"filename": "pkg/item.py", "content": "print('old')\n"},
            {"filename": "PKG\\ITEM.py", "content": "print('older')\n"},
        ],
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": "audit-user"},
        headers={"X-CSRF-Token": "csrf"},
    ) as client:
        response = await client.post("/v1/reviews/project", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "duplicate_source_path"
    assert service.create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["print('ok')\x00", "print('ok')\ufffd", "print('ok')\x01\x02"],
)
async def test_invalid_source_text_is_rejected_before_service(api, content):
    app, service = api
    response = await _post_project(app, [{"filename": "bad.py", "content": content}])

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error_code"] == "invalid_source_text"
    assert detail["path"] == "bad.py"
    assert service.create_calls == 0


@pytest.mark.asyncio
async def test_small_unauthenticated_request_keeps_authentication_error(api):
    app, _ = api
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/reviews/project", json={"files": []})
    assert response.status_code == 401


async def _run_raw_middleware(messages, *, content_length: bytes | None = None):
    index = 0
    sent = []

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        nonlocal index
        message = messages[index]
        index += 1
        return message

    async def send(message):
        sent.append(message)

    headers = [] if content_length is None else [(b"content-length", content_length)]
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=10)
    await middleware(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_body_limit_rejects_declared_length_before_receive():
    sent = await _run_raw_middleware([], content_length=b"11")
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_limit_counts_streamed_chunks_without_content_length():
    sent = await _run_raw_middleware(
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"78901", "more_body": False},
        ]
    )
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_limit_allows_exact_streamed_boundary():
    sent = await _run_raw_middleware(
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": False},
        ]
    )
    assert sent[0]["status"] == 204


def test_transport_limit_covers_json_encoding_overhead():
    assert main._MAX_REVIEW_BODY_BYTES >= 2 * main._MAX_REVIEW_SOURCE_BYTES + 1024 * 1024

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from code_review.domain.review_models import ReviewEvent, ReviewMode, ReviewSession, SourceFile


main = pytest.importorskip("code_review.api.main")


@dataclass
class _User:
    user_id: str
    must_change_password: bool = False


class _AuthStore:
    async def verify_csrf(self, token: str, csrf: str) -> str | None:
        return token if csrf == "csrf" else None


class _AuthService:
    def __init__(self) -> None:
        self._store = _AuthStore()

    async def current_user(self, token: str) -> _User | None:
        return _User(token) if token in {"alice-id", "bob-id"} else None


class _ReviewService:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.cancel_result = True
        self.event_requests: list[tuple[str, str, int]] = []
        self.bob_review = ReviewSession.create(
            review_id="bob-review",
            owner_id="bob-id",
            mode=ReviewMode.PASTE,
            files=[
                SourceFile.from_content(
                    file_id="bob-file",
                    relative_path="bob.py",
                    language="python",
                    content="pass\n",
                )
            ],
        )

    async def create(self, mode, files, *, owner_id, **kwargs):
        return ReviewSession.create(
            review_id="alice-created",
            owner_id=owner_id,
            mode=mode,
            files=files,
        )

    async def start(self, review_id: str, owner_id: str) -> None:
        self.started.append((review_id, owner_id))

    async def get(self, review_id: str, owner_id: str):
        if (review_id, owner_id) == ("bob-review", "bob-id"):
            return self.bob_review
        return None

    async def rename(self, review_id, owner_id, title):
        raise KeyError(review_id)

    async def delete(self, review_id, owner_id):
        return False

    async def revisions(self, review_id, owner_id):
        raise KeyError(review_id)

    async def followups(self, review_id, owner_id):
        raise KeyError(review_id)

    async def cancel(self, review_id, owner_id):
        self.cancelled.append((review_id, owner_id))
        return self.cancel_result and await self.get(review_id, owner_id) is not None

    async def events(self, review_id, owner_id, after_sequence=0):
        if await self.get(review_id, owner_id) is None:
            return
        self.event_requests.append((review_id, owner_id, after_sequence))
        yield ReviewEvent(sequence=1, event="stage", data={"review_id": review_id, "message": "owned"})


@pytest_asyncio.fixture
async def authenticated_client(monkeypatch):
    auth_service = _AuthService()
    review_service = _ReviewService()
    monkeypatch.setattr(main, "get_auth_service", lambda: auth_service)
    app = main.create_app()
    app.dependency_overrides[main.get_hybrid_review_service] = lambda: review_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": "alice-id"},
    ) as client:
        yield client, review_service


@pytest.mark.asyncio
async def test_create_review_starts_the_review_for_the_authenticated_owner(authenticated_client):
    client, service = authenticated_client

    response = await client.post(
        "/v1/reviews/paste",
        json={
            "filename": "alice.py",
            "language": "python",
            "content": "def review():\n    pass\n",
            "model_profile_id": "local-qwen3-8b",
        },
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 202, response.text
    assert service.started == [("alice-created", "alice-id")]


@pytest.mark.asyncio
async def test_alice_cannot_open_bobs_review_event_stream(authenticated_client):
    client, _service = authenticated_client

    async with client.stream("GET", "/v1/reviews/bob-review/events") as response:
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_owner_cancel_invokes_service_and_returns_its_real_result(authenticated_client):
    client, service = authenticated_client
    client.cookies.set("session", "bob-id")

    response = await client.post(
        "/v1/reviews/bob-review/cancel",
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200
    assert response.json() == {"review_id": "bob-review", "status": "cancelled"}
    assert service.cancelled == [("bob-review", "bob-id")]


@pytest.mark.asyncio
async def test_owner_cancel_returns_not_found_when_service_cannot_cancel(authenticated_client):
    client, service = authenticated_client
    client.cookies.set('session', 'bob-id')
    service.cancel_result = False

    response = await client.post(
        '/v1/reviews/bob-review/cancel',
        headers={'X-CSRF-Token': 'csrf'},
    )

    assert response.status_code == 404
    assert service.cancelled == [('bob-review', 'bob-id')]


@pytest.mark.asyncio
async def test_authorized_sse_contains_only_callers_event_payload(authenticated_client):
    client, service = authenticated_client
    client.cookies.set("session", "bob-id")

    async with client.stream("GET", "/v1/reviews/bob-review/events") as response:
        payload = (await response.aread()).decode()

    assert response.status_code == 200
    assert "bob-review" in payload
    assert "owner_id" not in payload
    assert "alice-id" not in payload
    assert service.event_requests == [("bob-review", "bob-id", 0)]


@pytest.mark.asyncio
async def test_alice_receives_404_for_every_bob_review_http_operation(authenticated_client):
    client, service = authenticated_client
    requests = (
        ("GET", "/v1/reviews/bob-review", {}),
        ("PATCH", "/v1/reviews/bob-review", {"json": {"title": "stolen"}, "headers": {"X-CSRF-Token": "csrf"}}),
        ("DELETE", "/v1/reviews/bob-review", {"headers": {"X-CSRF-Token": "csrf"}}),
        ("POST", "/v1/reviews/bob-review/findings/bob-finding/decision", {"json": {"decision": "keep"}, "headers": {"X-CSRF-Token": "csrf"}}),
        ("GET", "/v1/reviews/bob-review/revisions", {}),
        ("POST", "/v1/reviews/bob-review/revisions/bob-revision/undo", {"headers": {"X-CSRF-Token": "csrf"}}),
        ("GET", "/v1/reviews/bob-review/report", {}),
        ("GET", "/v1/reviews/bob-review/files/bob-file", {}),
        ("GET", "/v1/reviews/bob-review/findings/bob-finding", {}),
        ("GET", "/v1/reviews/bob-review/followups", {}),
        ("POST", "/v1/reviews/bob-review/followups", {"json": {"question": "stolen?"}, "headers": {"X-CSRF-Token": "csrf"}}),
        ("POST", "/v1/reviews/bob-review/cancel", {"headers": {"X-CSRF-Token": "csrf"}}),
        ("POST", "/v1/reviews/bob-review/resume", {"headers": {"X-CSRF-Token": "csrf"}}),
        ("GET", "/v1/reviews/bob-review/events", {}),
    )

    for method, path, kwargs in requests:
        response = await client.request(method, path, **kwargs)
        assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text}"

    assert service.started == []

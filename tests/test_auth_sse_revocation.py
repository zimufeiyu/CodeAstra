from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from code_review.application.inference_service import FixCandidateError
from code_review.domain.review_models import (
    FixCandidate,
    FollowupMessage,
    ReviewEvent,
    ReviewMode,
    ReviewSession,
    SourceFile,
)

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
        self.preview_error: FixCandidateError | None = None
        self.intent_requests = []
        self.followup_fix_requests = []
        self.followup_answer_requests = []
        self.reopen_requests = []
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

    async def record_finding_decision(self, review_id, owner_id, finding_id, decision):
        raise KeyError(review_id)

    async def reopen_finding(self, review_id, owner_id, finding_id):
        if (review_id, owner_id) != ("bob-review", "bob-id"):
            raise KeyError(review_id)
        self.reopen_requests.append((review_id, owner_id, finding_id))
        return self.bob_review, False, False

    async def preview_fix(self, review_id, owner_id, finding_id, intent=None):
        if self.preview_error is not None:
            raise self.preview_error
        if intent is not None:
            self.intent_requests.append((review_id, owner_id, finding_id, intent))
        raise KeyError(review_id)

    async def confirm_fix(self, review_id, owner_id, candidate_id):
        raise KeyError(review_id)

    async def preview_followup_fix(
        self, review_id, owner_id, instruction, base_sha, context
    ):
        if await self.get(review_id, owner_id) is None:
            raise KeyError(review_id)
        self.followup_fix_requests.append(
            (review_id, owner_id, instruction, base_sha, context)
        )
        now = datetime.now(tz=UTC)
        return FixCandidate(
            candidate_id="followup-candidate",
            review_id=review_id,
            finding_id=context["finding_id"],
            file_id=context["file_id"],
            relative_path="bob.py",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
            base_sha256=base_sha,
            after_sha256="b" * 64,
            diff="--- a/bob.py\n+++ b/bob.py\n@@ -1 +1 @@\n-pass\n+value = 1\n",
            explanation=f"按追问指令生成候选：{instruction}",
            validation=["Python 语法解析通过"],
            output_token_budget=256,
        )

    async def cancel_fix(self, review_id, owner_id, candidate_id):
        raise KeyError(review_id)

    async def followups(self, review_id, owner_id):
        raise KeyError(review_id)

    async def ask_followup(self, review_id, owner_id, question, context):
        if await self.get(review_id, owner_id) is None:
            raise KeyError(review_id)
        self.followup_answer_requests.append((review_id, owner_id, question, context))
        return [
            FollowupMessage(
                message_id="answer-1",
                review_id=review_id,
                role="assistant",
                content="这是解释回答。",
                context_key="finding:bob-file:bob-finding",
                created_at=datetime.now(tz=UTC),
            )
        ]

    async def cancel(self, review_id, owner_id):
        self.cancelled.append((review_id, owner_id))
        return self.cancel_result and await self.get(review_id, owner_id) is not None

    async def events(self, review_id, owner_id, after_sequence=0):
        if await self.get(review_id, owner_id) is None:
            return
        self.event_requests.append((review_id, owner_id, after_sequence))
        yield ReviewEvent(
            sequence=1, event="stage", data={"review_id": review_id, "message": "owned"}
        )


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
async def test_create_review_rejects_oversized_source_before_starting_analysis(
    authenticated_client,
):
    client, service = authenticated_client

    response = await client.post(
        "/v1/reviews/paste",
        json={
            "filename": "alice.py",
            "language": "python",
            "content": "#" + ("x" * (2 * 1024 * 1024)),
            "model_profile_id": "local-qwen3-8b",
        },
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "单个代码文件不能超过 2 MiB"
    assert service.started == []


@pytest.mark.asyncio
async def test_create_project_rejects_oversized_aggregate_before_starting_analysis(
    authenticated_client,
):
    client, service = authenticated_client
    content = "value = 1\n#" + ("x" * (2 * 1024 * 1024 - 20))

    response = await client.post(
        "/v1/reviews/project",
        json={
            "files": [
                {"filename": f"pkg/file_{index}.py", "language": "python", "content": content}
                for index in range(5)
            ],
            "model_profile_id": "local-qwen3-8b",
        },
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "一次审查的代码总量不能超过 8 MiB"
    assert service.started == []


@pytest.mark.asyncio
async def test_openapi_hides_legacy_review_and_unused_deployment_routes(authenticated_client):
    client, _service = authenticated_client
    schema = (await client.get("/openapi.json")).json()
    assert "/v1/reviews/paste" in schema["paths"]
    assert "/v1/review" not in schema["paths"]
    assert not any(path.startswith("/v1/deployment") for path in schema["paths"])


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
    client.cookies.set("session", "bob-id")
    service.cancel_result = False

    response = await client.post(
        "/v1/reviews/bob-review/cancel",
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 404
    assert service.cancelled == [("bob-review", "bob-id")]


@pytest.mark.asyncio
async def test_fix_preview_preserves_needs_intent_contract_and_requires_csrf(
    authenticated_client,
):
    client, service = authenticated_client
    service.preview_error = FixCandidateError(
        "needs_intent",
        "已定位症状，但无法唯一确定开发者意图。",
        details={"base_sha": "a" * 64, "use_def_evidence": {"options": []}},
    )
    without_csrf = await client.post("/v1/reviews/bob-review/findings/bob-finding/fix-preview")
    assert without_csrf.status_code == 403
    client.cookies.set("session", "bob-id")
    response = await client.post(
        "/v1/reviews/bob-review/findings/bob-finding/fix-preview",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "needs_intent",
        "code": "needs_intent",
        "message": "已定位症状，但无法唯一确定开发者意图。",
        "context": {"base_sha": "a" * 64, "use_def_evidence": {"options": []}},
        "base_sha": "a" * 64,
        "use_def_evidence": {"options": []},
    }


@pytest.mark.asyncio
async def test_followup_fix_preview_requires_csrf_owner_and_preserves_payload(
    authenticated_client,
):
    client, service = authenticated_client
    payload = {
        "instruction": "把缺失值按空字典处理",
        "base_sha": "a" * 64,
        "context": {
            "kind": "finding",
            "file_id": "bob-file",
            "finding_id": "bob-finding",
        },
    }

    missing_csrf = await client.post(
        "/v1/reviews/bob-review/followups/fix-preview", json=payload
    )
    assert missing_csrf.status_code == 403
    forged_owner = await client.post(
        "/v1/reviews/bob-review/followups/fix-preview",
        json=payload,
        headers={"X-CSRF-Token": "csrf"},
    )
    assert forged_owner.status_code == 404
    client.cookies.set("session", "bob-id")
    response = await client.post(
        "/v1/reviews/bob-review/followups/fix-preview",
        json=payload,
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["phase"] == "awaiting_confirmation"
    assert response.json()["candidate"]["candidate_id"] == "followup-candidate"
    assert service.followup_fix_requests == [
        (
            "bob-review",
            "bob-id",
            payload["instruction"],
            payload["base_sha"],
            {
                **payload["context"],
                "start_line": None,
                "end_line": None,
                "selected_code": None,
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit_prompt",
    ["删除这段判断", "把这里改成提前返回", "增加空值检查"],
)
async def test_followup_post_routes_edit_to_candidate_and_question_to_answer(
    authenticated_client,
    edit_prompt,
):
    client, service = authenticated_client
    client.cookies.set("session", "bob-id")
    context = {
        "kind": "finding",
        "file_id": "bob-file",
        "finding_id": "bob-finding",
    }
    edit = await client.post(
        "/v1/reviews/bob-review/followups",
        json={
            "question": edit_prompt,
            "base_sha": "a" * 64,
            "context": context,
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["action"] == "fix_candidate"
    assert edit.json()["candidate"]["candidate_id"] == "followup-candidate"
    assert service.followup_answer_requests == []

    answer = await client.post(
        "/v1/reviews/bob-review/followups",
        json={"question": "为什么要删除这段代码？", "context": context},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["action"] == "answer"
    assert answer.json()["messages"][0]["content"] == "这是解释回答。"
    assert len(service.followup_fix_requests) == 1


@pytest.mark.asyncio
async def test_selection_edit_command_is_fail_closed_and_preserves_structured_error(
    authenticated_client,
):
    client, service = authenticated_client
    client.cookies.set("session", "bob-id")
    response = await client.post(
        "/v1/reviews/bob-review/followups",
        json={
            "question": "删除第 1 行代码",
            "base_sha": "a" * 64,
            "context": {"kind": "selection", "file_id": "bob-file"},
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "scope_mismatch"
    assert "请从已验证的审查问题发起修改" in response.json()["detail"]["message"]
    assert service.followup_fix_requests == []
    assert service.followup_answer_requests == []


@pytest.mark.asyncio
async def test_custom_intent_api_preserves_bounded_user_intent_and_csrf(authenticated_client):
    client, service = authenticated_client
    client.cookies.set("session", "bob-id")
    user_intent = "缺少值时使用当前用户的默认配置"
    response = await client.post(
        "/v1/reviews/bob-review/findings/bob-finding/fix-preview/intent",
        json={
            "review_id": "bob-review",
            "finding_id": "bob-finding",
            "base_sha": "a" * 64,
            "option_id": "custom_behavior",
            "intent_kind": "custom_behavior",
            "user_intent": user_intent,
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 404
    assert len(service.intent_requests) == 1
    request = service.intent_requests[0][3]
    assert request.intent_kind == "custom_behavior"
    assert request.user_intent == user_intent
    assert request.initializer is None


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
async def test_reopen_endpoint_is_csrf_protected_and_owner_scoped(authenticated_client):
    client, service = authenticated_client
    client.cookies.set("session", "bob-id")

    rejected = await client.post(
        "/v1/reviews/bob-review/findings/bob-finding/reopen"
    )
    accepted = await client.post(
        "/v1/reviews/bob-review/findings/bob-finding/reopen",
        headers={"X-CSRF-Token": "csrf"},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["revision_retained"] is False
    assert service.reopen_requests == [("bob-review", "bob-id", "bob-finding")]


@pytest.mark.asyncio
async def test_alice_receives_404_for_every_bob_review_http_operation(authenticated_client):
    client, service = authenticated_client
    requests = (
        ("GET", "/v1/reviews/bob-review", {}),
        (
            "PATCH",
            "/v1/reviews/bob-review",
            {"json": {"title": "stolen"}, "headers": {"X-CSRF-Token": "csrf"}},
        ),
        ("DELETE", "/v1/reviews/bob-review", {"headers": {"X-CSRF-Token": "csrf"}}),
        (
            "POST",
            "/v1/reviews/bob-review/findings/bob-finding/decision",
            {"json": {"decision": "deferred"}, "headers": {"X-CSRF-Token": "csrf"}},
        ),
        (
            "POST",
            "/v1/reviews/bob-review/findings/bob-finding/reopen",
            {"headers": {"X-CSRF-Token": "csrf"}},
        ),
        (
            "POST",
            "/v1/reviews/bob-review/findings/bob-finding/fix-preview",
            {"headers": {"X-CSRF-Token": "csrf"}},
        ),
        (
            "POST",
            "/v1/reviews/bob-review/findings/bob-finding/fix-preview/intent",
            {
                "json": {
                    "review_id": "bob-review",
                    "finding_id": "bob-finding",
                    "base_sha": "a" * 64,
                    "option_id": "declare_local",
                    "intent_kind": "declare_local",
                    "initializer": "1",
                },
                "headers": {"X-CSRF-Token": "csrf"},
            },
        ),
        (
            "POST",
            "/v1/reviews/bob-review/fix-candidates/confirm",
            {"json": {"candidate_id": "fix-1"}, "headers": {"X-CSRF-Token": "csrf"}},
        ),
        (
            "DELETE",
            "/v1/reviews/bob-review/fix-candidates/fix-1",
            {"headers": {"X-CSRF-Token": "csrf"}},
        ),
        ("GET", "/v1/reviews/bob-review/revisions", {}),
        (
            "POST",
            "/v1/reviews/bob-review/revisions/bob-revision/undo",
            {"headers": {"X-CSRF-Token": "csrf"}},
        ),
        ("GET", "/v1/reviews/bob-review/report", {}),
        ("GET", "/v1/reviews/bob-review/fixes.patch", {}),
        ("GET", "/v1/reviews/bob-review/fixed-files.zip", {}),
        ("GET", "/v1/reviews/bob-review/fixed-files/bob-file", {}),
        ("GET", "/v1/reviews/bob-review/files/bob-file", {}),
        ("GET", "/v1/reviews/bob-review/findings/bob-finding", {}),
        ("GET", "/v1/reviews/bob-review/followups", {}),
        (
            "POST",
            "/v1/reviews/bob-review/followups",
            {"json": {"question": "stolen?"}, "headers": {"X-CSRF-Token": "csrf"}},
        ),
        ("POST", "/v1/reviews/bob-review/cancel", {"headers": {"X-CSRF-Token": "csrf"}}),
        ("POST", "/v1/reviews/bob-review/resume", {"headers": {"X-CSRF-Token": "csrf"}}),
        ("GET", "/v1/reviews/bob-review/events", {}),
    )

    for method, path, kwargs in requests:
        response = await client.request(method, path, **kwargs)
        assert response.status_code == 404, (
            f"{method} {path}: {response.status_code} {response.text}"
        )

    assert service.started == []

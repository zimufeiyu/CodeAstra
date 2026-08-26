from __future__ import annotations

import json
from time import perf_counter

import httpx
import pytest

from code_review.api import main
from code_review.application.auth_service import AuthService
from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.review_store import EnhancedInMemoryReviewStore
from code_review.domain.model_protocol import ReviewResponse
from code_review.domain.review_models import ReviewMode, SourceFile
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


class JourneyInference:
    def __init__(self) -> None:
        self.review_requests = []
        self.fix_requests = []

    async def review(self, request):
        self.review_requests.append(request)
        return ReviewResponse(summary="没有额外语义问题。", findings=[], uncovered=[])

    async def propose_fix(self, request):
        self.fix_requests.append(request)
        raise AssertionError("确定性安全修复不得调用模型生成候选")


@pytest.mark.asyncio
async def test_isolated_ordinary_user_journey_has_one_confirm_and_one_rereview(
    monkeypatch, tmp_path
):
    database = tmp_path / "ordinary-user.sqlite3"
    migrate_auth_schema(database, admin_username="admin", admin_password="admin-password")
    auth_store = SQLiteAuthStore(database)
    auth = AuthService(auth_store)
    ordinary = await auth.create_user("ordinary", "temporary-password")
    await auth_store.set_password(
        ordinary.user_id,
        AuthService.hash_password("ordinary-password"),
        False,
    )

    started = perf_counter()
    login = await auth.login("ordinary", "ordinary-password")
    login_ms = (perf_counter() - started) * 1000
    assert login.user.role == "user" and not login.user.must_change_password

    inference = JourneyInference()
    service = HybridReviewService(inference, EnhancedInMemoryReviewStore())
    files = [
        SourceFile.from_content(
            file_id="safe",
            relative_path="pkg/safe.py",
            language="python",
            content="def safe(value):\n    return valu\n",
        ),
        SourceFile.from_content(
            file_id="intent",
            relative_path="pkg/intent.py",
            language="python",
            content="def intent():\n    return missing\n",
        ),
    ]

    started = perf_counter()
    created = await service.create(ReviewMode.PROJECT, files, owner_id=ordinary.user_id)
    await service.run(created.review_id, ordinary.user_id)
    initial_review_ms = (perf_counter() - started) * 1000
    reviewed = await service.get(created.review_id, ordinary.user_id)
    assert reviewed is not None and reviewed.status == "completed"
    assert len(reviewed.findings) == 2

    safe = next(item for item in reviewed.findings if item.file_id == "safe")
    accepted = next(item for item in reviewed.findings if item.file_id == "intent")
    started = perf_counter()
    candidate = await service.preview_fix(created.review_id, ordinary.user_id, safe.finding_id)
    candidate_preview_ms = (perf_counter() - started) * 1000
    before_confirm = await service.get(created.review_id, ordinary.user_id)
    assert before_confirm is not None
    assert next(item for item in before_confirm.files if item.file_id == "safe").content.endswith(
        "return valu\n"
    )

    started = perf_counter()
    decided, immediate_rereview = await service.confirm_fix(
        created.review_id, ordinary.user_id, candidate.candidate_id
    )
    assert immediate_rereview is None and len(decided.findings) == 1
    decided, revised = await service.record_finding_decision(
        created.review_id, ordinary.user_id, accepted.finding_id, "accepted_risk"
    )
    assert revised is not None
    task = service._tasks.get(created.review_id)
    assert task is not None
    await task
    confirm_and_rereview_ms = (perf_counter() - started) * 1000

    final = await service.get(created.review_id, ordinary.user_id)
    assert final is not None and final.status == "completed" and final.findings == []
    assert final.finding_states[safe.finding_id] == "fixed_verified"
    assert final.finding_states[accepted.finding_id] == "accepted_risk"
    assert len(final.revisions) == 1
    assert inference.fix_requests == []
    assert len(inference.review_requests) == 4
    assert service.pipeline_counters()["re_review_count"] == 1

    monkeypatch.setattr(main, "get_auth_service", lambda: auth)
    app = main.create_app()
    app.dependency_overrides[main.get_hybrid_review_service] = lambda: service
    transport = httpx.ASGITransport(app=app)
    started = perf_counter()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={"session": login.session_token, "csrf": login.csrf_token},
    ) as client:
        assert (await client.get("/v1/admin/users")).status_code == 403
        session_response = await client.get(f"/v1/reviews/{created.review_id}")
        report = await client.get(f"/v1/reviews/{created.review_id}/report")
        patch = await client.get(f"/v1/reviews/{created.review_id}/fixes.patch")
        fixed_file = await client.get(
            f"/v1/reviews/{created.review_id}/fixed-files/safe"
        )
    result_download_ms = (perf_counter() - started) * 1000

    assert session_response.status_code == 200
    assert report.status_code == patch.status_code == fixed_file.status_code == 200
    assert "## 已修复" in report.text and "## 接受风险" in report.text
    assert "return value" in fixed_file.text and "+    return value" in patch.text

    measurements = {
        "login_ms": round(login_ms, 3),
        "review_create_and_initial_ms": round(initial_review_ms, 3),
        "candidate_preview_ms": round(candidate_preview_ms, 3),
        "confirm_apply_and_rereview_ms": round(confirm_and_rereview_ms, 3),
        "result_and_download_ms": round(result_download_ms, 3),
        "fake_model_review_calls": len(inference.review_requests),
        "fix_model_calls": len(inference.fix_requests),
        "re_review_count": service.pipeline_counters()["re_review_count"],
    }
    print("ORDINARY_USER_TIMINGS=" + json.dumps(measurements, sort_keys=True))

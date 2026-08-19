from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from code_review.application.auth_service import AuthService
from code_review.application.hybrid_review_service import HybridReviewService
from code_review.domain.model_protocol import ModelSelection
from code_review.domain.review_models import Finding, ReviewMode, ReviewSession, SourceFile
from code_review.infrastructure.persistence.sqlite_auth_migration import migrate_auth_schema
from code_review.infrastructure.persistence.sqlite_auth_store import SQLiteAuthStore


async def _auth(tmp_path):
    database = tmp_path / "auth.sqlite3"
    migrate_auth_schema(database, admin_username="admin", admin_password="current-password")
    return AuthService(SQLiteAuthStore(database))


@pytest.mark.asyncio
@pytest.mark.parametrize("csrf", [None, "stale-csrf"])
async def test_me_rejects_valid_session_with_missing_or_stale_csrf_cookie(tmp_path, csrf):
    service = await _auth(tmp_path)
    login = await service.login("admin", "current-password")
    from fastapi import FastAPI
    from code_review.api.auth_routes import create_auth_router

    app = FastAPI()
    app.include_router(create_auth_router(service))
    cookies = {"session": login.session_token}
    if csrf is not None:
        cookies["csrf"] = csrf
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies) as client:
        response = await client.get("/v1/auth/me")
    assert response.status_code == 403
    assert response.json() == {"detail": "invalid CSRF token"}


def _completed_session():
    session = ReviewSession.create(
        review_id="review-1", owner_id="owner-1", mode=ReviewMode.PASTE,
        files=[SourceFile.from_content(file_id="file-1", relative_path="example.py", language="python", content="dangerous_a()\ndangerous_b()\n")],
    )
    findings = []
    for suffix, line in (("a", 1), ("b", 2)):
        findings.append(Finding(finding_id=f"finding-{suffix}", source="static", analyzer="test", rule_id=suffix, category="security", severity="high", confidence=1.0, file_id="file-1", start_line=line, start_column=1, end_line=line, end_column=13, title=f"问题 {suffix}", hover_summary=suffix, detail=suffix, evidence=f"dangerous_{suffix}()", impact=suffix, suggestion=suffix))
    return session.model_copy(update={"status": "completed", "model": ModelSelection(profile_id="local-qwen3-8b", provider="local", model="Qwen3-8B", display_name="本地 Qwen3-8B"), "findings": findings})


@pytest.mark.asyncio
async def test_finding_followups_use_separate_context_keys_and_never_cross_prompt_history():
    class Store:
        def __init__(self):
            self.session = _completed_session()
            self.history_keys, self.appended_keys = [], []
        async def get(self, review_id, owner_id): return self.session
        async def followups(self, review_id, owner_id, context_key):
            self.history_keys.append(context_key)
            return [SimpleNamespace(role="assistant", content="A-only prior answer")] if context_key.endswith("finding-a") else []
        async def append_followup_exchange(self, question, answer, owner_id, context_key): self.appended_keys.append(context_key)
    class Inference:
        def __init__(self): self.prompts = []
        async def answer_followup(self, request): self.prompts.append(request.messages[-1].content); return "回答"
    store, inference = Store(), Inference()
    service = object.__new__(HybridReviewService)
    service._store, service._inference_service = store, inference
    for finding_id in ("finding-a", "finding-b"):
        await service.ask_followup("review-1", "owner-1", f"{finding_id}？", {"kind": "finding", "file_id": "file-1", "finding_id": finding_id})
    assert len(set(store.history_keys)) == 2
    assert len(set(store.appended_keys)) == 2
    assert "A-only prior answer" in inference.prompts[0]
    assert "A-only prior answer" not in inference.prompts[1]

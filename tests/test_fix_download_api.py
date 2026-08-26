import io
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest

from code_review.api import main
from code_review.domain.review_models import (
    Finding,
    FindingDecisionAudit,
    ReviewMode,
    ReviewRevision,
    ReviewSession,
    SourceFile,
)


class AuthStore:
    async def verify_csrf(self, token, csrf):
        return token


class AuthService:
    def __init__(self) -> None:
        self._store = AuthStore()

    async def current_user(self, token):
        return SimpleNamespace(user_id="owner-1", must_change_password=False) if token else None


def finding(finding_id: str, title: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        source="llm",
        analyzer="test",
        rule_id=f"test.{finding_id}",
        category="correctness",
        severity="medium",
        confidence=0.9,
        file_id="file-1",
        start_line=1,
        start_column=1,
        end_line=1,
        end_column=2,
        title=title,
        hover_summary=title,
        detail=title,
        evidence="value",
        impact="impact",
        suggestion="suggestion",
    )


def completed_session() -> ReviewSession:
    source = SourceFile.from_content(
        file_id="file-1", relative_path="pkg/example.py", language="python", content="value = 3\n"
    )
    fixed = finding("fixed", "已修复问题")
    accepted = finding("accepted", "接受风险问题")
    deferred = finding("deferred", "稍后处理问题")
    revision = ReviewRevision(
        revision_id="revision-1",
        finding_id="fixed",
        file_id="file-1",
        relative_path="pkg/example.py",
        created_at=datetime.now(UTC),
        before_content="value = 1\n",
        before_sha256="a" * 64,
        after_sha256="b" * 64,
        diff="--- a/pkg/example.py\n+++ b/pkg/example.py\n-value = 1\n+value = 2\n",
        explanation="修正数值",
        validation=["Python 语法解析通过"],
    )
    revision_2 = ReviewRevision(
        revision_id="revision-2",
        finding_id="fixed-2",
        file_id="file-1",
        relative_path="pkg/example.py",
        created_at=datetime.now(UTC),
        before_content="value = 2\n",
        before_sha256="b" * 64,
        after_sha256=source.sha256,
        diff="--- a/pkg/example.py\n+++ b/pkg/example.py\n-value = 2\n+value = 3\n",
        explanation="再次修正数值",
        validation=["Python 语法解析通过"],
    )
    return ReviewSession.create(
        review_id="review-1", owner_id="owner-1", mode=ReviewMode.SINGLE, files=[source]
    ).model_copy(
        update={
            "status": "completed",
            "findings": [accepted, deferred],
            "finding_decisions": {
                "fixed": "fixed",
                "accepted": "accepted_risk",
                "deferred": "deferred",
            },
            "decided_findings": {"fixed": fixed, "accepted": accepted, "deferred": deferred},
            "finding_decision_history": [
                FindingDecisionAudit(
                    finding_id="accepted",
                    action="decided",
                    decision="accepted_risk",
                    reason="用户明确接受该问题的风险。",
                ),
                FindingDecisionAudit(
                    finding_id="accepted",
                    action="reopened",
                    decision="accepted_risk",
                    reason="用户将该问题重新打开并恢复为待处理。",
                ),
                FindingDecisionAudit(
                    finding_id="accepted",
                    action="decided",
                    decision="accepted_risk",
                    reason="用户再次接受风险。",
                ),
            ],
            "revisions": [revision, revision_2],
        }
    )


class ReviewService:
    def __init__(self) -> None:
        self.session = completed_session()
        self.counters: dict[str, int] = {}

    async def get(self, review_id, owner_id):
        return self.session if (review_id, owner_id) == ("review-1", "owner-1") else None

    def record_pipeline_counter(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1


@pytest.mark.asyncio
async def test_report_and_fix_downloads_preserve_decisions_and_content(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "get_auth_service", lambda: AuthService())
    app = main.create_app()
    service = ReviewService()
    app.dependency_overrides[main.get_hybrid_review_service] = lambda: service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={"session": "owner-1"}
    ) as client:
        report = await client.get("/v1/reviews/review-1/report")
        patch = await client.get("/v1/reviews/review-1/fixes.patch")
        fixed_file = await client.get("/v1/reviews/review-1/fixed-files/file-1")
        archive = await client.get("/v1/reviews/review-1/fixed-files.zip")
        assert (await client.get("/v1/reviews/review-1/report")).content == report.content
        assert (await client.get("/v1/reviews/review-1/fixes.patch")).content == patch.content
        assert (await client.get("/v1/reviews/review-1/fixed-files.zip")).content == archive.content

    assert report.status_code == 200
    assert all(
        title in report.text for title in (
            "## 已修复", "## 接受风险", "## 待处理", "## 处理历史", "## 修订 Diff"
        )
    )
    assert "重新打开" in report.text and "用户再次接受风险" in report.text
    assert "接受风险问题" in report.text and "稍后处理问题" in report.text
    assert "未发现已验证问题" not in report.text
    assert patch.status_code == 200 and "+value = 3" in patch.text
    assert "-value = 2" not in patch.text
    (tmp_path / "pkg").mkdir()
    target = tmp_path / "pkg" / "example.py"
    target.write_text("value = 1\n", encoding="utf-8")
    checked = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=tmp_path,
        input=patch.text,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    applied = subprocess.run(
        ["git", "apply", "-"],
        cwd=tmp_path,
        input=patch.text,
        text=True,
        capture_output=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    assert target.read_text(encoding="utf-8") == "value = 3\n"
    assert fixed_file.text == "value = 3\n"
    with ZipFile(io.BytesIO(archive.content)) as bundle:
        assert bundle.read("pkg/example.py").decode() == "value = 3\n"
    assert service.counters == {"artifact_cache_misses": 3, "artifact_cache_hits": 3}

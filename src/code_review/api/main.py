import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated

import anyio
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr, ValidationError
from starlette.types import Receive, Scope, Send

from code_review.api.admin_routes import create_admin_router
from code_review.api.auth_routes import create_auth_router
from code_review.api.dependencies import (
    close_dependencies,
    get_auth_service,
    get_deepseek_model_catalog,
    get_gateway_health_service,
    get_hybrid_review_service,
    get_inference_service,
    get_review_store,
)
from code_review.api.deployment_routes import router as deployment_router
from code_review.api.schemas import (
    DeepSeekModelResponse,
    DeepSeekModelsResponse,
    FindingDecisionRequest,
    FollowupContextInput,
    FollowupRequest,
    GatewayHealthResponse,
    InstanceHealth,
    ModelProfileResponse,
    ReviewCreatedResponse,
    ReviewCreateRequest,
    ReviewRenameRequest,
)
from code_review.application.context_budget import ConservativeTokenEstimator
from code_review.application.deepseek_model_selection import (
    ReviewShape,
    select_deepseek_model,
)
from code_review.application.health_service import GatewayHealthService
from code_review.application.hybrid_review_service import HybridReviewService
from code_review.application.inference_service import (
    ReviewOutputError,
)
from code_review.application.model_router import RoutedInferenceService
from code_review.application.user_admin_service import UserAdminService
from code_review.config.settings import get_settings
from code_review.domain.model_protocol import InferenceRequest, ModelSelection, ReviewResponse
from code_review.domain.review_models import (
    GitLabReviewOrigin,
    Language,
    LocalDiffReviewOrigin,
    ReviewMode,
    ReviewRevision,
    ReviewSession,
    SourceFile,
)
from code_review.infrastructure.deepseek.catalog import (
    DeepSeekCatalogError,
    DeepSeekModelCatalog,
)
from code_review.infrastructure.deepseek.client import DeepSeekAPIError
from code_review.infrastructure.deepseek.context import bind_deepseek_context
from code_review.infrastructure.persistence.user_data_purger import UserDataPurger
from code_review.integrations.gitlab import (
    GitLabAccountProfile,
    GitLabAccountVerifyRequest,
    GitLabClient,
    GitLabIntegrationError,
    GitLabMergeRequestPreview,
    GitLabPreviewRequest,
)
from code_review.integrations.local_diff import (
    LocalDiffError,
    LocalDiffFileInput,
    LocalDiffPreview,
    LocalDiffPreviewRequest,
    LocalDiffService,
)

HealthServiceDependency = Annotated[
    GatewayHealthService,
    Depends(get_gateway_health_service),
]
InferenceServiceDependency = Annotated[
    RoutedInferenceService,
    Depends(get_inference_service),
]
HybridReviewServiceDependency = Annotated[
    HybridReviewService,
    Depends(get_hybrid_review_service),
]
DeepSeekCatalogDependency = Annotated[
    DeepSeekModelCatalog,
    Depends(get_deepseek_model_catalog),
]


class DisconnectAwareStreamingResponse(StreamingResponse):
    """Always cancel the producer when the HTTP client disconnects."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as task_group:

            async def run_and_cancel(awaitable: Callable[[], Awaitable[None]]) -> None:
                await awaitable()
                task_group.cancel_scope.cancel()

            async def stream_response() -> None:
                await self.stream_response(send)

            async def listen_for_disconnect() -> None:
                await self.listen_for_disconnect(receive)

            task_group.start_soon(run_and_cancel, stream_response)
            await run_and_cancel(listen_for_disconnect)

        if self.background is not None:
            await self.background()


async def cleanup_expired_reviews() -> None:
    while True:
        await asyncio.sleep(3600)
        await get_review_store().delete_expired(datetime.now(tz=UTC))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = get_hybrid_review_service()
    await service.recover()
    try:
        yield
    finally:
        await close_dependencies()


def default_frontend_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


class LazyReviewCancellation:
    async def cancel(self, review_id: str, owner_id: str) -> bool:
        return await get_hybrid_review_service().cancel(review_id, owner_id)


def create_app(frontend_directory: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Local Code Review Model Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(deployment_router)
    auth_service = get_auth_service()
    app.include_router(create_auth_router(auth_service))
    if hasattr(auth_service, "_store") and hasattr(auth_service, "hash_password"):
        settings = get_settings()
        admin_service = UserAdminService(
            auth_service._store,
            password_hasher=auth_service.hash_password,
            review_service=LazyReviewCancellation(),
            purger=UserDataPurger(
                [settings.state_dir / "users"], settings.state_dir / "user-quarantine"
            ),
        )
        app.include_router(create_admin_router(auth_service, admin_service))

    @app.middleware("http")
    async def require_authentication(request: Request, call_next):
        path = request.url.path
        public = (
            path == "/health" or path == "/" or path.startswith("/assets/")
            or path.startswith("/v1/auth/") or not path.startswith("/v1/")
        )
        if public:
            return await call_next(request)
        token = request.cookies.get("session", "")
        user = await auth_service.current_user(token)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        if user.must_change_password:
            return JSONResponse(status_code=403, content={"detail": "password change required"})
        request.state.current_user = user
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("X-CSRF-Token", "")
            if await auth_service._store.verify_csrf(token, csrf) is None:
                return JSONResponse(status_code=403, content={"detail": "invalid CSRF token"})
        return await call_next(request)

    @app.exception_handler(DeepSeekAPIError)
    async def deepseek_api_error_handler(
        _: Request,
        error: DeepSeekAPIError,
    ) -> JSONResponse:
        status_code = (
            error.status_code
            if error.status_code is not None and 400 <= error.status_code <= 599
            else 503
        )
        return JSONResponse(status_code=status_code, content={"detail": str(error)})

    frontend_dir = frontend_directory or default_frontend_directory()
    frontend_index = frontend_dir / "index.html"
    frontend_assets = frontend_dir / "assets"

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/v1/integrations/deepseek/models",
        response_model=DeepSeekModelsResponse,
    )
    async def deepseek_models(
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> DeepSeekModelsResponse:
        if api_key is None or not api_key.strip():
            raise HTTPException(status_code=400, detail="请先填写 DeepSeek API Key")
        try:
            models = await catalog.list_models(SecretStr(api_key.strip()))
        except DeepSeekCatalogError as error:
            status_code = error.status_code or 503
            raise HTTPException(status_code=status_code, detail=str(error)) from error

        def display_name(model_id: str) -> str:
            suffix = model_id.removeprefix("deepseek-").replace("-", " ")
            words = [
                word.upper()
                if word.lower().startswith("v") and word[1:].isdigit()
                else word.capitalize()
                for word in suffix.split()
            ]
            return "DeepSeek " + " ".join(words)

        return DeepSeekModelsResponse(
            models=[
                DeepSeekModelResponse(id=model_id, display_name=display_name(model_id))
                for model_id in models
            ]
        )

    @app.get("/v1/model-profiles", response_model=list[ModelProfileResponse])
    async def model_profiles() -> list[ModelProfileResponse]:
        settings = get_settings()
        profiles: list[ModelProfileResponse] = []
        if settings.local_provider_enabled:
            if settings.sglang_endpoints:
                profiles.append(
                    ModelProfileResponse(
                        profile_id="local-qwen3-8b",
                        provider="local",
                        model=settings.model_name,
                        display_name="\u672c\u5730 Qwen3-8B",
                        available=True,
                        context_tokens=settings.model_context_tokens,
                    )
                )
            if settings.qwen3_32b_endpoints:
                profiles.append(
                    ModelProfileResponse(
                        profile_id="local-qwen3-32b",
                        provider="local",
                        model=settings.qwen3_32b_model_name,
                        display_name="\u672c\u5730 Qwen3-32B",
                        available=True,
                        context_tokens=settings.qwen3_32b_context_tokens,
                    )
                )
        if settings.deepseek_provider_enabled:
            profiles.append(
                ModelProfileResponse(
                    profile_id="deepseek-api",
                    provider="deepseek",
                    model=settings.deepseek_model_name,
                    display_name="DeepSeek API",
                    available=True,
                    unavailable_reason=None,
                    requires_user_api_key=True,
                    context_tokens=settings.deepseek_context_tokens,
                )
            )
        return profiles

    @app.post(
        "/v1/integrations/gitlab/account/verify",
        response_model=GitLabAccountProfile,
    )
    async def verify_gitlab_account(
        payload: GitLabAccountVerifyRequest,
    ) -> GitLabAccountProfile:
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            try:
                return await GitLabClient(client).verify_account(payload)
            except GitLabIntegrationError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail=str(error),
                ) from error

    @app.post(
        "/v1/integrations/gitlab/merge-request/preview",
        response_model=GitLabMergeRequestPreview,
    )
    async def preview_gitlab_merge_request(
        payload: GitLabPreviewRequest,
    ) -> GitLabMergeRequestPreview:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            try:
                async with asyncio.timeout(90):
                    return await GitLabClient(client).preview_merge_request(payload)
            except TimeoutError as error:
                raise HTTPException(
                    status_code=504,
                    detail="GitLab 导入超过 90 秒，已停止读取",
                ) from error
            except GitLabIntegrationError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail=str(error),
                ) from error

    @app.post(
        "/v1/integrations/local-diff/preview",
        response_model=LocalDiffPreview,
    )
    async def preview_local_diff(
        payload: LocalDiffPreviewRequest,
    ) -> LocalDiffPreview:
        try:
            return LocalDiffService().preview(payload)
        except LocalDiffError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/health/instances", response_model=GatewayHealthResponse)
    async def instance_health(
        service: HealthServiceDependency,
    ) -> GatewayHealthResponse:
        return GatewayHealthResponse(
            instances=[
                InstanceHealth(
                    endpoint_id=state.endpoint_id,
                    inflight_requests=state.inflight_requests,
                    inflight_tokens=state.inflight_tokens,
                    circuit_open=state.circuit_open,
                )
                for state in service.snapshot()
            ]
        )

    @app.post("/v1/review", response_model=ReviewResponse)
    async def review(
        request: InferenceRequest,
        service: InferenceServiceDependency,
    ) -> ReviewResponse:
        try:
            return await service.review(request)
        except ReviewOutputError as error:
            raise HTTPException(
                status_code=502,
                detail=("模型未能生成完整的中文审查结果，请缩短输入或稍后重试。"),
            ) from error

    @app.post("/v1/review/stream")
    async def review_stream(
        request: InferenceRequest,
        service: InferenceServiceDependency,
    ) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            def encode(event: str, data: object) -> str:
                return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            try:
                async for event in service.review_stream(request):
                    yield encode(event.event, event.data)
            except asyncio.CancelledError:
                raise
            except ReviewOutputError:
                yield encode(
                    "error",
                    {
                        "code": "INCOMPLETE_OUTPUT",
                        "message": "模型未能生成完整的中文审查结果，请缩短输入或稍后重试。",
                    },
                )
            except Exception:
                yield encode(
                    "error",
                    {
                        "code": "MODEL_REQUEST_FAILED",
                        "message": "模型服务请求失败，请稍后重试。",
                    },
                )

        return DisconnectAwareStreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def build_sources(mode: ReviewMode, payload: ReviewCreateRequest) -> list[SourceFile]:
        def language_for(
            filename: str,
            language: str | None,
            content: str,
        ) -> Language:
            if language is not None and language not in {"python", "cpp"}:
                raise HTTPException(
                    status_code=400,
                    detail="仅支持 Python 和 C++ 代码审查",
                )

            suffix = PurePosixPath(filename).suffix.lower()
            extension_languages: dict[str, Language] = {
                ".py": "python",
                ".pyw": "python",
                ".cc": "cpp",
                ".cpp": "cpp",
                ".cxx": "cpp",
                ".hh": "cpp",
                ".hpp": "cpp",
                ".hxx": "cpp",
            }
            extension_language = extension_languages.get(suffix)
            if suffix and extension_language is None:
                raise HTTPException(
                    status_code=400,
                    detail="仅支持 Python 和 C++ 文件",
                )

            unsupported_syntax = (
                re.search(
                    r"^\s*(?:function\s+\w+\s*\(|(?:let|var)\s+\w+\s*=|console\.)",
                    content,
                    re.IGNORECASE | re.MULTILINE,
                )
                or re.search(
                    r"^\s*(?:const|let|var)\s+\w+\s*=.*=>",
                    content,
                    re.IGNORECASE | re.MULTILINE,
                )
                or re.search(
                    r"^\s*class\s+\w+\s*\{[\s\S]*\bconstructor\s*\(",
                    content,
                    re.IGNORECASE | re.MULTILINE,
                )
            )
            if unsupported_syntax:
                raise HTTPException(
                    status_code=400,
                    detail="\u4ec5\u652f\u6301 Python \u548c C++ \u4ee3\u7801",
                )

            python_score = sum(
                (
                    3
                    if re.search(
                        r"(^|\n)\s*(?:(?:async\s+)?def\s+\w+\s*\([^)]*\)"
                        r"(?:\s*->\s*[^:\n]+)?"
                        r"|class\s+\w+(?:\([^)]*\))?)\s*:"
                        r"|(^|\n)\s*(?:import|from)\b",
                        content,
                    )
                    else 0,
                    3 if re.search(r"\b(print|eval|exec|input)\s*\(", content) else 0,
                    2
                    if re.search(
                        r"^\s*[A-Za-z_]\w*\s*=(?!=)[^;\n]*$",
                        content,
                        re.MULTILINE,
                    )
                    else 0,
                    3
                    if re.search(
                        r"^\s*[A-Za-z_]\w*\s*:\s*[^=\n]+(?:=\s*[^;\n]+)?$",
                        content,
                        re.MULTILINE,
                    )
                    else 0,
                    1
                    if re.search(r"^\s{4,}\S+", content, re.MULTILINE)
                    and re.search(r":\s*(\n|$)", content)
                    else 0,
                )
            )
            cpp_score = sum(
                (
                    4 if re.search(r'#include\s*[<"]', content) else 0,
                    3 if re.search(r"\bstd::\w+|using\s+namespace\s+std\b", content) else 0,
                    3 if re.search(r"\b(?:int|void|auto|bool|string)\s+main\s*\(", content) else 0,
                    3
                    if re.search(
                        r"\b[A-Za-z_]\w*(?:::\w+)*(?:<[^;{}\n]+>)?"
                        r"\s*[*&]?\s+\w+\s*\([^;{}]*\)\s*(?:const\s*)?\{",
                        content,
                    )
                    else 0,
                    3
                    if (
                        extension_language == "cpp"
                        or re.search(r"\b(?:public|private|protected)\s*:", content)
                    )
                    and re.search(r"\b(?:class|struct|enum)\s+\w+", content)
                    else 0,
                    1 if re.search(r"[{}]\s*$|;\s*(?:\n|$)", content, re.MULTILINE) else 0,
                    1
                    if re.search(r"\b(?:int|void|char|float|double)\s+\w+\s*(?:=|;)", content)
                    else 0,
                )
            )
            detected: Language | None = None
            if python_score >= 2 and python_score >= cpp_score + 1:
                detected = "python"
            elif cpp_score >= 2 and cpp_score >= python_score + 1:
                detected = "cpp"

            if detected is None:
                raise HTTPException(
                    status_code=400,
                    detail="无法识别代码语言，仅支持 Python 和 C++",
                )
            if language is not None and language != detected:
                raise HTTPException(
                    status_code=400,
                    detail="声明的语言与代码内容不一致",
                )
            if extension_language is not None and extension_language != detected:
                raise HTTPException(
                    status_code=400,
                    detail="文件扩展名与代码内容不一致",
                )
            return detected

        def safe_path(filename: str) -> str:
            path = PurePosixPath(filename)
            if path.is_absolute() or ".." in path.parts:
                raise HTTPException(status_code=400, detail="文件路径不合法")
            return str(path)

        if mode in {ReviewMode.PASTE, ReviewMode.SINGLE}:
            if not payload.content or not payload.content.strip():
                raise HTTPException(status_code=400, detail="代码内容不能为空")
            language = language_for(
                payload.filename or "",
                payload.language,
                payload.content,
            )
            extension = "py" if language == "python" else "cpp"
            filename = safe_path(payload.filename or f"snippet.{extension}")
            return [
                SourceFile.from_content(
                    file_id="file-1",
                    relative_path=filename,
                    language=language,
                    content=payload.content,
                )
            ]

        if not payload.files:
            raise HTTPException(status_code=400, detail="项目至少需要一个文件")
        sources: list[SourceFile] = []
        for index, item in enumerate(payload.files, start=1):
            if not item.content.strip():
                continue
            sources.append(
                SourceFile.from_content(
                    file_id=f"file-{index}",
                    relative_path=safe_path(item.filename),
                    language=language_for(item.filename, item.language, item.content),
                    content=item.content,
                )
            )
        if not sources:
            raise HTTPException(status_code=400, detail="项目没有可审查的文件")
        return sources

    def public_revision(
        revision: ReviewRevision,
        *,
        include_diff: bool = False,
    ) -> dict[str, object]:
        data = revision.model_dump(mode="json")
        data.pop("before_content", None)
        data.pop("before_changed_ranges", None)
        if not include_diff:
            data.pop("diff", None)
        return data

    def public_session(session: ReviewSession) -> dict[str, object]:
        data = session.model_dump(mode="json")
        data.pop("owner_id", None)
        data["title"] = session.display_title()
        data["revisions"] = [public_revision(item) for item in session.revisions]
        file_names = {item.file_id: item.relative_path for item in session.files}
        for finding in data["findings"]:
            if isinstance(finding, dict):
                finding["file"] = file_names.get(str(finding.get("file_id")), "")
        return data

    def history_item(session: ReviewSession) -> dict[str, object]:
        return {
            "review_id": session.review_id,
            "title": session.display_title(),
            "mode": session.mode,
            "status": session.status,
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "file_count": len(session.files),
            "file_names": [item.relative_path for item in session.files],
            "summary": session.summary.model_dump(mode="json"),
            "error": session.error,
            "origin": (
                session.origin.model_dump(mode="json") if session.origin is not None else None
            ),
        }

    @asynccontextmanager
    async def bind_review_deepseek(
        review_id: str,
        owner_id: str,
        service: HybridReviewService,
        catalog: DeepSeekModelCatalog,
        api_key: str | None,
    ) -> AsyncIterator[ReviewSession]:
        session = await service.get(review_id, owner_id)
        if session is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        if session.model.provider != "deepseek":
            yield session
            return
        if api_key is None or not api_key.strip():
            raise HTTPException(status_code=400, detail="请先填写 DeepSeek API Key")
        try:
            models = await catalog.list_models(SecretStr(api_key.strip()))
        except DeepSeekCatalogError as error:
            raise HTTPException(
                status_code=error.status_code or 503,
                detail=str(error),
            ) from error
        if session.model.model not in models:
            raise HTTPException(
                status_code=409,
                detail="该审查固定的 DeepSeek 模型当前账号不可用，请重新选择模型创建审查。",
            )
        with bind_deepseek_context(api_key, session.model.model):
            yield session

    def render_report(session: ReviewSession) -> str:
        lines = [
            "# 代码审查报告",
            "",
            f"- 审查 ID：{session.review_id}",
            f"- 状态：{session.status}",
            f"- 创建时间：{session.created_at.isoformat()}",
            f"- 文件数：{len(session.files)}",
            "",
            "## 摘要",
            "",
            session.summary.text,
            "",
            "## 文件",
            "",
            *[f"- `{item.relative_path}`" for item in session.files],
            "",
            "## 已验证问题",
            "",
        ]
        if isinstance(session.origin, GitLabReviewOrigin):
            origin_lines = [
                "## GitLab 来源",
                "",
                (
                    f"- 合并请求：[{session.origin.project_path} "
                    f"!{session.origin.merge_request_iid}]"
                    f"({session.origin.merge_request_url})"
                ),
                f"- 基线版本：{session.origin.base_sha}",
                f"- 审查版本：{session.origin.head_sha}",
                f"- 已选文件：{len(session.origin.selected_paths)}",
                "",
            ]
            lines[lines.index("## 摘要") : lines.index("## 摘要")] = origin_lines
        elif isinstance(session.origin, LocalDiffReviewOrigin):
            origin_lines = [
                "## 本地版本对比来源",
                "",
                f"- 修改前：{session.origin.old_label}",
                f"- 修改后：{session.origin.new_label}",
                f"- 已选文件：{len(session.origin.selected_paths)}",
                "",
            ]
            lines[lines.index("## 摘要") : lines.index("## 摘要")] = origin_lines
        if not session.findings:
            lines.append("未发现已验证问题。")
        for index, finding in enumerate(session.findings, start=1):
            path = next(
                (item.relative_path for item in session.files if item.file_id == finding.file_id),
                finding.file_id,
            )
            lines.extend(
                [
                    f"### {index}. {finding.title}",
                    "",
                    f"- 严重度：{finding.severity}",
                    f"- 位置：`{path}:{finding.start_line}-{finding.end_line}`",
                    f"- 置信度：{finding.confidence:.0%}",
                    "",
                    f"证据：{finding.evidence}",
                    "",
                    f"影响：{finding.impact}",
                    "",
                    f"建议：{finding.suggestion}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    async def create_review(
        mode: ReviewMode,
        payload: ReviewCreateRequest,
        owner_id: str,
        service: HybridReviewService,
        catalog: DeepSeekModelCatalog,
        api_key: str | None,
    ) -> ReviewCreatedResponse:
        sources = build_sources(mode, payload)
        profile_id = payload.model_profile_id or get_settings().default_model_profile_id
        origin = payload.origin
        if origin is not None:
            selected_paths = list(dict.fromkeys(origin.selected_paths))
            sources_by_path = {source.relative_path: source for source in sources}
            selected_path_set = set(selected_paths)
            if not selected_paths or not selected_path_set.issubset(sources_by_path):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "GitLab 来源文件与实际审查文件不一致"
                        if isinstance(origin, GitLabReviewOrigin)
                        else "本地版本来源文件与实际审查文件不一致"
                    ),
                )

            if isinstance(origin, GitLabReviewOrigin):
                if not set(origin.changed_ranges or {}).issubset(selected_path_set):
                    raise HTTPException(
                        status_code=400,
                        detail="GitLab 来源文件与实际审查文件不一致",
                    )
                for source_path, ranges in (origin.changed_ranges or {}).items():
                    line_count = max(
                        1,
                        len(sources_by_path[source_path].content.splitlines()),
                    )
                    if any(changed.end_line > line_count for changed in ranges):
                        raise HTTPException(
                            status_code=400,
                            detail="GitLab 变更行范围超出文件内容",
                        )
                origin = origin.model_copy(update={"selected_paths": selected_paths})
            else:
                base_files = payload.local_diff_base_files
                base_by_path = {item.filename: item for item in base_files}
                if len(base_by_path) != len(base_files) or set(base_by_path) != selected_path_set:
                    raise HTTPException(
                        status_code=400,
                        detail="本地旧版本文件与所选新版本文件不一致",
                    )
                service_for_diff = LocalDiffService()
                canonical_ranges = {}
                old_sha256 = {}
                new_sha256 = {}
                for source_path in selected_paths:
                    source = sources_by_path[source_path]
                    try:
                        change = service_for_diff.compare(
                            LocalDiffFileInput(
                                filename=source_path,
                                content=base_by_path[source_path].content,
                            ),
                            LocalDiffFileInput(
                                filename=source_path,
                                content=source.content,
                            ),
                        )
                    except LocalDiffError as error:
                        raise HTTPException(
                            status_code=400,
                            detail=str(error),
                        ) from error
                    if not change.selectable:
                        raise HTTPException(
                            status_code=400,
                            detail=change.unavailable_reason or "本地版本没有可审查变更",
                        )
                    canonical_ranges[source_path] = change.changed_ranges
                    old_sha256[source_path] = change.old_sha256
                    new_sha256[source_path] = change.new_sha256
                origin = origin.model_copy(
                    update={
                        "selected_paths": selected_paths,
                        "changed_ranges": canonical_ranges,
                        "old_sha256": old_sha256,
                        "new_sha256": new_sha256,
                    }
                )
        elif payload.local_diff_base_files:
            raise HTTPException(
                status_code=400,
                detail="普通文件审查不能携带本地旧版本",
            )
        try:
            if profile_id == "deepseek-api":
                if api_key is None or not api_key.strip():
                    raise HTTPException(
                        status_code=400,
                        detail="请先填写 DeepSeek API Key",
                    )
                available_models = await catalog.list_models(SecretStr(api_key.strip()))
                estimator = ConservativeTokenEstimator()
                shape = ReviewShape(
                    mode=mode,
                    file_count=len(sources),
                    total_lines=sum(max(1, len(source.content.splitlines())) for source in sources),
                    estimated_input_tokens=sum(
                        estimator.estimate_text(source.content) for source in sources
                    ),
                )
                selected_model = select_deepseek_model(
                    payload.deepseek_selection_mode,
                    payload.deepseek_model,
                    available_models,
                    shape,
                )
                suffix = selected_model.removeprefix("deepseek-").replace("-", " ")
                display_name = "DeepSeek " + " ".join(
                    word.upper()
                    if word.lower().startswith("v") and word[1:].isdigit()
                    else word.capitalize()
                    for word in suffix.split()
                )
                model = ModelSelection(
                    profile_id="deepseek-api",
                    provider="deepseek",
                    model=selected_model,
                    display_name=display_name,
                    selection_source=payload.deepseek_selection_mode,
                )
                with bind_deepseek_context(api_key, selected_model):
                    session = await service.create(
                        mode,
                        sources,
                        owner_id=owner_id,
                        origin=origin,
                        model_profile_id="deepseek-api",
                        model=model,
                    )
                    await service.start(session.review_id, owner_id)
            else:
                session = await service.create(
                    mode,
                    sources,
                    owner_id=owner_id,
                    origin=origin,
                    model_profile_id=profile_id,
                )
                await service.start(session.review_id, owner_id)
        except DeepSeekCatalogError as error:
            raise HTTPException(
                status_code=error.status_code or 503,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return ReviewCreatedResponse(
            review_id=session.review_id,
            status=session.status,
            expires_at=session.expires_at.isoformat(),
        )

    @app.post("/v1/reviews/paste", response_model=ReviewCreatedResponse, status_code=202)
    async def create_paste_review(
        request: Request,
        payload: ReviewCreateRequest,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> ReviewCreatedResponse:
        return await create_review(ReviewMode.PASTE, payload, request.state.current_user.user_id, service, catalog, api_key)

    @app.post("/v1/reviews/single", response_model=ReviewCreatedResponse, status_code=202)
    async def create_single_review(
        request: Request,
        payload: ReviewCreateRequest,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> ReviewCreatedResponse:
        return await create_review(ReviewMode.SINGLE, payload, request.state.current_user.user_id, service, catalog, api_key)

    @app.post("/v1/reviews/project", response_model=ReviewCreatedResponse, status_code=202)
    async def create_project_review(
        request: Request,
        payload: ReviewCreateRequest,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> ReviewCreatedResponse:
        return await create_review(ReviewMode.PROJECT, payload, request.state.current_user.user_id, service, catalog, api_key)

    @app.get("/v1/reviews")
    async def list_reviews(
        request: Request,
        service: HybridReviewServiceDependency,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        sessions = await service.list_sessions(request.state.current_user.user_id, limit, offset)
        return {
            "items": [history_item(item) for item in sessions],
            "limit": limit,
            "offset": offset,
        }

    @app.patch("/v1/reviews/{review_id}")
    async def rename_review(
        request: Request,
        review_id: str,
        payload: ReviewRenameRequest,
        service: HybridReviewServiceDependency,
    ) -> dict[str, object]:
        try:
            session = await service.rename(review_id, request.state.current_user.user_id, payload.title)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="审查记录不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return public_session(session)

    @app.delete("/v1/reviews/{review_id}", status_code=204)
    async def delete_review(
        request: Request,
        review_id: str,
        service: HybridReviewServiceDependency,
    ) -> Response:
        if not await service.delete(review_id, request.state.current_user.user_id):
            raise HTTPException(status_code=404, detail="审查记录不存在")
        return Response(status_code=204)

    @app.post("/v1/reviews/{review_id}/findings/{finding_id}/decision")
    async def decide_review_finding(
        review_id: str,
        finding_id: str,
        payload: FindingDecisionRequest,
        request: Request,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> dict[str, object]:
        try:
            owner_id = request.state.current_user.user_id
            async with bind_review_deepseek(review_id, owner_id, service, catalog, api_key):
                decided, revised, explanation = await service.decide_finding(
                    review_id,
                    owner_id,
                    finding_id,
                    payload.decision,
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="审查记录不存在") from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail="问题不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ReviewOutputError as error:
            message = str(error)
            if message.startswith("生成的修复"):
                detail = message
            elif "empty or truncated" in message:
                detail = "模型返回的修复内容不完整，原代码和问题已保留，请重试。"
            else:
                detail = "模型未能生成可应用的修复，原代码和问题已保留，请稍后重试。"
            raise HTTPException(status_code=502, detail=detail) from error
        return {
            "session": public_session(decided),
            "revised_review": public_session(revised) if revised is not None else None,
            "explanation": explanation,
        }

    @app.get("/v1/reviews/{review_id}/revisions")
    async def list_review_revisions(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
    ) -> dict[str, object]:
        try:
            revisions = await service.revisions(review_id, request.state.current_user.user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="审查记录不存在") from error
        return {"items": [public_revision(item, include_diff=True) for item in revisions]}

    @app.post("/v1/reviews/{review_id}/revisions/{revision_id}/undo")
    async def undo_review_revision(
        review_id: str,
        revision_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> dict[str, object]:
        try:
            owner_id = request.state.current_user.user_id
            async with bind_review_deepseek(review_id, owner_id, service, catalog, api_key):
                restarted = await service.undo_revision(review_id, owner_id, revision_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="审查记录不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"revised_review": public_session(restarted)}

    @app.get("/v1/reviews/{review_id}/report")
    async def download_review_report(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
    ) -> Response:
        session = await service.get(review_id, request.state.current_user.user_id)
        if session is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        if session.status != "completed":
            raise HTTPException(status_code=409, detail="审查完成后才能导出报告")
        safe_review_id = re.sub(r"[^A-Za-z0-9_.-]", "-", review_id)
        return Response(
            content=render_report(session),
            media_type="text/markdown",
            headers={"Content-Disposition": (f'attachment; filename="{safe_review_id}-report.md"')},
        )

    @app.get("/v1/reviews/{review_id}/followups")
    async def get_review_followups(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
        context: str | None = Query(default=None, max_length=25000),
    ) -> dict[str, object]:
        try:
            parsed_context = (
                FollowupContextInput.model_validate(json.loads(context)).model_dump()
                if context is not None
                else None
            )
        except (json.JSONDecodeError, TypeError, ValidationError) as error:
            raise HTTPException(status_code=422, detail="追问上下文格式无效") from error
        try:
            owner_id = request.state.current_user.user_id
            messages = (
                await service.followups(review_id, owner_id)
                if parsed_context is None
                else await service.followups(review_id, owner_id, parsed_context)
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="审查记录不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"messages": [item.model_dump(mode="json") for item in messages]}
    @app.post("/v1/reviews/{review_id}/followups")
    async def create_review_followup(
        review_id: str,
        payload: FollowupRequest,
        request: Request,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> dict[str, object]:
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="追问内容不能为空")
        try:
            owner_id = request.state.current_user.user_id
            async with bind_review_deepseek(review_id, owner_id, service, catalog, api_key):
                messages = await service.ask_followup(
                    review_id,
                    owner_id,
                    question,
                    payload.context.model_dump() if payload.context is not None else None,
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="审查记录不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ReviewOutputError as error:
            raise HTTPException(
                status_code=502,
                detail="模型未能生成完整的追问回答，请稍后重试。",
            ) from error
        return {
            "messages": [item.model_dump(mode="json") for item in messages],
        }

    @app.get("/v1/reviews/{review_id}")
    async def get_review(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
    ) -> dict[str, object]:
        session = await service.get(review_id, request.state.current_user.user_id)
        if session is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        return public_session(session)

    @app.get("/v1/reviews/{review_id}/files/{file_id}")
    async def get_review_file(
        review_id: str,
        file_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
    ) -> dict[str, object]:
        session = await service.get(review_id, request.state.current_user.user_id)
        if session is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        source = next((item for item in session.files if item.file_id == file_id), None)
        if source is None:
            raise HTTPException(status_code=404, detail="源文件不存在")
        return source.model_dump(mode="json")

    @app.get("/v1/reviews/{review_id}/findings/{finding_id}")
    async def get_review_finding(
        review_id: str,
        finding_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
    ) -> dict[str, object]:
        session = await service.get(review_id, request.state.current_user.user_id)
        if session is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        finding = next((item for item in session.findings if item.finding_id == finding_id), None)
        if finding is None:
            raise HTTPException(status_code=404, detail="问题不存在")
        data = finding.model_dump(mode="json")
        data["file"] = next(
            (item.relative_path for item in session.files if item.file_id == finding.file_id),
            "",
        )
        return data

    @app.post("/v1/reviews/{review_id}/cancel")
    async def cancel_review(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
    ) -> dict[str, str]:
        owner_id = request.state.current_user.user_id
        if await service.get(review_id, owner_id) is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        if not await service.cancel(review_id, owner_id):
            raise HTTPException(status_code=404, detail="审查记录不存在")
        return {"review_id": review_id, "status": "cancelled"}

    @app.post("/v1/reviews/{review_id}/resume")
    async def resume_review(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
        catalog: DeepSeekCatalogDependency,
        api_key: str | None = Header(default=None, alias="X-DeepSeek-API-Key"),
    ) -> dict[str, str]:
        owner_id = request.state.current_user.user_id
        async with bind_review_deepseek(review_id, owner_id, service, catalog, api_key):
            if not await service.resume(review_id, owner_id):
                raise HTTPException(status_code=404, detail="review not found")
        session = await service.get(review_id, owner_id)
        status = session.status if session is not None else "queued"
        return {"review_id": review_id, "status": status}

    @app.get("/v1/reviews/{review_id}/events")
    async def review_events(
        review_id: str,
        request: Request,
        service: HybridReviewServiceDependency,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        owner_id = request.state.current_user.user_id
        if await service.get(review_id, owner_id) is None:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        try:
            after_sequence = int(last_event_id or "0")
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Last-Event-ID 必须是整数") from error
        if after_sequence < 0:
            raise HTTPException(status_code=400, detail="Last-Event-ID 不能为负数")

        async def events(stream_owner_id: str = owner_id) -> AsyncIterator[str]:
            async for item in service.events(review_id, stream_owner_id, after_sequence):
                yield (
                    f"id: {item.sequence}\n"
                    f"event: {item.event}\n"
                    f"data: {json.dumps(item.data, ensure_ascii=False)}\n\n"
                )

        return DisconnectAwareStreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if frontend_assets.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_assets), name="frontend-assets")

    @app.get("/", response_class=FileResponse)
    async def frontend_index_route() -> FileResponse:
        if not frontend_index.is_file():
            raise HTTPException(status_code=503, detail="frontend build is not available")

        return FileResponse(frontend_index, media_type="text/html")

    @app.get("/{full_path:path}", response_class=FileResponse, include_in_schema=False)
    async def frontend_spa_route(full_path: str) -> FileResponse:
        """Serve the SPA shell for client-side detail routes."""
        if full_path.startswith(("v1/", "health", "assets/")):
            raise HTTPException(status_code=404, detail="not found")
        if not frontend_index.is_file():
            raise HTTPException(status_code=503, detail="frontend build is not available")

        return FileResponse(frontend_index, media_type="text/html")

    return app


app = create_app()

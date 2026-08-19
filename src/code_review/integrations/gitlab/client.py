from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx

from code_review.integrations.gitlab.models import (
    ChangedRange,
    GitLabAccountProfile,
    GitLabAccountVerifyRequest,
    GitLabFileChange,
    GitLabMergeRequestPreview,
    GitLabPreviewRequest,
)

GitLabLanguage = Literal["python", "cpp"]
GitLabChangeType = Literal["added", "modified", "deleted", "renamed"]

_SUPPORTED_EXTENSIONS: dict[str, GitLabLanguage] = {
    ".py": "python",
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}
_MR_PATH_RE = re.compile(r"^/(.+)/-/merge_requests/(\d+)(?:/.*)?$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MAX_FILES = 200
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 10 * 1024 * 1024
_MAX_DIFF_CHARS = 40_000
_DOWNLOAD_BATCH_SIZE = 3


class GitLabIntegrationError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class _MergeRequestAddress:
    host: str
    project_path: str
    iid: int


class GitLabClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._download_limit = asyncio.Semaphore(6)

    async def verify_account(
        self,
        request: GitLabAccountVerifyRequest,
    ) -> GitLabAccountProfile:
        host = self._normalize_host(request.host)
        payload = await self._get_json(
            f"{host}/api/v4/user",
            headers={"PRIVATE-TOKEN": request.private_token},
        )
        if not isinstance(payload, dict):
            raise GitLabIntegrationError("GitLab 返回了无效的账户信息。")
        user_id = payload.get("id")
        username = str(payload.get("username") or "").strip()
        if not isinstance(user_id, int) or user_id < 1 or not username:
            raise GitLabIntegrationError("GitLab 账户信息不完整。")
        return GitLabAccountProfile(
            gitlab_host=host,
            user_id=user_id,
            username=username,
            name=str(payload.get("name") or username),
            avatar_url=str(payload["avatar_url"]) if payload.get("avatar_url") else None,
            web_url=str(payload["web_url"]) if payload.get("web_url") else None,
        )

    async def preview_merge_request(
        self,
        request: GitLabPreviewRequest,
    ) -> GitLabMergeRequestPreview:
        address = self._parse_merge_request_url(request.merge_request_url)
        headers = {"PRIVATE-TOKEN": request.private_token} if request.private_token else {}
        project = quote(address.project_path, safe="")
        api_base = f"{address.host}/api/v4/projects/{project}"
        merge_request = await self._get_json(
            f"{api_base}/merge_requests/{address.iid}",
            headers=headers,
        )
        if not isinstance(merge_request, dict):
            raise GitLabIntegrationError("GitLab 返回了无效的合并请求信息。")

        diff_refs = merge_request.get("diff_refs")
        if not isinstance(diff_refs, dict):
            raise GitLabIntegrationError("GitLab 合并请求尚未生成可用的 diff refs。", 409)
        base_sha = str(diff_refs.get("base_sha") or "")
        head_sha = str(diff_refs.get("head_sha") or merge_request.get("sha") or "")
        if not base_sha or not head_sha:
            raise GitLabIntegrationError("GitLab 合并请求缺少固定的 base/head SHA。", 409)

        raw_diffs = await self._list_diffs(
            api_base,
            address.iid,
            headers=headers,
        )
        total_bytes = 0
        budget_exhausted = False
        limited_changes: list[GitLabFileChange] = []
        for offset in range(0, len(raw_diffs), _DOWNLOAD_BATCH_SIZE):
            batch = raw_diffs[offset : offset + _DOWNLOAD_BATCH_SIZE]
            changes = await asyncio.gather(
                *(
                    self._resolve_change(
                        api_base,
                        raw,
                        base_sha=base_sha,
                        head_sha=head_sha,
                        headers=headers,
                        fetch_content=not budget_exhausted,
                    )
                    for raw in batch
                )
            )
            for change in changes:
                size = len((change.old_content or "").encode("utf-8"))
                size += len((change.new_content or "").encode("utf-8"))
                if budget_exhausted or total_bytes + size > _MAX_TOTAL_BYTES:
                    if size:
                        change = change.model_copy(
                            update={
                                "selectable": False,
                                "unavailable_reason": "本次导入内容超过 10 MB 总量限制",
                                "old_content": None,
                                "new_content": None,
                            }
                        )
                    budget_exhausted = True
                else:
                    total_bytes += size
                    budget_exhausted = total_bytes >= _MAX_TOTAL_BYTES
                limited_changes.append(change)

        return GitLabMergeRequestPreview(
            gitlab_host=address.host,
            project_id=int(merge_request.get("project_id") or 0),
            project_path=address.project_path,
            merge_request_iid=address.iid,
            title=str(merge_request.get("title") or f"MR !{address.iid}"),
            web_url=str(merge_request.get("web_url") or request.merge_request_url),
            base_sha=base_sha,
            head_sha=head_sha,
            files=limited_changes,
        )

    @staticmethod
    def _normalize_host(value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/")
        ):
            raise GitLabIntegrationError(
                "GitLab 地址应类似 https://gitlab.example.com。",
                400,
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _parse_merge_request_url(url: str) -> _MergeRequestAddress:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GitLabIntegrationError("请输入完整的 GitLab Merge Request URL。", 400)
        matched = _MR_PATH_RE.match(parsed.path.rstrip("/"))
        if matched is None:
            raise GitLabIntegrationError(
                "GitLab URL 应类似 https://gitlab.example.com/group/project/-/merge_requests/12。",
                400,
            )
        project_path, iid = matched.groups()
        return _MergeRequestAddress(
            host=f"{parsed.scheme}://{parsed.netloc}",
            project_path=project_path,
            iid=int(iid),
        )

    async def _list_diffs(
        self,
        api_base: str,
        iid: int,
        *,
        headers: dict[str, str],
    ) -> list[dict[str, object]]:
        collected: list[dict[str, object]] = []
        page = 1
        while len(collected) < _MAX_FILES:
            response = await self._request(
                "GET",
                f"{api_base}/merge_requests/{iid}/diffs",
                headers=headers,
                params={"page": page, "per_page": 100},
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise GitLabIntegrationError("GitLab 返回了无效的文件差异列表。")
            collected.extend(item for item in payload if isinstance(item, dict))
            next_page = response.headers.get("x-next-page", "").strip()
            if not next_page:
                break
            page = int(next_page)
        if len(collected) > _MAX_FILES:
            collected = collected[:_MAX_FILES]
        return collected

    async def _resolve_change(
        self,
        api_base: str,
        raw: dict[str, object],
        *,
        base_sha: str,
        head_sha: str,
        headers: dict[str, str],
        fetch_content: bool = True,
    ) -> GitLabFileChange:
        old_path = str(raw.get("old_path") or "")
        new_path = str(raw.get("new_path") or old_path)
        added = bool(raw.get("new_file"))
        deleted = bool(raw.get("deleted_file"))
        renamed = bool(raw.get("renamed_file"))
        change_type: GitLabChangeType = (
            "added" if added else "deleted" if deleted else "renamed" if renamed else "modified"
        )
        language = self._language_for(new_path or old_path)
        diff = str(raw.get("diff") or "")
        diff_truncated = bool(raw.get("collapsed") or raw.get("too_large"))
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS]
            diff_truncated = True

        reason: str | None = None
        if language is None:
            reason = "仅支持 Python 和 C/C++ 文件"
        elif bool(raw.get("generated_file")):
            reason = "生成文件默认不进入代码审查"
        elif diff_truncated:
            reason = "GitLab 未提供完整差异，无法进行精确的变更行审查"
        elif not fetch_content:
            reason = "本次导入内容超过 10 MB 总量限制"

        old_content: str | None = None
        new_content: str | None = None
        if reason is None:
            try:
                old_content, new_content = await asyncio.gather(
                    self._raw_file(
                        api_base,
                        old_path,
                        base_sha,
                        headers=headers,
                    )
                    if not added
                    else self._empty(),
                    self._raw_file(
                        api_base,
                        new_path,
                        head_sha,
                        headers=headers,
                    )
                    if not deleted
                    else self._empty(),
                )
            except GitLabIntegrationError as error:
                reason = str(error)

        selectable = reason is None and not deleted and new_content is not None
        if deleted and reason is None:
            reason = "删除文件可预览，但首版不作为新版本代码审查"
        return GitLabFileChange(
            old_path=old_path,
            new_path=new_path,
            change_type=change_type,
            language=language,
            old_content=old_content,
            new_content=new_content,
            diff=diff,
            changed_ranges=self._changed_ranges(diff),
            diff_truncated=diff_truncated,
            selectable=selectable,
            unavailable_reason=reason,
        )

    async def _raw_file(
        self,
        api_base: str,
        path: str,
        ref: str,
        *,
        headers: dict[str, str],
    ) -> str:
        encoded_path = quote(path, safe="")
        async with self._download_limit:
            response = await self._request(
                "GET",
                f"{api_base}/repository/files/{encoded_path}/raw",
                headers=headers,
                params={"ref": ref},
            )
        content = response.content
        if len(content) > _MAX_FILE_BYTES:
            raise GitLabIntegrationError("文件超过 2 MB，未下载")
        if b"\x00" in content:
            raise GitLabIntegrationError("二进制文件不进入代码审查")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GitLabIntegrationError("文件不是 UTF-8 文本，未导入") from error

    @staticmethod
    async def _empty() -> None:
        return None

    @staticmethod
    def _language_for(path: str) -> GitLabLanguage | None:
        lowered = path.casefold()
        return next(
            (
                language
                for suffix, language in _SUPPORTED_EXTENSIONS.items()
                if lowered.endswith(suffix)
            ),
            None,
        )

    @staticmethod
    def _changed_ranges(diff: str) -> list[ChangedRange]:
        ranges: list[ChangedRange] = []
        for line in diff.splitlines():
            matched = _HUNK_RE.match(line)
            if matched is None:
                continue
            start = int(matched.group(1))
            count = int(matched.group(2) or "1")
            if count > 0:
                ranges.append(ChangedRange(start_line=start, end_line=start + count - 1))
        return ranges

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> object:
        return (await self._request("GET", url, headers=headers)).json()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
            )
        except httpx.TimeoutException as error:
            raise GitLabIntegrationError("连接 GitLab 超时，请稍后重试。", 504) from error
        except httpx.HTTPError as error:
            raise GitLabIntegrationError("无法连接 GitLab 服务。", 502) from error
        if response.status_code == 401:
            raise GitLabIntegrationError("GitLab 令牌无效或已过期。", 401)
        if response.status_code == 403:
            raise GitLabIntegrationError("当前 GitLab 令牌没有读取该项目的权限。", 403)
        if response.status_code == 404:
            raise GitLabIntegrationError("GitLab 项目、合并请求或文件不存在。", 404)
        if response.status_code == 429:
            raise GitLabIntegrationError("GitLab 请求过于频繁，请稍后重试。", 429)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise GitLabIntegrationError(
                f"GitLab 请求失败（HTTP {response.status_code}）。",
                502,
            ) from error
        return response

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GitLabPreviewRequest(BaseModel):
    merge_request_url: str = Field(min_length=1, max_length=2000)
    private_token: str | None = Field(default=None, max_length=1000)


class GitLabAccountVerifyRequest(BaseModel):
    host: str = Field(min_length=1, max_length=2000)
    private_token: str = Field(min_length=1, max_length=1000)


class GitLabAccountProfile(BaseModel):
    gitlab_host: str
    user_id: int = Field(ge=1)
    username: str
    name: str
    avatar_url: str | None = None
    web_url: str | None = None


class ChangedRange(BaseModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class GitLabFileChange(BaseModel):
    old_path: str
    new_path: str
    change_type: Literal["added", "modified", "deleted", "renamed"]
    language: Literal["python", "cpp"] | None = None
    old_content: str | None = None
    new_content: str | None = None
    diff: str = ""
    changed_ranges: list[ChangedRange] = Field(default_factory=list)
    diff_truncated: bool = False
    selectable: bool = False
    unavailable_reason: str | None = None


class GitLabMergeRequestPreview(BaseModel):
    gitlab_host: str
    project_id: int
    project_path: str
    merge_request_iid: int
    title: str
    web_url: str
    base_sha: str
    head_sha: str
    files: list[GitLabFileChange]

from pydantic import BaseModel, Field

from code_review.domain.review_models import ChangedLineRange, Language


class LocalDiffFileInput(BaseModel):
    filename: str = Field(min_length=1, max_length=1000)
    content: str


class LocalDiffPreviewRequest(BaseModel):
    old_file: LocalDiffFileInput
    new_file: LocalDiffFileInput


class LocalDiffFileChange(BaseModel):
    old_path: str
    new_path: str
    change_type: str
    language: Language | None = None
    old_content: str
    new_content: str
    old_sha256: str = Field(min_length=64, max_length=64)
    new_sha256: str = Field(min_length=64, max_length=64)
    diff: str
    changed_ranges: list[ChangedLineRange] = Field(default_factory=list)
    diff_truncated: bool = False
    selectable: bool = False
    unavailable_reason: str | None = None


class LocalDiffPreview(BaseModel):
    old_label: str
    new_label: str
    files: list[LocalDiffFileChange]

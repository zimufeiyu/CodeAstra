from __future__ import annotations

import difflib
import hashlib
from pathlib import PurePosixPath

from code_review.domain.review_models import ChangedLineRange, Language
from code_review.integrations.local_diff.models import (
    LocalDiffFileChange,
    LocalDiffFileInput,
    LocalDiffPreview,
    LocalDiffPreviewRequest,
)

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_DIFF_CHARS = 40_000
_SUPPORTED_EXTENSIONS: dict[str, Language] = {
    ".py": "python",
    ".pyw": "python",
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}


class LocalDiffError(ValueError):
    pass


class LocalDiffService:
    def preview(self, request: LocalDiffPreviewRequest) -> LocalDiffPreview:
        change = self.compare(request.old_file, request.new_file)
        return LocalDiffPreview(
            old_label=change.old_path,
            new_label=change.new_path,
            files=[change],
        )

    def compare(
        self,
        old_file: LocalDiffFileInput,
        new_file: LocalDiffFileInput,
    ) -> LocalDiffFileChange:
        old_path = self._normalize_path(old_file.filename)
        new_path = self._normalize_path(new_file.filename)
        old_bytes = old_file.content.encode("utf-8")
        new_bytes = new_file.content.encode("utf-8")
        if len(old_bytes) > _MAX_FILE_BYTES or len(new_bytes) > _MAX_FILE_BYTES:
            raise LocalDiffError("单个本地版本文件不能超过 2 MB")

        language = self._language_for(new_path)
        reason: str | None = None
        if language is None:
            reason = "仅支持 Python 和 C/C++ 文件"

        changed_ranges = self._changed_ranges(old_file.content, new_file.content)
        if reason is None and not changed_ranges:
            reason = "两个本地版本内容相同，没有可审查的变更"

        raw_diff = "".join(
            difflib.unified_diff(
                old_file.content.splitlines(keepends=True),
                new_file.content.splitlines(keepends=True),
                fromfile=old_path,
                tofile=new_path,
            )
        )
        diff_truncated = len(raw_diff) > _MAX_DIFF_CHARS
        diff = raw_diff[:_MAX_DIFF_CHARS] if diff_truncated else raw_diff
        change_type = "renamed" if old_path != new_path else "modified"
        return LocalDiffFileChange(
            old_path=old_path,
            new_path=new_path,
            change_type=change_type,
            language=language,
            old_content=old_file.content,
            new_content=new_file.content,
            old_sha256=hashlib.sha256(old_bytes).hexdigest(),
            new_sha256=hashlib.sha256(new_bytes).hexdigest(),
            diff=diff,
            changed_ranges=changed_ranges,
            diff_truncated=diff_truncated,
            selectable=reason is None,
            unavailable_reason=reason,
        )

    @staticmethod
    def _normalize_path(value: str) -> str:
        normalized = value.strip().replace("\\", "/").lstrip("/")
        path = PurePosixPath(normalized)
        if not normalized or any(part in {"", ".", ".."} for part in path.parts):
            raise LocalDiffError("本地版本文件名无效")
        return str(path)

    @staticmethod
    def _language_for(path: str) -> Language | None:
        lowered = path.lower()
        return next(
            (
                language
                for suffix, language in _SUPPORTED_EXTENSIONS.items()
                if lowered.endswith(suffix)
            ),
            None,
        )

    @staticmethod
    def _changed_ranges(old_content: str, new_content: str) -> list[ChangedLineRange]:
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        raw_ranges: list[tuple[int, int]] = []
        for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            if new_start < new_end:
                raw_ranges.append((new_start + 1, new_end))
            else:
                anchor = min(max(1, new_start + 1), max(1, len(new_lines)))
                raw_ranges.append((anchor, anchor))

        merged: list[tuple[int, int]] = []
        for start, end in raw_ranges:
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return [ChangedLineRange(start_line=start, end_line=end) for start, end in merged]

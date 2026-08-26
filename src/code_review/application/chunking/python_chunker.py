from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable

from code_review.application.python_analysis_cache import parse_python_source
from code_review.domain.review_chunks import ReviewChunk, ReviewPlanningError
from code_review.domain.review_models import SourceFile


class PythonSyntaxChunker:
    def split(
        self,
        review_id: str,
        source: SourceFile,
        *,
        parent_chunk_id: str | None = None,
        split_depth: int = 0,
    ) -> list[ReviewChunk]:
        try:
            module = parse_python_source(source)
        except SyntaxError as error:
            raise ReviewPlanningError(
                message=f"Python 语法解析失败：第 {error.lineno or 1} 行。",
                file_id=source.file_id,
                lines=[error.lineno or 1],
            ) from error

        ranges: list[tuple[int, int]] = []
        pending: list[ast.stmt] = []
        for statement in module.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                ranges.extend(self._pending_range(pending))
                pending = []
                ranges.append(self._statement_range(statement))
            else:
                pending.append(statement)
        ranges.extend(self._pending_range(pending))
        return [
            self._chunk(
                review_id,
                source,
                start,
                end,
                index,
                parent_chunk_id=parent_chunk_id,
                split_depth=split_depth,
            )
            for index, (start, end) in enumerate(ranges)
        ]

    @staticmethod
    def _pending_range(statements: Iterable[ast.stmt]) -> list[tuple[int, int]]:
        items = list(statements)
        if not items:
            return []
        return [
            (
                min(PythonSyntaxChunker._statement_range(item)[0] for item in items),
                max(PythonSyntaxChunker._statement_range(item)[1] for item in items),
            )
        ]

    @staticmethod
    def _statement_range(statement: ast.stmt) -> tuple[int, int]:
        decorators = getattr(statement, "decorator_list", [])
        start = min([statement.lineno, *(decorator.lineno for decorator in decorators)])
        return start, statement.end_lineno or statement.lineno

    @staticmethod
    def _chunk(
        review_id: str,
        source: SourceFile,
        start: int,
        end: int,
        index: int,
        *,
        parent_chunk_id: str | None,
        split_depth: int,
    ) -> ReviewChunk:
        code = "".join(source.content.splitlines(keepends=True)[start - 1 : end]).rstrip("\n")
        material = f"{source.sha256}:{start}:{end}:{code}".encode()
        digest = hashlib.sha256(material).hexdigest()
        return ReviewChunk(
            chunk_id=f"{review_id}:{source.file_id}:{start}-{end}:{index}",
            review_id=review_id,
            language="python",
            target_file_id=source.file_id,
            target_path=source.relative_path,
            target_start_line=start,
            target_end_line=end,
            target_code=code,
            content_fingerprint=digest,
            parent_chunk_id=parent_chunk_id,
            split_depth=split_depth,
        )

from __future__ import annotations

import hashlib

from code_review.application.chunking.cpp_chunker import CppSyntaxChunker
from code_review.application.chunking.dependency_resolver import DependencyResolver
from code_review.application.chunking.python_chunker import PythonSyntaxChunker
from code_review.domain.review_chunks import (
    ChunkStatus,
    ReviewChunk,
    ReviewPlan,
    ReviewPlanningError,
)
from code_review.domain.review_models import SourceFile


class ReviewPlanner:
    def __init__(self) -> None:
        self._python = PythonSyntaxChunker()
        self._cpp = CppSyntaxChunker()
        self._dependencies = DependencyResolver()

    def plan(self, review_id: str, files: list[SourceFile]) -> ReviewPlan:
        chunks: list[ReviewChunk] = []
        significant: dict[str, set[int]] = {}
        zero_target: set[str] = set()
        for source in files:
            significant[source.file_id] = significant_lines(source)
            if source.language == "python":
                file_chunks = self._python.split(review_id, source)
            elif source.language == "cpp":
                file_chunks = self._cpp.split(review_id, source)
            else:
                raise ReviewPlanningError(
                    message=f"不支持的语言：{source.language}",
                    file_id=source.file_id,
                )
            if not file_chunks:
                if significant[source.file_id]:
                    raise ReviewPlanningError(
                        file_id=source.file_id,
                        lines=sorted(significant[source.file_id]),
                    )
                zero_target.add(source.file_id)
            chunks.extend(file_chunks)

        chunks = [
            chunk.model_copy(
                update={"context_references": self._dependencies.resolve(chunk, files)}
            )
            for chunk in chunks
        ]
        for source in files:
            covered = {
                line
                for chunk in chunks
                if chunk.target_file_id == source.file_id
                for line in range(chunk.target_start_line, chunk.target_end_line + 1)
            }
            missing = significant[source.file_id] - covered
            if missing:
                raise ReviewPlanningError(
                    file_id=source.file_id,
                    lines=sorted(missing),
                )
        return ReviewPlan(
            review_id=review_id,
            chunks=chunks,
            significant_lines=significant,
            zero_target_file_ids=zero_target,
        )

    def split(self, chunk: ReviewChunk) -> list[ReviewChunk]:
        lines = chunk.target_code.splitlines(keepends=True)
        if len(lines) < 2:
            return []
        middle = len(lines) // 2
        ranges = [
            (chunk.target_start_line, chunk.target_start_line + middle - 1, lines[:middle]),
            (chunk.target_start_line + middle, chunk.target_end_line, lines[middle:]),
        ]
        children: list[ReviewChunk] = []
        for index, (start, end, body) in enumerate(ranges):
            code = "".join(body).rstrip("\n")
            digest = hashlib.sha256(
                f"{chunk.content_fingerprint}:{start}:{end}:{code}".encode()
            ).hexdigest()
            children.append(
                chunk.model_copy(
                    update={
                        "chunk_id": f"{chunk.chunk_id}.{index + 1}",
                        "target_start_line": start,
                        "target_end_line": end,
                        "target_code": code,
                        "content_fingerprint": digest,
                        "context_references": chunk.context_references,
                        "parent_chunk_id": chunk.chunk_id,
                        "split_depth": chunk.split_depth + 1,
                        "attempt_count": 0,
                        "status": ChunkStatus.PENDING,
                        "error_code": None,
                        "error_message": None,
                    }
                )
            )
        return children


def significant_lines(source: SourceFile) -> set[int]:
    result: set[int] = set()
    in_block_comment = False
    for number, raw_line in enumerate(source.content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if source.language == "python":
            if not line.startswith("#"):
                result.add(number)
            continue
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
                line = line.split("*/", 1)[1].strip()
            else:
                continue
        while line.startswith("/*"):
            if "*/" not in line[2:]:
                in_block_comment = True
                line = ""
                break
            line = line.split("*/", 1)[1].strip()
        if line and not line.startswith("//"):
            result.add(number)
    return result

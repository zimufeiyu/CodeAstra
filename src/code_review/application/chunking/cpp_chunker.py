from __future__ import annotations

import hashlib

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

from code_review.domain.review_chunks import ReviewChunk, ReviewPlanningError
from code_review.domain.review_models import SourceFile

CPP_LANGUAGE = Language(tree_sitter_cpp.language())
TOP_LEVEL_TYPES = {
    "namespace_definition",
    "class_specifier",
    "struct_specifier",
    "function_definition",
    "template_declaration",
}


class CppSyntaxChunker:
    def __init__(self, *, maximum_error_ratio: float = 0.15) -> None:
        self._parser = Parser(CPP_LANGUAGE)
        self._maximum_error_ratio = maximum_error_ratio

    def split(
        self,
        review_id: str,
        source: SourceFile,
        *,
        parent_chunk_id: str | None = None,
        split_depth: int = 0,
    ) -> list[ReviewChunk]:
        source_bytes = source.content.encode("utf-8")
        tree = self._parser.parse(source_bytes)
        error_ranges = self._error_byte_ranges(tree.root_node)
        error_bytes = sum(max(1, end - start) for start, end in error_ranges)
        error_ratio = min(1.0, error_bytes / max(1, len(source_bytes)))
        if tree.root_node.has_error and error_ratio > self._maximum_error_ratio:
            raise ReviewPlanningError(
                message="C++ 语法错误过多，无法保证完整分块。",
                file_id=source.file_id,
            )

        nodes = [node for node in tree.root_node.named_children if self._has_content(node, source)]
        return [
            self._chunk(
                review_id,
                source,
                node,
                index,
                parent_chunk_id=parent_chunk_id,
                split_depth=split_depth,
            )
            for index, node in enumerate(nodes)
        ]

    @staticmethod
    def _has_content(node: Node, source: SourceFile) -> bool:
        text = source.content.encode("utf-8")[node.start_byte : node.end_byte]
        return bool(text.strip())

    @classmethod
    def _error_byte_ranges(cls, node: Node) -> list[tuple[int, int]]:
        if node.is_error:
            return [(node.start_byte, node.end_byte)]
        ranges: list[tuple[int, int]] = []
        if node.is_missing:
            ranges.append((node.start_byte, node.end_byte))
        for child in node.children:
            ranges.extend(cls._error_byte_ranges(child))
        return ranges

    @staticmethod
    def _chunk(
        review_id: str,
        source: SourceFile,
        node: Node,
        index: int,
        *,
        parent_chunk_id: str | None,
        split_depth: int,
    ) -> ReviewChunk:
        start = node.start_point.row + 1
        end = node.end_point.row + 1
        if node.end_point.column == 0 and end > start:
            end -= 1
        code = source.content.encode("utf-8")[node.start_byte : node.end_byte].decode("utf-8")
        material = f"{source.sha256}:{start}:{end}:{code}".encode()
        digest = hashlib.sha256(material).hexdigest()
        return ReviewChunk(
            chunk_id=f"{review_id}:{source.file_id}:{start}-{end}:{index}",
            review_id=review_id,
            language="cpp",
            target_file_id=source.file_id,
            target_path=source.relative_path,
            target_start_line=start,
            target_end_line=end,
            target_code=code.rstrip("\n"),
            content_fingerprint=digest,
            parent_chunk_id=parent_chunk_id,
            split_depth=split_depth,
        )

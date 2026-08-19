from __future__ import annotations

import ast
import posixpath
import re

from code_review.domain.review_chunks import ContextReference, ReviewChunk
from code_review.domain.review_models import SourceFile

_CPP_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)


class DependencyResolver:
    def resolve(
        self,
        chunk: ReviewChunk,
        files: list[SourceFile],
    ) -> list[ContextReference]:
        by_path = {posixpath.normpath(item.relative_path): item for item in files}
        target = next(
            (item for item in files if item.file_id == chunk.target_file_id),
            None,
        )
        if target is None:
            return []
        if target.language == "python":
            paths = self._python_dependencies(target, by_path)
        else:
            paths = self._cpp_dependencies(target, by_path)
        references = [
            self._reference(by_path[path], reason, rank)
            for path, reason, rank in paths
            if by_path[path].file_id != chunk.target_file_id
        ]
        return sorted(references, key=lambda item: (item.rank, item.path))

    @staticmethod
    def _python_dependencies(
        source: SourceFile,
        by_path: dict[str, SourceFile],
    ) -> list[tuple[str, str, int]]:
        try:
            module = ast.parse(source.content)
        except SyntaxError:
            return []
        modules: set[str] = set()
        for statement in module.body:
            if isinstance(statement, ast.Import):
                modules.update(alias.name for alias in statement.names)
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                modules.add(statement.module)
        result: list[tuple[str, str, int]] = []
        for module_name in sorted(modules):
            candidate = module_name.replace(".", "/")
            for path in (f"{candidate}.py", f"{candidate}/__init__.py"):
                if path in by_path:
                    result.append((path, "exact import", 0))
                    break
        return result

    @staticmethod
    def _cpp_dependencies(
        source: SourceFile,
        by_path: dict[str, SourceFile],
    ) -> list[tuple[str, str, int]]:
        directory = posixpath.dirname(posixpath.normpath(source.relative_path))
        result: list[tuple[str, str, int]] = []
        for include in _CPP_INCLUDE.findall(source.content):
            candidate = posixpath.normpath(posixpath.join(directory, include))
            if candidate in by_path:
                result.append((candidate, "exact include", 0))
                continue
            suffix = include.lstrip("./")
            matches = [path for path in by_path if path.endswith(suffix)]
            if len(matches) == 1:
                result.append((matches[0], "include suffix", 10))
        return result

    @staticmethod
    def _reference(
        source: SourceFile,
        reason: str,
        rank: int,
    ) -> ContextReference:
        return ContextReference(
            file_id=source.file_id,
            path=source.relative_path,
            start_line=1,
            end_line=max(1, len(source.content.splitlines())),
            code=source.content,
            reason=reason,
            rank=rank,
        )

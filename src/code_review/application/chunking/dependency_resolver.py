from __future__ import annotations

import ast
import posixpath
import re

from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.python_analysis_cache import parse_python_source
from code_review.application.repository_graph import RepositoryGraph
from code_review.domain.review_chunks import ContextReference, ReviewChunk
from code_review.domain.review_models import SourceFile

_CPP_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)


class DependencyResolver:
    def __init__(self, metrics: PipelineMetrics | None = None) -> None:
        self._graph = RepositoryGraph(metrics)

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
            return self._graph.context_for(chunk, files)
        else:
            paths = self._cpp_dependencies(target, by_path)
        references = [
            self._reference(by_path[path], reason, rank)
            for path, reason, rank in paths
            if by_path[path].file_id != chunk.target_file_id
        ]
        return sorted(references, key=lambda item: (item.rank, item.path))

    @staticmethod
    def _local_python_imports(source: SourceFile) -> ContextReference | None:
        try:
            module = parse_python_source(source)
        except SyntaxError:
            return None
        imports = [
            statement
            for statement in module.body
            if isinstance(statement, (ast.Import, ast.ImportFrom))
        ]
        if not imports:
            return None
        lines = source.content.splitlines(keepends=True)
        selected = sorted(
            {
                line
                for statement in imports
                for line in range(statement.lineno, (statement.end_lineno or statement.lineno) + 1)
            }
        )
        return ContextReference(
            file_id=source.file_id,
            path=source.relative_path,
            start_line=selected[0],
            end_line=selected[-1],
            code="".join(f"{line}: {lines[line - 1]}" for line in selected),
            reason="target imports",
            rank=0,
        )

    @staticmethod
    def _python_dependencies(
        source: SourceFile,
        by_path: dict[str, SourceFile],
    ) -> list[tuple[str, str, int]]:
        try:
            module = parse_python_source(source)
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
        code = source.content
        start_line = 1
        end_line = max(1, len(source.content.splitlines()))
        if source.language == "python":
            try:
                module = parse_python_source(source)
            except SyntaxError:
                code = ""
            else:
                lines = source.content.splitlines(keepends=True)
                selected: set[int] = set()

                def select_signature(
                    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
                ) -> None:
                    body_line = node.body[0].lineno if node.body else node.lineno + 1
                    selected.update(range(node.lineno, max(node.lineno, body_line - 1) + 1))

                for statement in module.body:
                    if isinstance(statement, (ast.Import, ast.ImportFrom)):
                        selected.update(
                            range(statement.lineno, (statement.end_lineno or statement.lineno) + 1)
                        )
                    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        select_signature(statement)
                    elif isinstance(statement, ast.ClassDef):
                        select_signature(statement)
                        for member in statement.body:
                            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                select_signature(member)
                if selected:
                    start_line = min(selected)
                    end_line = max(selected)
                    code = "".join(
                        f"{line}: {lines[line - 1]}"
                        for line in sorted(selected)
                        if 1 <= line <= len(lines)
                    )
                else:
                    code = ""
        return ContextReference(
            file_id=source.file_id,
            path=source.relative_path,
            start_line=start_line,
            end_line=end_line,
            code=code,
            reason=reason,
            rank=rank,
        )

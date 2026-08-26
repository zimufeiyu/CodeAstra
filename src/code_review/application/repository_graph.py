from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.python_analysis_cache import parse_python_source
from code_review.domain.review_chunks import ContextReference, ReviewChunk
from code_review.domain.review_models import SourceFile


@dataclass(frozen=True)
class SymbolNode:
    file_id: str
    path: str
    qualified_name: str
    name: str
    kind: str
    start_line: int
    end_line: int
    signature_end_line: int
    calls: frozenset[str]
    bases: frozenset[str]


@dataclass(frozen=True)
class RepositoryGraphData:
    revision: str
    symbols: tuple[SymbolNode, ...]
    imports: dict[str, tuple[int, ...]]
    edge_count: int


class RepositoryGraph:
    def __init__(self, metrics: PipelineMetrics | None = None) -> None:
        self._metrics = metrics or PipelineMetrics()
        self._cache: dict[str, RepositoryGraphData] = {}

    @staticmethod
    def revision(files: list[SourceFile]) -> str:
        state = "\x1f".join(
            f"{item.relative_path}:{item.sha256}" for item in sorted(files, key=lambda x: x.file_id)
        )
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    def build(self, files: list[SourceFile]) -> RepositoryGraphData:
        revision = self.revision(files)
        cached = self._cache.get(revision)
        if cached is not None:
            self._metrics.increment("repo_graph_cache_hits")
            return cached
        self._metrics.increment("repo_graph_cache_misses")
        symbols: list[SymbolNode] = []
        imports: dict[str, tuple[int, ...]] = {}
        for source in files:
            if source.language != "python":
                continue
            try:
                tree = parse_python_source(source)
            except SyntaxError:
                continue
            imports[source.file_id] = tuple(
                line
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
            )
            parents = {
                child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                parent = parents.get(node)
                owner = parent.name if isinstance(parent, ast.ClassDef) else None
                qualified = f"{owner}.{node.name}" if owner else node.name
                body = getattr(node, "body", [])
                signature_end = max(node.lineno, (body[0].lineno - 1) if body else node.lineno)
                calls = frozenset(
                    child.func.id
                    if isinstance(child.func, ast.Name)
                    else child.func.attr
                    if isinstance(child.func, ast.Attribute)
                    else ""
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                ) - {""}
                bases = (
                    frozenset(
                        base.id
                        if isinstance(base, ast.Name)
                        else base.attr
                        if isinstance(base, ast.Attribute)
                        else ""
                        for base in node.bases
                    )
                    - {""}
                    if isinstance(node, ast.ClassDef)
                    else frozenset()
                )
                symbols.append(
                    SymbolNode(
                        file_id=source.file_id,
                        path=source.relative_path,
                        qualified_name=qualified,
                        name=node.name,
                        kind=type(node).__name__,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        signature_end_line=signature_end,
                        calls=calls,
                        bases=bases,
                    )
                )
        edges = sum(len(item.calls) + len(item.bases) for item in symbols)
        data = RepositoryGraphData(revision, tuple(symbols), imports, edges)
        self._cache[revision] = data
        while len(self._cache) > 32:
            self._cache.pop(next(iter(self._cache)))
        self._metrics.increment("repo_graph_nodes", len(symbols))
        self._metrics.increment("repo_graph_edges", edges)
        return data

    def context_for(
        self,
        chunk: ReviewChunk,
        files: list[SourceFile],
        *,
        max_references: int = 12,
    ) -> list[ContextReference]:
        graph = self.build(files)
        by_id = {item.file_id: item for item in files}
        enclosing = [
            item
            for item in graph.symbols
            if item.file_id == chunk.target_file_id
            and item.start_line <= chunk.target_start_line <= item.end_line
        ]
        target = (
            min(enclosing, key=lambda item: item.end_line - item.start_line)
            if enclosing
            else None
        )
        candidates: list[tuple[int, str, SymbolNode]] = []
        if target is not None:
            for symbol in graph.symbols:
                if symbol == target:
                    continue
                if symbol.name in target.calls:
                    candidates.append((10, "callee signature", symbol))
                elif target.name in symbol.calls:
                    candidates.append((20, "caller signature", symbol))
                elif symbol.name == target.name:
                    candidates.append((5, "override signature", symbol))
        references: list[ContextReference] = []
        source = by_id.get(chunk.target_file_id)
        import_lines = graph.imports.get(chunk.target_file_id, ())
        if source is not None and import_lines:
            lines = source.content.splitlines(keepends=True)
            references.append(
                ContextReference(
                    file_id=source.file_id,
                    path=source.relative_path,
                    start_line=min(import_lines),
                    end_line=max(import_lines),
                    code="".join(f"{line}: {lines[line - 1]}" for line in import_lines),
                    reason="target imports",
                    rank=0,
                )
            )
        seen: set[tuple[str, int]] = set()
        for rank, reason, symbol in sorted(candidates, key=lambda item: (item[0], item[2].path)):
            key = symbol.file_id, symbol.start_line
            source = by_id.get(symbol.file_id)
            if key in seen or source is None:
                continue
            seen.add(key)
            lines = source.content.splitlines(keepends=True)
            references.append(
                ContextReference(
                    file_id=symbol.file_id,
                    path=symbol.path,
                    start_line=symbol.start_line,
                    end_line=symbol.signature_end_line,
                    code="".join(
                        f"{line}: {lines[line - 1]}"
                        for line in range(symbol.start_line, symbol.signature_end_line + 1)
                    ),
                    reason=reason,
                    rank=rank,
                )
            )
            if len(references) >= max_references:
                break
        self._metrics.increment("repo_graph_context_references", len(references))
        return references

    def capability(self, files: list[SourceFile]) -> dict[str, int | str]:
        graph = self.build(files)
        return {
            "status": "available",
            "revision": graph.revision[:16],
            "node_count": len(graph.symbols),
            "edge_count": graph.edge_count,
        }

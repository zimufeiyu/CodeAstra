from code_review.application.pipeline_metrics import PipelineMetrics
from code_review.application.repository_graph import RepositoryGraph
from code_review.application.review_planner import ReviewPlanner
from code_review.domain.review_models import SourceFile


def source(file_id: str, path: str, content: str) -> SourceFile:
    return SourceFile.from_content(
        file_id=file_id, relative_path=path, language="python", content=content
    )


def test_repo_graph_reuses_sha_ast_and_selects_only_ranked_symbol_context():
    files = [
        source(
            "base",
            "base.py",
            "class Base:\n    def run(self, value):\n        return helper(value)\n",
        ),
        source("helper", "helpers.py", "def helper(value):\n    return value + 1\n"),
        source(
            "child",
            "child.py",
            "from base import Base\n"
            "class Child(Base):\n"
            "    def run(self, value):\n"
            "        return value\n",
        ),
    ]
    metrics = PipelineMetrics()
    planner = ReviewPlanner(metrics)
    plan = planner.plan("review", files)
    target = next(item for item in plan.chunks if item.target_file_id == "base").model_copy(
        update={"target_start_line": 2, "target_end_line": 3}
    )
    references = RepositoryGraph(metrics).context_for(target, files)
    assert any(
        item.reason == "callee signature" and item.path == "helpers.py"
        for item in references
    )
    assert any(
        item.reason == "override signature" and item.path == "child.py"
        for item in references
    )
    assert all("return value + 1" not in item.code for item in references)
    graph = RepositoryGraph(metrics)
    first = graph.capability(files)
    second = graph.capability(files)
    assert first == second and first["node_count"] >= 4
    snapshot = metrics.snapshot()
    assert snapshot["repo_graph_cache_hits"] >= 1
    assert snapshot["repo_graph_cache_misses"] >= 1

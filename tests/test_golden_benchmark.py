import json

import pytest

from code_review.application.benchmark import (
    GoldenBenchmarkRunner,
    built_in_golden_cases,
)


@pytest.mark.asyncio
async def test_builtin_golden_benchmark_is_source_free_and_perfect_for_supported_rules():
    report = await GoldenBenchmarkRunner().run(built_in_golden_cases(), suite="test")
    assert report.metrics.precision == 1
    assert report.metrics.recall == 1
    assert report.metrics.false_positive_rate == 0
    assert report.metrics.finding_location_accuracy == 1
    assert report.metrics.actionable_rate == 1
    assert report.metrics.candidate_parse_rate == 1
    assert report.metrics.patch_apply_rate == 1
    assert report.metrics.fingerprint_removed_rate == 1
    assert report.metrics.new_diagnostic_regression_rate == 1
    assert report.metrics.model_calls == 0
    assert report.failures == []
    payload = json.loads(report.model_dump_json())
    assert "content" not in json.dumps(payload)
    assert "# CodeAstra Golden Benchmark" in report.markdown()

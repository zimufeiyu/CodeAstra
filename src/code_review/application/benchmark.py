from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, Field

from code_review.application.static_analysis import StaticAnalyzer
from code_review.domain.review_models import SourceFile


class GoldenExpectation(BaseModel):
    rule_id: str
    relative_path: str
    line: int = Field(ge=1)
    actionable: bool = True


class GoldenCase(BaseModel):
    case_id: str
    category: str
    files: list[SourceFile]
    expected: list[GoldenExpectation] = Field(default_factory=list)
    candidate_parse_ok: bool | None = None
    patch_apply_ok: bool | None = None
    fingerprint_removed: bool | None = None
    new_diagnostic_count: int | None = Field(default=None, ge=0)


class BenchmarkMetrics(BaseModel):
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float
    false_positive_rate: float
    finding_location_accuracy: float
    actionable_rate: float
    candidate_parse_rate: float
    patch_apply_rate: float
    fingerprint_removed_rate: float
    new_diagnostic_regression_rate: float
    model_calls: int
    prompt_tokens: int
    elapsed_ms: int


class BenchmarkReport(BaseModel):
    schema_version: str = "1.0"
    suite: str
    metrics: BenchmarkMetrics
    case_count: int
    capabilities: dict[str, str]
    failures: list[str] = Field(default_factory=list)

    def markdown(self) -> str:
        metrics = self.metrics
        rows = [
            ("Precision", metrics.precision),
            ("Recall", metrics.recall),
            ("False-positive rate", metrics.false_positive_rate),
            ("Location accuracy", metrics.finding_location_accuracy),
            ("Actionable rate", metrics.actionable_rate),
            ("Candidate parse rate", metrics.candidate_parse_rate),
            ("Patch apply rate", metrics.patch_apply_rate),
            ("Fingerprint removed", metrics.fingerprint_removed_rate),
            ("New diagnostic regression", metrics.new_diagnostic_regression_rate),
        ]
        lines = [
            f"# CodeAstra Golden Benchmark — {self.suite}",
            "",
            f"- Cases: {self.case_count}",
            f"- Model calls: {metrics.model_calls}",
            f"- Prompt tokens: {metrics.prompt_tokens}",
            f"- Elapsed: {metrics.elapsed_ms} ms",
            "",
            "| Metric | Value |",
            "|---|---:|",
            *(f"| {name} | {value:.3f} |" for name, value in rows),
            "",
            "## Capabilities",
            "",
            *(f"- {name}: {state}" for name, state in sorted(self.capabilities.items())),
        ]
        if self.failures:
            lines.extend(["", "## Failures", "", *(f"- {item}" for item in self.failures)])
        return "\n".join(lines) + "\n"


@dataclass
class _Totals:
    expected: set[tuple[str, str, str, int]] = field(default_factory=set)
    actual: set[tuple[str, str, str, int]] = field(default_factory=set)
    actionable: int = 0
    finding_count: int = 0
    candidate_parse: list[bool] = field(default_factory=list)
    patch_apply: list[bool] = field(default_factory=list)
    fingerprints: list[bool] = field(default_factory=list)
    diagnostic_regression: list[bool] = field(default_factory=list)


class GoldenBenchmarkRunner:
    def __init__(self, analyzer: StaticAnalyzer | None = None) -> None:
        self._analyzer = analyzer or StaticAnalyzer()

    async def run(self, cases: list[GoldenCase], *, suite: str = "offline") -> BenchmarkReport:
        started = perf_counter()
        totals = _Totals()
        failures: list[str] = []
        capabilities: dict[str, str] = {}
        for case in cases:
            result = await self._analyzer.analyze(case.files, external_standard=False)
            for coverage in result.coverage:
                capabilities[coverage.language] = (
                    "available" if coverage.available else "unavailable"
                )
            expected = {
                (item.rule_id, item.relative_path, item.line) for item in case.expected
            }
            actual = {
                (finding.rule_id, finding.relative_path or finding.file_id, finding.start_line)
                for finding in result.findings
            }
            totals.expected.update((case.case_id, *item) for item in expected)
            totals.actual.update((case.case_id, *item) for item in actual)
            totals.finding_count += len(result.findings)
            totals.actionable += sum(
                item.applicability != "unavailable" for item in result.findings
            )
            for missing in sorted(expected - actual):
                failures.append(
                    f"{case.case_id}: missing {missing[0]} at {missing[1]}:{missing[2]}"
                )
            for unexpected in sorted(actual - expected):
                failures.append(
                    f"{case.case_id}: unexpected {unexpected[0]} at {unexpected[1]}:{unexpected[2]}"
                )
            if case.candidate_parse_ok is not None:
                totals.candidate_parse.append(case.candidate_parse_ok)
            if case.patch_apply_ok is not None:
                totals.patch_apply.append(case.patch_apply_ok)
            if case.fingerprint_removed is not None:
                totals.fingerprints.append(case.fingerprint_removed)
            if case.new_diagnostic_count is not None:
                totals.diagnostic_regression.append(case.new_diagnostic_count > 0)

        tp = len(totals.expected & totals.actual)
        fp = len(totals.actual - totals.expected)
        fn = len(totals.expected - totals.actual)
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0

        def rate(values: list[bool], *, invert: bool = False) -> float:
            if not values:
                return 1.0
            value = sum(values) / len(values)
            return 1.0 - value if invert else value

        metrics = BenchmarkMetrics(
            true_positive=tp,
            false_positive=fp,
            false_negative=fn,
            precision=precision,
            recall=recall,
            false_positive_rate=fp / (tp + fp) if tp + fp else 0.0,
            finding_location_accuracy=precision,
            actionable_rate=(
                totals.actionable / totals.finding_count if totals.finding_count else 1.0
            ),
            candidate_parse_rate=rate(totals.candidate_parse),
            patch_apply_rate=rate(totals.patch_apply),
            fingerprint_removed_rate=rate(totals.fingerprints),
            new_diagnostic_regression_rate=rate(totals.diagnostic_regression, invert=True),
            model_calls=0,
            prompt_tokens=0,
            elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
        )
        return BenchmarkReport(
            suite=suite,
            metrics=metrics,
            case_count=len(cases),
            capabilities=capabilities,
            failures=failures,
        )


def built_in_golden_cases() -> list[GoldenCase]:
    def source(
        case_id: str,
        content: str,
        *,
        language: Literal["python", "cpp"] = "python",
    ) -> SourceFile:
        suffix = "py" if language == "python" else "cpp"
        return SourceFile.from_content(
            file_id=case_id,
            relative_path=f"golden/{case_id}.{suffix}",
            language=language,
            content=content,
        )

    undefined = source("undefined", "def f():\n    return missing\n")
    return [
        GoldenCase(
            case_id="python-undefined-tp",
            category="true_positive",
            files=[undefined],
            expected=[
                GoldenExpectation(
                    rule_id="python.undefined-name",
                    relative_path=undefined.relative_path,
                    line=2,
                )
            ],
            candidate_parse_ok=True,
            patch_apply_ok=True,
            fingerprint_removed=True,
            new_diagnostic_count=0,
        ),
        GoldenCase(
            case_id="python-except-alias-fp",
            category="false_positive",
            files=[
                source(
                    "except_alias",
                    "try:\n"
                    "    raise ValueError('x')\n"
                    "except Exception as exc:\n"
                    "    raise RuntimeError(str(exc)) from exc\n",
                )
            ],
        ),
        GoldenCase(
            case_id="cpp-safe-parse",
            category="cpp_false_positive",
            files=[source("cpp_safe", "int add(int a, int b) { return a + b; }\n", language="cpp")],
        ),
    ]


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run the source-free CodeAstra golden benchmark")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    arguments = parser.parse_args()
    report = await GoldenBenchmarkRunner().run(built_in_golden_cases())
    payload = report.model_dump_json(indent=2)
    if arguments.json:
        arguments.json.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    if arguments.markdown:
        arguments.markdown.write_text(report.markdown(), encoding="utf-8")
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

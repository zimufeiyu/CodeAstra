import json
import os
import sys

import pytest

from code_review.application.analyzer_adapters import (
    AnalyzerCapability,
    CodeQLDeepModeAdapter,
    CppIsolationExecutor,
    LibCSTRenameAdapter,
    RuffAnalyzerAdapter,
    SemgrepStructuralAdapter,
    StandardDiagnostic,
)
from code_review.application.evidence_validation import EvidenceValidator
from code_review.application.finding_verifier import FindingVerifier
from code_review.application.python_analysis_cache import parse_python_source
from code_review.application.review_planner import ReviewPlanner
from code_review.application.static_analysis import StaticAnalyzer
from code_review.domain.model_protocol import ReviewFinding
from code_review.domain.review_models import SourceFile


def source(file_id: str, path: str, content: str) -> SourceFile:
    return SourceFile.from_content(
        file_id=file_id, relative_path=path, language="python", content=content
    )


def draft(path: str, evidence: str, *, line: int = 1, confidence: float = 0.9) -> ReviewFinding:
    return ReviewFinding(
        rule_id="R003",
        severity="info",
        confidence=confidence,
        category="代码质量",
        file=path,
        start_line=line,
        end_line=line,
        title="方法参数命名不一致",
        evidence=evidence,
        impact="可能降低可读性",
        suggestion="统一参数命名",
    )


def test_production_snake_case_signature_is_suppressed_before_navigation():
    signature = "def forward_l1(self, user_indices, item_indices, return_components=False):"
    reviewed = source("target", "model.py", signature + "\n    return user_indices\n")
    result = FindingVerifier().verify_draft(draft("model.py", signature), reviewed, [reviewed])
    assert result.action == "suppress"
    assert result.code == "already_compliant"
    assert EvidenceValidator().validate(draft("model.py", signature), [reviewed], []) is None


def test_structural_interface_evidence_produces_executable_rename_plan():
    target = source(
        "target",
        "target.py",
        "class Target(Contract):\n"
        "    def _load(self, userIndices):\n"
        "        return userIndices\n"
        "\n"
        "def call(target):\n"
        "    return target._load(userIndices=1)\n",
    )
    contract = source(
        "contract",
        "contract.py",
        "class Contract:\n    def _load(self, user_indices):\n        ...\n",
    )
    evidence = "def _load(self, userIndices):"
    result = FindingVerifier().verify_draft(
        draft("target.py", evidence, line=2), target, [target, contract]
    )
    assert result.action == "accept"
    assert result.rename_plan is not None
    assert result.rename_plan.old_name == "userIndices"
    assert result.rename_plan.new_name == "user_indices"
    assert result.rename_plan.executable is True
    assert result.rename_plan.affected_keyword_callsites[0].line == 6


def test_no_contract_is_not_actionable_and_dynamic_calls_require_review():
    target = source(
        "target",
        "target.py",
        "class Target(Contract):\n"
        "    def load(self, userIndices):\n"
        "        return userIndices\n"
        "\n"
        "def call(target, values):\n"
        "    return target.load(**values)\n",
    )
    no_contract = FindingVerifier().verify_draft(
        draft("target.py", "def load(self, userIndices):", line=2), target, [target]
    )
    assert no_contract.code == "finding_not_actionable"
    contract = source(
        "contract",
        "contract.py",
        "class Contract:\n    def load(self, user_indices):\n        ...\n",
    )
    unsafe = FindingVerifier().verify_draft(
        draft("target.py", "def load(self, userIndices):", line=2),
        target,
        [target, contract],
    )
    assert unsafe.rename_plan is not None
    assert unsafe.rename_plan.safety == "requires_review"
    assert unsafe.rename_plan.executable is False
    assert unsafe.rename_plan.unsafe_reasons


def test_ruff_adapter_isolated_command_and_standardizes_diagnostics(monkeypatch, tmp_path):
    executable = tmp_path / "ruff"
    executable.write_text("placeholder")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        payload = [
            {
                "code": "F821",
                "message": "Undefined name `missing`",
                "location": {"row": 1, "column": 1},
                "end_location": {"row": 1, "column": 8},
            }
        ]
        return type("Completed", (), {"stdout": json.dumps(payload)})()

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = RuffAnalyzerAdapter(str(executable))
    diagnostics = adapter.analyze(source("one", "pkg/a.py", "missing\n"))
    assert diagnostics[0].rule_id == "ruff.f821"
    command, kwargs = calls[0]
    assert "--isolated" in command and "--no-cache" in command
    assert command[-1] == "-"
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] <= 5


def test_ruff_adapter_finds_sibling_of_active_virtualenv_python(monkeypatch, tmp_path):
    executable = tmp_path / ("ruff.exe" if os.name == "nt" else "ruff")
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    adapter = RuffAnalyzerAdapter()
    assert adapter.capability.available is True
    assert adapter._executable == str(executable)


def test_external_deep_and_cpp_capabilities_fail_closed():
    assert CodeQLDeepModeAdapter(executable=None).capability.available is False
    assert SemgrepStructuralAdapter(executable=None).capability.available is False
    cpp = CppIsolationExecutor()
    assert cpp.capability.available is False
    with pytest.raises(RuntimeError, match="禁止宿主机"):
        cpp.run_allowlisted("g++", "uploaded.cpp")
    libcst = LibCSTRenameAdapter().capability
    if not libcst.available:
        assert libcst.mode == "unavailable"
        assert "fail-closed" in libcst.message


def test_adapter_rejects_parent_traversal_before_process_start():
    adapter = RuffAnalyzerAdapter("ruff")
    with pytest.raises(ValueError, match="escapes"):
        adapter.analyze(source("bad", "../secret.py", "value = 1\n"))


def test_same_revision_reuses_ast_and_verifier_result():
    reviewed = source("one", "one.py", "def ready(user_indices):\n    return user_indices\n")
    assert parse_python_source(reviewed) is parse_python_source(reviewed)
    verifier = FindingVerifier()
    claim = draft("one.py", "def ready(user_indices):")
    first = verifier.verify_draft(claim, reviewed, [reviewed])
    second = verifier.verify_draft(claim, reviewed, [reviewed])
    assert first is second
    assert len(verifier._naming_cache) == 1


def test_libcst_rename_preserves_format_and_updates_definition_body_and_keyword_call():
    adapter = LibCSTRenameAdapter()
    if not adapter.capability.available:
        pytest.skip("LibCST is not installed in this test environment")
    content = (
        "class Base:\n"
        "    def _run(self, user_id):\n"
        "        return user_id\n\n"
        "class Child(Base):\n"
        "    def _run(self, userId):  # keep-comment\n"
        "        return userId + 1\n\n"
        "Child()._run(userId=1)\n"
    )
    reviewed = source("rename", "model.py", content)
    result = FindingVerifier().verify_draft(
        draft("model.py", "def _run(self, userId):  # keep-comment", line=6),
        reviewed,
        [reviewed],
    )
    assert result.rename_plan is not None and result.rename_plan.executable
    revised = adapter.apply(
        content,
        result.rename_plan,
        target_line=6,
        function_name="_run",
    )
    assert "def _run(self, user_id):  # keep-comment" in revised
    assert "return user_id + 1" in revised
    assert "_run(user_id=1)" in revised
    assert "userId" not in revised


@pytest.mark.asyncio
async def test_available_standard_adapter_replaces_lexical_name_rule_without_duplicates():
    class Adapter:
        capability = AnalyzerCapability("standard-test", True, "isolated", "test")

        def analyze(self, reviewed):
            return [
                StandardDiagnostic(
                    analyzer_id="standard-test",
                    rule_id="ruff.f821",
                    severity="error",
                    path=reviewed.relative_path,
                    line=1,
                    column=1,
                    end_line=1,
                    end_column=8,
                    message="Undefined name `missing`",
                )
            ]

    result = await StaticAnalyzer(Adapter()).analyze(
        [source("one", "one.py", "missing\n")]
    )
    undefined = [item for item in result.findings if item.rule_id == "python.undefined-name"]
    assert len(undefined) == 1
    assert undefined[0].analyzer == "standard-test"


def test_llm_deterministic_fact_is_suppressed_instead_of_merged_twice():
    reviewed = source("one", "one.py", "missing\n")
    deterministic = draft("one.py", "missing").model_copy(
        update={
            "rule_id": "python.undefined-name",
            "title": "名称未定义",
            "category": "correctness",
        }
    )
    assert EvidenceValidator().validate(deterministic, [reviewed], []) is None


def test_python_model_context_uses_target_node_imports_and_contract_signatures_only():
    target = source(
        "target",
        "app.py",
        "import contract\n\ndef run(value):\n    return contract.normalize(value)\n",
    )
    dependency = source(
        "contract",
        "contract.py",
        "import typing\n\ndef normalize(value: str) -> str:\n"
        "    large_body_marker = 'must not enter model context'\n"
        "    return value.strip()\n",
    )
    plan = ReviewPlanner().plan("review", [target, dependency])
    run_chunk = next(
        item
        for item in plan.chunks
        if item.target_path == "app.py" and item.target_code.startswith("def run")
    )
    context = "\n".join(item.code for item in run_chunk.context_references)
    assert "import contract" in context
    assert "def normalize(value: str) -> str:" in context
    assert "large_body_marker" not in context
    assert run_chunk.target_code.startswith("def run")

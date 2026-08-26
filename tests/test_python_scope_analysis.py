import pytest

from code_review.application.static_analysis import StaticAnalyzer
from code_review.domain.review_models import SourceFile


async def undefined_names(code: str) -> set[str]:
    source = SourceFile.from_content(
        file_id="scope", relative_path="scope.py", language="python", content=code
    )
    result = await StaticAnalyzer().analyze([source])
    return {item.evidence for item in result.findings if item.rule_id == "python.undefined-name"}


@pytest.mark.asyncio
async def test_lexical_bindings_cover_except_with_match_and_nested_scopes():
    names = await undefined_names(
        "import contextlib\n"
        "matrix_name = 'm'\npath = 'p'\n"
        "def outer(value):\n"
        "    total = 1\n"
        "    def inner():\n        nonlocal total\n        return total\n"
        "    try:\n        raise ValueError()\n"
        "    except Exception as exc:\n"
        "        message = f'Failed to load {matrix_name} from {path}: {exc}'\n"
        "    with contextlib.nullcontext(value) as selected:\n        (total := 2)\n"
        "    match selected:\n"
        "        case {'item': item, **rest}:\n            return item, rest, inner(), message\n"
    )
    assert names == set()


@pytest.mark.asyncio
async def test_comprehension_scope_and_real_undefined_are_preserved():
    analyzer = StaticAnalyzer()
    source = SourceFile.from_content(
        file_id="scope",
        relative_path="scope.py",
        language="python",
        content="items = [1]\nvalues = [item for item in items]\nprint(item_typo, values)\n",
    )
    result = await analyzer.analyze([source])
    undefined = [item for item in result.findings if item.rule_id == "python.undefined-name"]
    assert [item.evidence for item in undefined] == ["item_typo"]
    assert undefined[0].source_kind == "deterministic_fact"
    assert undefined[0].applicability == "applicable"
    assert undefined[0].relative_path == "scope.py"
    assert len(undefined[0].fingerprint) == 64
    await analyzer.analyze([source])
    assert source.sha256 in analyzer._python_ast_cache
    assert source.sha256 in analyzer._python_scope_cache

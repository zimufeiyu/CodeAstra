from code_review.application.static_analysis import StaticAnalyzer
from code_review.domain.review_models import SourceFile


def test_cpp_analysis_fails_closed_without_an_isolated_executor():
    files = [
        SourceFile.from_content(
            file_id="first",
            relative_path="src/first.cpp",
            language="cpp",
            content='#include "include/value.hpp"\n',
        ),
        SourceFile.from_content(
            file_id="second",
            relative_path="src/second.cpp",
            language="cpp",
            content="int second() { return 2; }\n",
        ),
        SourceFile.from_content(
            file_id="header",
            relative_path="include/value.hpp",
            language="cpp",
            content="int value;\nint unused;\n",
        ),
    ]

    findings, coverage = StaticAnalyzer()._analyze_cpp(files)

    assert findings == []
    assert coverage.available is False
    assert "未启动 g++" in coverage.message
    assert "不能隔离文件读取" in coverage.message
    assert "未运行任何用户程序" in coverage.message

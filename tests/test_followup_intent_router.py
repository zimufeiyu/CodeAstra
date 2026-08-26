import pytest

from code_review.application.followup_intent_router import route_followup_prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "删除第 8 行的危险调用",
        "请移除这个函数中的 eval 调用",
        "在这个方法中新增参数校验",
        "把返回逻辑替换为安全解析",
        "重构这个函数的异常处理逻辑",
        "修复第 12 行的导入语句",
        "将 b 替换为空字典",
        "删除 unused_value",
        "删除这段判断",
        "把这里改成提前返回",
        "增加空值检查",
    ],
)
def test_explicit_edit_imperatives_route_to_fix_candidate(prompt):
    assert route_followup_prompt(prompt) == "fix_candidate"


@pytest.mark.parametrize(
    "prompt",
    [
        "为什么要删除这段代码？",
        "解释如何修改这个函数",
        "这个方法是否需要增加参数校验",
        "删除这段代码有什么影响",
        "不要删除这个函数",
        "请说明“替换返回逻辑”的含义",
        "修复这个问题",
        "我看到报告里写了删除",
        "他说了删除 b，但我不确定",
    ],
)
def test_questions_negation_quotes_and_ambiguous_text_default_to_answer(prompt):
    assert route_followup_prompt(prompt) == "answer"

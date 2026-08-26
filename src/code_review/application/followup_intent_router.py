from __future__ import annotations

import re
import unicodedata
from typing import Literal

FollowupAction = Literal["answer", "fix_candidate"]

_EDIT_VERBS = "删除|移除|增加|新增|添加|替换|改为|改成|重构|修复"
_EXPLANATION_MARKERS = (
    "为什么",
    "为何",
    "如何",
    "怎么",
    "是否",
    "有什么影响",
    "解释",
    "说明",
    "什么意思",
    "能否",
    "可不可以",
)
_TARGET_MARKERS = re.compile(
    r"第\s*\d+\s*行|代码|函数|方法|类|变量|参数|返回|分支|逻辑|检查|调用|导入|"
    r"异常|语句|表达式|接口|字段|属性|循环|条件|判断|这里|这段|模块|文件|"
    r"\b(?:def|class|if|return|import)\b",
    re.IGNORECASE,
)
_IDENTIFIER_TARGET = re.compile(
    r"^(?:变量|参数)?\s*[A-Za-z_]\w*(?:\s*[，,、]\s*[A-Za-z_]\w*)?(?:\s|$|改|替换|删除|移除)"
)
_NEGATED_EDIT = re.compile(rf"(?:不要|别|无需|不需要|禁止|避免).{{0,20}}(?:{_EDIT_VERBS})")
_DIRECT_EDIT = re.compile(
    rf"^(?:请|请你|帮我|麻烦|直接|现在|立即)?\s*(?:{_EDIT_VERBS})(?P<target>.+)$"
)
_OBJECT_FIRST_EDIT = re.compile(
    rf"^(?:请|请你|帮我|麻烦|直接|现在|立即)?\s*(?:把|将|在)(?P<target>.+)(?:{_EDIT_VERBS})(?:.+)?$"
)


def normalize_followup_prompt(prompt: str) -> str:
    normalized = unicodedata.normalize("NFKC", prompt).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def route_followup_prompt(prompt: str) -> FollowupAction:
    """Conservatively route explicit edit imperatives; uncertainty stays explanatory."""

    normalized = normalize_followup_prompt(prompt)
    if not normalized:
        return "answer"
    if "?" in normalized or "？" in normalized:
        return "answer"
    if any(marker in normalized for marker in _EXPLANATION_MARKERS):
        return "answer"
    if _NEGATED_EDIT.search(normalized):
        return "answer"

    match = _DIRECT_EDIT.match(normalized) or _OBJECT_FIRST_EDIT.match(normalized)
    if match is None:
        return "answer"
    target = match.group("target").strip(" ，,。.!！:：;；")
    if not target or (
        not _TARGET_MARKERS.search(target) and not _IDENTIFIER_TARGET.search(target)
    ):
        return "answer"
    return "fix_candidate"

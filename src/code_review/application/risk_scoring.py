"""Deterministic severity assessment for model-produced review findings.

The model supplies evidence and risk factors, but the final severity is computed
here so that wording or model drift cannot arbitrarily change the risk level.
"""

from __future__ import annotations

import re

from code_review.domain.model_protocol import ReviewFinding, ReviewResponse

_IMPACT_SCORE = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_EXPLOITABILITY_SCORE = {"low": 1, "medium": 2, "high": 3}
_EXPOSURE_SCORE = {"local": 0, "unknown": 1, "internal": 1, "authenticated": 2, "internet": 3}

_QUALITY_CATEGORY_MARKERS = ("code_quality", "quality", "style", "maintainability", "readability")
_QUALITY_TEXT_MARKERS = (
    "未使用",
    "unused import",
    "unused variable",
    "命名",
    "格式",
    "注释",
    "风格",
    "可读性",
    "lint",
    "可维护",
    "重复代码",
    "magic number",
)
_CRITICAL_MARKERS = (
    "远程代码执行",
    "任意代码执行",
    "remote code execution",
    "arbitrary code execution",
    "认证绕过",
    "身份认证绕过",
    "auth bypass",
    "privilege escalation",
    "权限提升",
    "任意文件写入",
    "任意文件删除",
)
_HIGH_MARKERS = (
    "命令注入",
    "command injection",
    "sql注入",
    "sql injection",
    "路径穿越",
    "path traversal",
    "服务器端请求伪造",
    "ssrf",
    "硬编码密钥",
    "硬编码密码",
    "hardcoded secret",
    "存储型 xss",
    "stored xss",
    "不安全反序列化",
    "insecure deserialization",
)


def _normalized_text(finding: ReviewFinding) -> str:
    fields = (finding.rule_id, finding.category, finding.title, finding.evidence, finding.impact)
    return re.sub(r"\s+", " ", " ".join(fields).lower())


def _is_quality_issue(finding: ReviewFinding, text: str) -> bool:
    category = finding.category.lower().strip()
    category_match = any(
        category == marker or category.startswith(marker + "_") or category.startswith(marker + "/")
        for marker in _QUALITY_CATEGORY_MARKERS
    )
    return category_match or any(marker in text for marker in _QUALITY_TEXT_MARKERS)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _score(impact: str, exploitability: str, exposure: str) -> int:
    raw = (
        2 * _IMPACT_SCORE[impact]
        + 2 * _EXPLOITABILITY_SCORE[exploitability]
        + _EXPOSURE_SCORE[exposure]
    )
    # Maximum is 17 (critical impact, high exploitability, internet exposed).
    return max(0, min(100, round(raw / 17 * 100)))


def assess_finding(finding: ReviewFinding) -> ReviewFinding:
    text = _normalized_text(finding)
    score = _score(finding.impact_level, finding.exploitability, finding.exposure)

    if _is_quality_issue(finding, text):
        severity = "info"
        score = min(score, 20)
        reason = "代码质量或可维护性问题，不直接构成安全风险。"
    elif (
        _contains_any(text, _CRITICAL_MARKERS)
        and finding.impact_level == "critical"
        and finding.exploitability == "high"
    ):
        severity = "critical"
        score = max(score, 85)
        reason = "涉及关键安全边界（代码执行、认证绕过或权限提升），且具备高可利用性。"
    elif _contains_any(text, _CRITICAL_MARKERS):
        severity = "critical" if score >= 70 else "high"
        reason = "问题可能触及代码执行、认证或权限边界，需优先核实并修复。"
    elif _contains_any(text, _HIGH_MARKERS):
        severity = "critical" if score >= 85 else ("high" if score >= 45 else "medium")
        score = max(score, 85 if severity == "critical" else (60 if severity == "high" else 45))
        reason = "命中高风险攻击模式，外部或不可信输入可能被利用。"
    elif score >= 85:
        severity = "critical"
        reason = "影响范围和可利用性均高，且暴露面较大。"
    elif score >= 60:
        severity = "high"
        reason = "影响或可利用性较高，建议尽快修复。"
    elif score >= 40:
        severity = "medium"
        reason = "存在可验证的安全或稳定性影响，建议纳入近期修复。"
    elif score >= 20:
        severity = "low"
        reason = "影响范围有限或利用条件较多，可安排在常规迭代中修复。"
    else:
        severity = "info"
        reason = "当前证据不足以构成明确安全风险，建议关注并持续改进。"

    model_reason = finding.severity_reason.strip()
    final_reason = reason if not model_reason else f"{reason} 模型依据：{model_reason}"
    return finding.model_copy(
        update={
            "severity": severity,
            "risk_score": score,
            "severity_reason": final_reason[:500],
        }
    )


def assess_response(response: ReviewResponse) -> ReviewResponse:
    return response.model_copy(
        update={"findings": [assess_finding(item) for item in response.findings]}
    )

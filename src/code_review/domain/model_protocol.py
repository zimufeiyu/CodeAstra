from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SeverityLevel = Literal["critical", "high", "medium", "low", "info"]
RiskLevel = Literal["critical", "high", "medium", "low"]
ExploitabilityLevel = Literal["high", "medium", "low"]
ExposureLevel = Literal["internet", "authenticated", "internal", "local", "unknown"]
ModelProvider = Literal["local", "deepseek"]


class ModelSelection(BaseModel):
    profile_id: str = Field(min_length=1)
    provider: ModelProvider
    model: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    selection_source: Literal["fixed", "auto", "manual"] = "fixed"


class ContextWindowExceededError(ValueError):
    def __init__(self, context_limit: int, prompt_tokens: int) -> None:
        self.context_limit = context_limit
        self.prompt_tokens = prompt_tokens
        super().__init__(f"input uses {prompt_tokens} tokens in a {context_limit}-token context")


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class InferenceRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    messages: list[ChatMessage] = Field(min_length=1)
    max_output_tokens: int = Field(default=32768, ge=128, le=32768)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    request_id: str = Field(min_length=1)
    response_format: Literal["review", "fix", "text"] = "review"
    model_profile_id: str = "local-qwen3-8b"


class RawInferenceResult(BaseModel):
    request_id: str
    instance_id: str
    content: str
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    latency_ms: int = Field(ge=0)
    provider: str = "local"
    model: str | None = None
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0)


class ReviewFinding(BaseModel):
    rule_id: str = Field(min_length=1)
    severity: SeverityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    category: str = Field(min_length=1)
    file: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    impact_level: RiskLevel = "medium"
    exploitability: ExploitabilityLevel = "medium"
    exposure: ExposureLevel = "unknown"
    risk_score: int = Field(default=0, ge=0, le=100)
    severity_reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_line_range(self) -> "ReviewFinding":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReviewResponse(BaseModel):
    summary: str = Field(min_length=1)
    findings: list[ReviewFinding] = Field(max_length=50)
    uncovered: list[str] = Field(max_length=50)


class FixProposal(BaseModel):
    replacement: str
    explanation: str = Field(min_length=1)

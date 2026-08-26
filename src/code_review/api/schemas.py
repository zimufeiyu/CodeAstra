from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_review.domain.review_models import ReviewOrigin


class ModelProfileResponse(BaseModel):
    profile_id: str
    provider: Literal["local", "deepseek"]
    model: str
    display_name: str
    available: bool
    unavailable_reason: str | None = None
    context_tokens: int = Field(ge=4096)
    supports_json: bool = True
    requires_user_api_key: bool = False


class DeepSeekModelResponse(BaseModel):
    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class DeepSeekModelsResponse(BaseModel):
    models: list[DeepSeekModelResponse]


class InstanceHealth(BaseModel):
    endpoint_id: str
    inflight_requests: int = Field(ge=0)
    inflight_tokens: int = Field(ge=0)
    circuit_open: bool
    available: bool
    reason_code: str | None = None


class GatewayHealthResponse(BaseModel):
    instances: list[InstanceHealth]


class ReviewFileInput(BaseModel):
    filename: str = Field(min_length=1)
    language: str | None = None
    content: str


class ReviewCreateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    language: str | None = None
    filename: str | None = None
    content: str | None = None
    files: list[ReviewFileInput] = Field(default_factory=list, max_length=200)
    local_diff_base_files: list[ReviewFileInput] = Field(default_factory=list, max_length=200)
    origin: ReviewOrigin | None = None
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    deepseek_selection_mode: Literal["auto", "manual"] = "auto"
    deepseek_model: str | None = Field(default=None, min_length=1, max_length=200)


class ReviewCreatedResponse(BaseModel):
    review_id: str
    status: str
    expires_at: str


class ReviewRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class FollowupContextInput(BaseModel):
    kind: Literal["finding", "selection"]
    file_id: str = Field(min_length=1)
    finding_id: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    selected_code: str | None = Field(default=None, max_length=20000)


class FollowupRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    context: FollowupContextInput | None = None
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FollowupFixPreviewRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    base_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    context: FollowupContextInput


class FindingDecisionRequest(BaseModel):
    decision: Literal["accepted_risk", "deferred", "dismissed"]


class FixCandidateRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=100)


class RepairIntentRequest(BaseModel):
    review_id: str = Field(min_length=1, max_length=100)
    finding_id: str = Field(min_length=1, max_length=100)
    base_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    option_id: str = Field(min_length=1, max_length=300)
    intent_kind: Literal[
        "rename_existing",
        "declare_parameter",
        "declare_local",
        "import_symbol",
        "custom_behavior",
        "defer",
    ]
    selected_symbol: str | None = Field(default=None, max_length=300)
    import_source: str | None = Field(default=None, max_length=500)
    initializer: str | None = Field(default=None, max_length=1000)
    user_intent: str | None = Field(default=None, min_length=1, max_length=2000)

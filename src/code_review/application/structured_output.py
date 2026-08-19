import json

from code_review.application.risk_scoring import assess_response
from code_review.domain.model_protocol import ReviewResponse


def decode_review_response(content: str) -> ReviewResponse:
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("model output must be a JSON object")
    return assess_response(ReviewResponse.model_validate(parsed))

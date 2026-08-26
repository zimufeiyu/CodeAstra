from __future__ import annotations

import httpx

from code_review.application.chunk_review_service import ChunkReviewService
from code_review.domain.review_chunks import ChunkErrorCode
from code_review.infrastructure.sglang.registry import AllInstancesUnavailable


def _wrapped(error: Exception) -> RuntimeError:
    try:
        raise error
    except Exception as cause:
        try:
            raise RuntimeError("all model attempts failed") from cause
        except RuntimeError as wrapped:
            return wrapped


def test_local_model_connection_refused_is_structured() -> None:
    request = httpx.Request("POST", "http://127.0.0.1:30000/v1/chat/completions")
    error = _wrapped(httpx.ConnectError("connection refused", request=request))

    assert ChunkReviewService._error_code(error, "local-qwen3-8b") == (
        ChunkErrorCode.LOCAL_MODEL_CONNECTION_REFUSED
    )


def test_local_model_timeout_is_structured() -> None:
    request = httpx.Request("POST", "http://127.0.0.1:30001/v1/chat/completions")
    error = _wrapped(httpx.ReadTimeout("timed out", request=request))

    assert ChunkReviewService._error_code(error, "local-qwen3-32b") == (
        ChunkErrorCode.LOCAL_MODEL_TIMEOUT
    )


def test_local_model_circuit_open_is_structured() -> None:
    error = _wrapped(AllInstancesUnavailable("all instances unavailable"))

    assert ChunkReviewService._error_code(error, "local-qwen3-8b") == (
        ChunkErrorCode.LOCAL_MODEL_CIRCUIT_OPEN
    )

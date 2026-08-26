from __future__ import annotations

import ast
import hashlib
from functools import lru_cache

from code_review.domain.review_models import SourceFile


@lru_cache(maxsize=256)
def _parse_cached(sha256: str, content: str) -> ast.Module:
    del sha256
    return ast.parse(content)


def parse_python_source(source: SourceFile) -> ast.Module:
    return _parse_cached(source.sha256, source.content)


def parse_python_content(content: str) -> ast.Module:
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return _parse_cached(sha256, content)


def python_parse_cache_info() -> dict[str, int]:
    info = _parse_cached.cache_info()
    return {"hits": info.hits, "misses": info.misses, "size": info.currsize}

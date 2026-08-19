from __future__ import annotations

from pathlib import Path

from code_review.deployment.model_artifacts import ModelArtifactError, inspect_model_directory
from code_review.deployment.models import ModelCandidate


class ModelDiscovery:
    def __init__(self, roots: list[Path], *, max_candidates: int = 200) -> None:
        self._roots = [root.expanduser() for root in roots]
        self._max_candidates = max_candidates

    def _inside_allowed_root(self, candidate: Path) -> bool:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return False
        for root in self._roots:
            try:
                resolved.relative_to(root.resolve(strict=True))
                return True
            except (OSError, ValueError):
                continue
        return False

    def validate_manual_path(self, candidate: Path) -> ModelCandidate:
        if not self._inside_allowed_root(candidate):
            raise ValueError("model path is outside the configured search roots")
        resolved = candidate.resolve(strict=True)
        metadata = inspect_model_directory(resolved)
        return ModelCandidate(
            path=resolved,
            architecture=metadata.architecture,
            torch_dtype=metadata.torch_dtype,
            context_tokens=metadata.max_position_embeddings,
            shard_count=metadata.shard_count,
        )

    def discover(self) -> list[ModelCandidate]:
        candidates: list[ModelCandidate] = []
        visited: set[Path] = set()
        for root in self._roots:
            if not root.is_dir():
                continue
            paths = [root]
            paths.extend(path.parent for path in root.glob("*/config.json"))
            paths.extend(path.parent for path in root.glob("models--*/snapshots/*/config.json"))
            for path in paths:
                if len(candidates) >= self._max_candidates:
                    return candidates
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                if resolved in visited or not self._inside_allowed_root(resolved):
                    continue
                visited.add(resolved)
                try:
                    candidates.append(self.validate_manual_path(resolved))
                except (ModelArtifactError, ValueError):
                    continue
        return sorted(candidates, key=lambda item: str(item.path))

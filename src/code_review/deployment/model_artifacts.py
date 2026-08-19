from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError


class ModelArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class ModelMetadata:
    model_type: str
    architecture: str
    torch_dtype: str
    max_position_embeddings: int
    shard_count: int


class _ModelConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model_type: str
    architectures: list[str]
    torch_dtype: str
    max_position_embeddings: int


class _WeightIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")

    weight_map: dict[str, str]


def _parse_model(model: type[BaseModel], path: Path) -> BaseModel:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as error:
        raise ModelArtifactError(f"invalid model artifact: {path.name}") from error


def inspect_model_directory(model_path: Path) -> ModelMetadata:
    if not model_path.is_dir():
        raise ModelArtifactError(f"model directory does not exist: {model_path}")
    tokenizer_path = model_path / "tokenizer_config.json"
    if not tokenizer_path.is_file():
        raise ModelArtifactError("missing tokenizer_config.json")

    config = _parse_model(_ModelConfig, model_path / "config.json")
    assert isinstance(config, _ModelConfig)
    if config.model_type != "qwen3" or "Qwen3ForCausalLM" not in config.architectures:
        raise ModelArtifactError("model is not Qwen3ForCausalLM")

    index = _parse_model(_WeightIndex, model_path / "model.safetensors.index.json")
    assert isinstance(index, _WeightIndex)
    shards = set(index.weight_map.values())
    if not shards:
        raise ModelArtifactError("weight index contains no shards")
    for shard in sorted(shards):
        shard_path = PurePosixPath(shard)
        if shard_path.is_absolute() or ".." in shard_path.parts or len(shard_path.parts) != 1:
            raise ModelArtifactError(f"unsafe weight shard path: {shard}")
        actual_path = model_path / shard
        if not actual_path.is_file():
            raise ModelArtifactError(f"missing weight shard: {shard}")

    return ModelMetadata(
        model_type=config.model_type,
        architecture="Qwen3ForCausalLM",
        torch_dtype=config.torch_dtype,
        max_position_embeddings=config.max_position_embeddings,
        shard_count=len(shards),
    )

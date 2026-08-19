from __future__ import annotations

import contextlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from code_review.deployment.models import DeploymentMode


class DeploymentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    deployment_mode: DeploymentMode
    default_model_profile_id: str
    sglang_endpoints: list[str] = Field(default_factory=list, max_length=16)
    qwen3_32b_endpoints: list[str] = Field(default_factory=list, max_length=16)
    ppu_model_path: Path | None = None
    ppu_device_ids: list[str] = Field(default_factory=list, max_length=16)

    @classmethod
    def load(cls, path: Path) -> DeploymentManifest | None:
        if not path.is_file():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def backup_existing(path: Path) -> Path | None:
        if not path.is_file():
            return None
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_dir / f"{path.stem}-{stamp}{path.suffix}"
        shutil.copy2(path, backup)
        with contextlib.suppress(OSError):
            backup.chmod(0o600)
        return backup

    @staticmethod
    def restore(path: Path, backup: Path | None) -> None:
        if backup is None:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.restore.tmp")
        shutil.copy2(backup, temporary)
        temporary.replace(path)

    def write_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(path)

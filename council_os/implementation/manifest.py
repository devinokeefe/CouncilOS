from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPortfolioEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    temperature: float
    max_tokens: int
    prompt_version_hash: str


class ManifestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    code_version: str
    schema_versions: dict[str, str]
    run_config_version: str
    stage_machine_version: str
    validator_suite_version: str
    model_portfolio: dict[str, ModelPortfolioEntry]
    toolchain_versions: dict[str, str]
    policy_knobs: dict[str, object]
    base_commit: str
    head_commit: str
    prompt_pack_hash: str
    config_raw: str
    capability_level: str = "v1"
    artifact_refs: dict[str, str] = Field(default_factory=dict)


class ImplementationRunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    created_at: datetime
    code_version: str
    schema_versions: dict[str, str]
    run_config_version: str
    stage_machine_version: str
    validator_suite_version: str
    model_portfolio: dict[str, ModelPortfolioEntry]
    toolchain_versions: dict[str, str]
    policy_knobs: dict[str, object]
    base_commit: str
    head_commit: str
    prompt_pack_hash: str
    config_hash: str
    capability_level: str = "v1"
    artifact_refs: dict[str, str] = Field(default_factory=dict)


def _manifest_path(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root / "implementation_run_manifest.json"


def create_manifest(run_root: Path, manifest_input: ManifestInput) -> ImplementationRunManifest:
    path = _manifest_path(run_root)
    if path.exists():
        raise FileExistsError("implementation_run_manifest already exists")

    config_hash = hashlib.sha256(manifest_input.config_raw.encode("utf-8")).hexdigest()
    manifest = ImplementationRunManifest(
        run_id=manifest_input.run_id,
        created_at=datetime.now(UTC),
        code_version=manifest_input.code_version,
        schema_versions=dict(manifest_input.schema_versions),
        run_config_version=manifest_input.run_config_version,
        stage_machine_version=manifest_input.stage_machine_version,
        validator_suite_version=manifest_input.validator_suite_version,
        model_portfolio=manifest_input.model_portfolio,
        toolchain_versions=manifest_input.toolchain_versions,
        policy_knobs=manifest_input.policy_knobs,
        base_commit=manifest_input.base_commit,
        head_commit=manifest_input.head_commit,
        prompt_pack_hash=manifest_input.prompt_pack_hash,
        config_hash=config_hash,
        capability_level=manifest_input.capability_level,
        artifact_refs=dict(manifest_input.artifact_refs),
    )
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def load_manifest(run_root: Path) -> ImplementationRunManifest:
    path = _manifest_path(run_root)
    return ImplementationRunManifest.model_validate_json(path.read_text(encoding="utf-8"))

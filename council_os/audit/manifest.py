from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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
    schema_versions: dict[str, str] | str
    run_config_version: str = ""
    stage_machine_version: str
    validator_suite_version: str
    selection_protocol_version: str
    model_portfolio: dict[str, ModelPortfolioEntry]
    branching_policy: dict[str, object]
    ensemble_policy: dict[str, object]
    iteration_caps: dict[str, object]
    tool_policy_version: str
    tools_enabled: bool
    consent_profile: str
    determinism_disclaimer: str
    prompt_pack_hash: str = ""
    config_raw: str
    parent_run_id: UUID | None = None
    forked_from_checkpoint_id: str | None = None


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    created_at: datetime
    code_version: str
    schema_versions: dict[str, str]
    run_config_version: str = ""
    stage_machine_version: str
    validator_suite_version: str
    selection_protocol_version: str
    model_portfolio: dict[str, ModelPortfolioEntry]
    branching_policy: dict[str, object]
    ensemble_policy: dict[str, object]
    iteration_caps: dict[str, object]
    tool_policy_version: str
    tools_enabled: bool
    consent_profile: str
    determinism_disclaimer: str
    prompt_pack_hash: str = ""
    config_hash: str
    parent_run_id: UUID | None = None
    forked_from_checkpoint_id: str | None = None


def _manifest_path(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root / "manifest.json"


def create_manifest(run_root: Path, manifest_input: ManifestInput) -> RunManifest:
    path = _manifest_path(run_root)
    if path.exists():
        raise FileExistsError("manifest already exists")

    config_hash = hashlib.sha256(manifest_input.config_raw.encode("utf-8")).hexdigest()
    if isinstance(manifest_input.schema_versions, str):
        schema_versions = {"artifacts": manifest_input.schema_versions}
    else:
        schema_versions = dict(manifest_input.schema_versions)
    manifest = RunManifest(
        run_id=manifest_input.run_id,
        created_at=datetime.now(UTC),
        code_version=manifest_input.code_version,
        schema_versions=schema_versions,
        run_config_version=manifest_input.run_config_version,
        stage_machine_version=manifest_input.stage_machine_version,
        validator_suite_version=manifest_input.validator_suite_version,
        selection_protocol_version=manifest_input.selection_protocol_version,
        model_portfolio=manifest_input.model_portfolio,
        branching_policy=manifest_input.branching_policy,
        ensemble_policy=manifest_input.ensemble_policy,
        iteration_caps=manifest_input.iteration_caps,
        tool_policy_version=manifest_input.tool_policy_version,
        tools_enabled=manifest_input.tools_enabled,
        consent_profile=manifest_input.consent_profile,
        determinism_disclaimer=manifest_input.determinism_disclaimer,
        prompt_pack_hash=manifest_input.prompt_pack_hash,
        config_hash=config_hash,
        parent_run_id=manifest_input.parent_run_id,
        forked_from_checkpoint_id=manifest_input.forked_from_checkpoint_id,
    )
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def load_manifest(run_root: Path) -> RunManifest:
    path = _manifest_path(run_root)
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))

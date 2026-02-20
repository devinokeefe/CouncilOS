from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", ser_json_exclude_none=True)


class HandoffArtifactRef(HandoffModel):
    path: str
    sha256: str


class HandoffPlanRef(HandoffArtifactRef):
    plan_content_hash: str


class ApprovalRef(HandoffModel):
    required: bool
    path: str | None = None
    sha256: str | None = None
    approved_plan_hash: str | None = None


class ClarificationsRef(HandoffModel):
    required: bool
    path: str | None = None
    sha256: str | None = None


class RepoSnapshot(HandoffModel):
    commit_sha: str
    branch: str
    dirty: bool
    tree_hash: str | None = None


class HashSpec(HandoffModel):
    schema_version: str = "hash_spec.v1"
    hash_spec_version: str = "1"
    hash_alg: str = "sha256"
    canonical_json: str = "JCS-like-v1"
    excluded_fields_by_pointer: list[str] = Field(default_factory=list)


class ClarificationSummary(HandoffModel):
    question_id: str
    resolved_value: Any
    source: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class PlanReviewRoundRef(HandoffModel):
    round: int
    feedback_ref: HandoffArtifactRef
    diff_ref: HandoffArtifactRef | None = None


class HumanFeedbackBundle(HandoffModel):
    schema_version: str = "2.1.0"
    clarifications: list[ClarificationSummary] = Field(default_factory=list)
    clarification_resolutions_ref: HandoffArtifactRef | None = None
    plan_review_rounds: list[PlanReviewRoundRef] = Field(default_factory=list)
    approval_ref: HandoffArtifactRef | None = None
    notes: list[str] = Field(default_factory=list)


class PlanFreezeRecord(HandoffModel):
    schema_version: str = "2.1.0"
    plan_id: str
    planning_run_id: str
    frozen_at: str
    plan_package_final_ref: HandoffArtifactRef
    plan_content_hash: str
    hash_spec_version: str | None = None
    selected_draft_ref: str | None = None
    selected_draft_hash: str | None = None
    interactive_flags: dict[str, bool]
    clarification_resolutions_ref: HandoffArtifactRef | None = None
    plan_approval_ref: HandoffArtifactRef | None = None
    approved_plan_hash: str | None = None
    repo_snapshot_ref: HandoffArtifactRef | None = None
    hash_spec_ref: HandoffArtifactRef | None = None
    config_snapshot_ref: HandoffArtifactRef
    human_feedback_bundle_ref: HandoffArtifactRef
    notes: str | None = None


class PlanningHandoffBundle(HandoffModel):
    schema_version: str = "2.1.0"
    handoff_bundle_id: str
    planning_run_id: str
    implementation_run_id: str | None
    final_plan: HandoffPlanRef
    freeze_record: HandoffArtifactRef
    approval: ApprovalRef
    clarifications: ClarificationsRef
    human_feedback_bundle: HandoffArtifactRef
    config_snapshot: HandoffArtifactRef
    repo_snapshot: RepoSnapshot
    required_artifacts: list[HandoffArtifactRef]
    optional_artifacts: list[HandoffArtifactRef] = Field(default_factory=list)
    handoff_digest: str


class ManifestArtifactRef(HandoffModel):
    ref: str
    digest: str
    schema_version: str
    role: str


class ManifestPointers(HandoffModel):
    plan_ref: str
    freeze_record_ref: str
    hash_spec_ref: str


class RepoAcquisitionSpec(HandoffModel):
    repo_url: str
    git_commit: str
    git_tree_hash: str | None = None
    submodules: str | None = None
    fetch_depth: int | None = None


class HandoffManifest(HandoffModel):
    schema_version: str = "handoff_manifest.v1"
    hash_spec_version: str
    pointers: ManifestPointers
    repo_acquisition_spec: RepoAcquisitionSpec
    artifacts: list[ManifestArtifactRef]
    env_requirements_ref: str | None = None
    handoff_digest: str


class ImportedArtifactRef(HandoffModel):
    ref: str
    digest: str


class HandoffAck(HandoffModel):
    schema_version: str = "2.1.0"
    implementation_run_id: str
    accepted_at: str
    accepted_handoff_digest: str
    accepted_plan_content_hash: str
    accepted_repo_commit: str
    accepted_repo_tree_hash: str | None = None
    accepted_config_sha256: str
    implementation_engine_version: str
    env_fingerprint: str | None = None
    imported_artifacts: list[ImportedArtifactRef] = Field(default_factory=list)
    acquired_repo: dict[str, Any] | None = None


class HandoffRejection(HandoffModel):
    schema_version: str = "2.1.0"
    rejected_at: str
    reason: str
    reason_code: str | None = None
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    remediation_hints: list[str] = Field(default_factory=list)


class HandoffOverride(HandoffModel):
    schema_version: str = "handoff_override.v1"
    override_type: str
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None
    new_baseline_snapshot_ref: str | None = None

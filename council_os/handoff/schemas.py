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
    interactive_flags: dict[str, bool]
    clarification_resolutions_ref: HandoffArtifactRef | None = None
    plan_approval_ref: HandoffArtifactRef | None = None
    approved_plan_hash: str | None = None
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


class HandoffAck(HandoffModel):
    schema_version: str = "2.1.0"
    implementation_run_id: str
    accepted_at: str
    accepted_handoff_digest: str
    accepted_plan_content_hash: str
    accepted_repo_commit: str
    accepted_config_sha256: str
    implementation_engine_version: str


class HandoffRejection(HandoffModel):
    schema_version: str = "2.1.0"
    rejected_at: str
    reason: str
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)

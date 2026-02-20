from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"

ArtifactType = Literal[
    "project_capsule",
    "clarification_plan",
    "requirements_draft",
    "requirements_merge",
    "requirements_canonical",
    "acceptance_draft",
    "acceptance_canonical",
    "architecture_options",
    "architecture_options_draft",
    "architecture_merge",
    "architecture_canonical",
    "qa_strategy",
    "qa_templates_draft",
    "qa_strategy_canonical",
    "governance_draft",
    "risk_gov_draft",
    "risk_gov_merge",
    "risk_gov_canonical",
    "alignment_warnings",
    "branch_bundle",
    "branch_set",
    "candidate_delta",
    "plan_candidate",
    "plan_package",
    "plan_frozen",
    "validator_report",
    "triage_findings",
    "triage_report",
    "patch_or_reroute",
    "patch_apply_result",
    "consistency_findings",
    "trace_graph_findings",
    "failure_mode_findings",
    "judge_pairwise_result",
    "arbitration_decision",
    "freeze_record",
    "decision_record",
    "markdown_render",
    "run_metrics",
]

Priority = Literal["MUST", "SHOULD", "COULD"]
Severity = Literal["low", "medium", "high", "critical"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _is_valid_json_pointer(pointer: str) -> bool:
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    idx = 1
    length = len(pointer)
    while idx < length:
        ch = pointer[idx]
        if ch == "~":
            if idx + 1 >= length:
                return False
            nxt = pointer[idx + 1]
            if nxt not in {"0", "1"}:
                return False
            idx += 2
            continue
        idx += 1
    return True


class Constraint(StrictModel):
    id: str
    type: Literal["time", "budget", "tech", "policy", "legal", "compliance", "org", "other"]
    text: str


class Assumption(StrictModel):
    id: str
    text: str
    impact: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    needs_confirmation: bool


class OpenQuestion(StrictModel):
    id: str
    text: str
    impact: Literal["low", "medium", "high"]
    blocking: bool


class ProjectCapsulePayload(StrictModel):
    brief: str
    problem_statement: str
    goals: list[str]
    non_goals: list[str]
    constraints: list[Constraint]
    assumptions: list[Assumption]
    open_questions: list[OpenQuestion]
    success_metrics: list[str]
    stakeholders: list[str] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)


class PlanScope(StrictModel):
    in_scope_paths: list[str] = Field(default_factory=list)
    out_of_scope_paths: list[str] = Field(default_factory=list)


class PlanCaps(StrictModel):
    max_files_changed: int | None = None
    max_loc_changed: int | None = None
    max_dep_changes: int | None = None


class PlanConstraints(StrictModel):
    caps: PlanCaps | None = None


class ClarificationItem(StrictModel):
    id: str
    text: str
    impact: Literal["low", "medium", "high"]
    blocking: bool
    rationale: str | None = None
    evidence: list[EvidencePointer] = Field(default_factory=list)


class ClarificationPlanPayload(StrictModel):
    questions: list[ClarificationItem]
    assumptions: list[Assumption] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class GlossaryItem(StrictModel):
    term: str
    definition: str


class Requirement(StrictModel):
    id: str
    priority: Priority
    text: str
    rationale: str


class RequirementsDraftPayload(StrictModel):
    requirements: list[Requirement]


class RequirementMergeItem(StrictModel):
    id: str
    priority: Priority
    text: str
    rationale: str
    source_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidencePointer] = Field(default_factory=list)


class RequirementsMergePayload(StrictModel):
    units: list[RequirementMergeItem]
    merge_log: list[str] = Field(default_factory=list)


class RequirementsCanonicalPayload(StrictModel):
    requirements: list[Requirement]
    id_map: dict[str, str]
    merge_log: list[str] = Field(default_factory=list)


class AcceptanceTest(StrictModel):
    id: str
    maps_to_requirements: list[str]
    type: Literal["unit", "integration", "system", "human_eval", "metric"]
    procedure: str
    pass_criteria: str


class AcceptanceDraftPayload(StrictModel):
    acceptance_tests: list[AcceptanceTest]


class AcceptanceCanonicalPayload(StrictModel):
    acceptance_tests: list[AcceptanceTest]
    id_map: dict[str, str]


class QaTemplate(StrictModel):
    id: str
    name: str
    description: str
    applies_to: list[str] = Field(default_factory=list)


class QaTemplatesDraftPayload(StrictModel):
    templates: list[QaTemplate]
    coverage_guidance: list[str]
    definition_of_done: list[str]


class Component(StrictModel):
    id: str
    name: str
    responsibilities: list[str]


class Interface(StrictModel):
    id: str
    from_component: str = Field(alias="from")
    to: str
    contract: str


class Tradeoffs(StrictModel):
    pros: list[str]
    cons: list[str]


class ArchitectureOption(StrictModel):
    id: str
    summary: str
    components: list[Component]
    interfaces: list[Interface]
    tradeoffs: Tradeoffs
    risks: list[str]


class ArchitectureOptionsPayload(StrictModel):
    options: list[ArchitectureOption]


class ArchitectureOptionMerge(StrictModel):
    id: str
    summary: str
    components: list[Component]
    interfaces: list[Interface]
    tradeoffs: Tradeoffs
    risks: list[str]
    source_ids: list[str] = Field(default_factory=list)


class ArchitectureMergePayload(StrictModel):
    options: list[ArchitectureOptionMerge]
    merge_log: list[str] = Field(default_factory=list)


class ArchitectureCanonicalPayload(StrictModel):
    options: list[ArchitectureOption]
    option_id_map: dict[str, str]
    component_id_map: dict[str, str]
    interface_id_map: dict[str, str]
    merge_log: list[str] = Field(default_factory=list)


class QaStrategyPayload(StrictModel):
    test_strategy: str
    quality_gates: list[str]
    observability_plan: list[str]
    nonfunctional_test_areas: list[str]


class QaStrategyCanonicalPayload(StrictModel):
    templates: list[QaTemplate]
    coverage_guidance: list[str]
    definition_of_done: list[str]
    merge_log: list[str] = Field(default_factory=list)


class Mitigation(StrictModel):
    text: str
    owner: str
    status: Literal["planned", "in_progress", "done"]


class Acceptance(StrictModel):
    rationale: str
    signoff: str


class RiskItem(StrictModel):
    id: str
    severity: Severity
    description: str
    mitigation: Mitigation | None
    acceptance: Acceptance | None


class RiskItemMerge(StrictModel):
    id: str
    severity: Severity
    description: str
    mitigation: Mitigation | None
    acceptance: Acceptance | None
    source_ids: list[str] = Field(default_factory=list)


class ToolPolicyStage(StrictModel):
    stage: str
    allowlisted_tools: list[str]
    restrictions: list[dict[str, object]]
    hitl_triggers: list[str]


class ToolPolicy(StrictModel):
    stage_policies: list[ToolPolicyStage]


class HitlPolicy(StrictModel):
    when_to_interrupt: list[str]
    approval_roles: list[str]


class GovernanceDraftPayload(StrictModel):
    risk_register: list[RiskItem]
    tool_policy: ToolPolicy
    hitl_policy: HitlPolicy


class RiskGovDraftPayload(StrictModel):
    risk_register: list[RiskItem]
    tool_policy: ToolPolicy
    hitl_policy: HitlPolicy
    taxonomy_categories: list[str]


class RiskGovMergePayload(StrictModel):
    risk_register: list[RiskItemMerge]
    tool_policy: ToolPolicy
    hitl_policy: HitlPolicy
    taxonomy_categories: list[str]
    merge_log: list[str] = Field(default_factory=list)


class RiskGovCanonicalPayload(StrictModel):
    risk_register: list[RiskItem]
    tool_policy: ToolPolicy
    hitl_policy: HitlPolicy
    taxonomy_categories: list[str]
    risk_id_map: dict[str, str]
    merge_log: list[str] = Field(default_factory=list)


class BranchBundlePayload(StrictModel):
    class MilestoneStrategy(StrictModel):
        style: Literal["thin_slice", "big_bang", "hybrid"]
        notes: str

    class AcceptanceStrategy(StrictModel):
        notes: str

    class GovernancePosture(StrictModel):
        tooling_level: Literal["none", "sandboxed", "full"]
        hitl_strictness: Literal["high", "medium", "low"]

    class RiskPosture(StrictModel):
        notes: str

    class PreSynthesisScores(StrictModel):
        feasibility: float = Field(ge=0.0, le=1.0)
        complexity_risk: float = Field(ge=0.0, le=1.0)
        clarity: float = Field(ge=0.0, le=1.0)
        overall: float = Field(ge=0.0, le=1.0)

    branch_id: str
    selected_arch_option: str
    milestone_strategy: MilestoneStrategy
    acceptance_strategy: AcceptanceStrategy
    governance_posture: GovernancePosture
    risk_posture: RiskPosture
    pre_synthesis_scores: PreSynthesisScores
    rationale: str


class BranchSpec(StrictModel):
    class MilestoneStrategy(StrictModel):
        style: Literal["thin_slice", "big_bang", "hybrid"]
        notes: str

    class AcceptanceStrategy(StrictModel):
        notes: str

    class GovernancePosture(StrictModel):
        tooling_level: Literal["none", "sandboxed", "full"]
        hitl_strictness: Literal["high", "medium", "low"]

    class RiskPosture(StrictModel):
        notes: str

    branch_id: str
    selected_arch_option_id: str
    milestone_strategy: MilestoneStrategy
    acceptance_strategy: AcceptanceStrategy
    governance_posture: GovernancePosture
    risk_posture: RiskPosture
    rationale: str


class BranchSetPayload(StrictModel):
    branches: list[BranchSpec]


class CandidateInputs(StrictModel):
    capsule_ref: str
    draft_refs: list[str]
    branch_ref: str


class EvidencePointer(StrictModel):
    artifact_ref: str
    entity_id: str | None = None
    json_pointer: str
    note: str | None = None
    candidate_id: str | None = None
    artifact_id: str | None = None
    quote: str | None = Field(default=None, max_length=200)

    @field_validator("quote", mode="before")
    @classmethod
    def _truncate_quote(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > 200:
            return value[:200]
        return value

    @field_validator("json_pointer")
    @classmethod
    def _validate_json_pointer(cls, value: str) -> str:
        if _is_valid_json_pointer(value):
            return value
        raise ValueError("json_pointer must be a valid RFC 6901 JSON Pointer")


class AlignmentWarning(StrictModel):
    id: str
    severity: Severity
    summary: str
    evidence: list[EvidencePointer]
    routing_target: str


class AlignmentWarningsPayload(StrictModel):
    warnings: list[AlignmentWarning]


class ArchitectureChosen(StrictModel):
    option_id: str
    rationale: str
    high_level_dataflow: str
    key_design_decisions: list[str]


class ArchitectureSection(StrictModel):
    options: list[ArchitectureOption]
    chosen: ArchitectureChosen


class MilestoneDeliverable(StrictModel):
    id: str
    text: str


class Milestone(StrictModel):
    id: str
    name: str
    deliverables: list[MilestoneDeliverable]
    exit_criteria: list[str]
    depends_on: list[str] = Field(default_factory=list)


class FailureModeClosure(StrictModel):
    status: Literal["mitigated", "accepted"]
    rationale: str
    signoff: str


class FailureModeItem(StrictModel):
    id: str
    taxonomy: str
    category: str
    finding: str
    severity: Severity
    closure: FailureModeClosure


class GovernanceSection(StrictModel):
    failure_modes: list[FailureModeItem]
    tool_policy: ToolPolicy
    hitl_policy: HitlPolicy


class DecisionLogItem(StrictModel):
    id: str
    question: str
    choice: str
    rationale: str
    alternatives_considered: list[str]
    what_would_change_this_decision: list[str]
    evidence_pointers: list[EvidencePointer]


class SupplementalEvaluation(StrictModel):
    title: str
    notes: str
    evidence: list[EvidencePointer] = Field(default_factory=list)


class SynthesisWarning(StrictModel):
    id: str
    severity: Severity
    summary: str
    routing_target: str
    evidence: list[EvidencePointer] = Field(default_factory=list)


class CandidateDeltaPayload(StrictModel):
    candidate_id: str
    executive_summary: str
    architecture_choice: ArchitectureChosen
    milestones: list[Milestone]
    decision_log: list[DecisionLogItem]
    supplemental_evaluations: list[SupplementalEvaluation] = Field(default_factory=list)
    synthesis_warnings: list[SynthesisWarning] = Field(default_factory=list)


class PlanMeta(StrictModel):
    model_config = ConfigDict(extra="forbid", ser_json_exclude_none=True)

    plan_id: UUID
    version: str = Field(pattern=r"^(v\d+|vFinal)$")
    created_at: datetime
    source_run_id: UUID
    schema_version: str
    candidate_id: str
    branch_id: str
    plan_status: Literal["CANDIDATE", "FROZEN"]
    parent_plan_hash: str | None = None
    amendment_id: str | None = None
    hash_spec_version: str | None = None


class PlanPackage(StrictModel):
    meta: PlanMeta
    project_capsule: ProjectCapsulePayload
    scope: PlanScope | None = None
    constraints: PlanConstraints | None = None
    executive_summary: str | None = None
    requirements: list[Requirement]
    acceptance_tests: list[AcceptanceTest]
    architecture: ArchitectureSection
    milestones: list[Milestone]
    risk_register: list[RiskItem]
    governance: GovernanceSection
    decision_log: list[DecisionLogItem]
    supplemental_evaluations: list[SupplementalEvaluation] = Field(default_factory=list)
    synthesis_warnings: list[SynthesisWarning] = Field(default_factory=list)
    class Appendix(StrictModel):
        glossary: list[GlossaryItem] = Field(default_factory=list)

    appendix: Appendix | None = None


class PlanCandidatePayload(StrictModel):
    candidate_id: str
    branch_id: str
    synthesizer_id: str
    inputs: CandidateInputs
    plan_package: PlanPackage


class PlanFrozenPayload(StrictModel):
    candidate_id: str
    branch_id: str
    synthesizer_id: str
    inputs: CandidateInputs
    plan_package: PlanPackage
    plan_status: Literal["FROZEN"]
    freeze_record_ref: str


class Violation(StrictModel):
    severity: Severity
    json_pointer: str
    message: str
    repair_hint: str | None = None


class ValidatorCheck(StrictModel):
    name: str
    hard_gate: bool
    passed: bool = Field(alias="pass")
    violations: list[Violation]


class ValidatorReportPayload(StrictModel):
    candidate_ref: str
    overall_status: Literal["PASS", "FAIL_REPAIRABLE", "FAIL_STRUCTURAL"]
    checks: list[ValidatorCheck]


class TriageDefect(StrictModel):
    label: Literal["blocker", "fix", "note"]
    severity: Severity
    summary: str
    evidence: list[EvidencePointer]
    violated_gate: str
    patch: list[dict[str, Any]]


class TriageFindingsPayload(StrictModel):
    candidate_ref: str
    critic_id: Literal["A", "B", "MERGED"]
    verdict: Literal["approve", "approve_with_fixes", "reject"]
    defects: list[TriageDefect]
    reroute_target: str | None = None
    reroute_reason: str | None = None


class ConsistencyFinding(StrictModel):
    id: str
    severity: Severity
    summary: str
    evidence: list[EvidencePointer]
    routing_target: str


class ConsistencyFindingsPayload(StrictModel):
    candidate_ref: str
    findings: list[ConsistencyFinding]


class TraceGraphFinding(StrictModel):
    id: str
    severity: Severity
    summary: str
    evidence: list[EvidencePointer]
    routing_target: str


class TraceGraphFindingsPayload(StrictModel):
    candidate_ref: str
    findings: list[TraceGraphFinding]


class PatchOperation(StrictModel):
    op: Literal["add", "remove", "replace", "move", "copy", "test"]
    path: str
    from_path: str | None = Field(default=None, alias="from")
    value: Any | None = None


class PatchOrReroutePayload(StrictModel):
    action: Literal["patch", "reroute"]
    rationale: str
    patch: list[PatchOperation] = Field(default_factory=list)
    reroute_target: str | None = None


class PatchApplyCandidateResult(StrictModel):
    patch_ref: str
    applied: bool
    validator_status: Literal["PASS", "FAIL_REPAIRABLE", "FAIL_STRUCTURAL"]
    op_count: int
    paths_touched: int
    reason: str | None = None


class PatchApplyResultPayload(StrictModel):
    candidate_ref: str
    selected_patch_ref: str | None = None
    reroute_target: str | None = None
    applied_candidate_ref: str | None = None
    candidates: list[PatchApplyCandidateResult]


class ArbitrationDecisionPayload(StrictModel):
    decision: Literal["select_winner", "request_hitl"]
    winner_ref: str | None = None
    rationale: str
    evidence: list[EvidencePointer] = Field(default_factory=list)


class DecisionRecordPayload(StrictModel):
    winner_ref: str
    winner_summary: str
    reasons: list[str]
    tradeoffs: list[str] = Field(default_factory=list)
    evidence: list[EvidencePointer] = Field(default_factory=list)
    runner_ups: list[str] = Field(default_factory=list)


class MarkdownRenderPayload(StrictModel):
    markdown: str


class FailureModeFindingItem(StrictModel):
    id: str
    category: str
    severity: Severity
    finding: str
    required_closure: Literal["mitigate", "accept"]
    status: Literal["open", "mitigated", "accepted"]
    closure: dict[str, str]


class FailureModeFindingsPayload(StrictModel):
    candidate_ref: str
    taxonomy: str
    critical_findings: list[FailureModeFindingItem]
    closure_pass: bool


class JudgePairwisePayload(StrictModel):
    comparison: dict[str, str]
    judge_id: str
    judge_model: str
    winner: Literal["a", "b"]
    scores: dict[str, int]
    evidence: list[EvidencePointer]
    notes: str

    @field_validator("comparison")
    @classmethod
    def _validate_comparison(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value.keys()) != {"a", "b"}:
            raise ValueError("comparison must contain exactly keys 'a' and 'b'")
        if not value["a"] or not value["b"]:
            raise ValueError("comparison ids must be non-empty")
        return value

    @field_validator("scores")
    @classmethod
    def _validate_scores_raw(cls, value: dict[str, int]) -> dict[str, int]:
        if not isinstance(value, dict):
            raise ValueError("scores must be a dict")
        return value

    @model_validator(mode="after")
    def _normalize_scores(self) -> "JudgePairwisePayload":
        required = {"feasibility", "testability", "governance", "architecture", "clarity"}

        def validate_range(scores: dict[str, int]) -> None:
            for key, score in scores.items():
                if not isinstance(score, int):
                    raise ValueError(f"score for {key} must be an integer")
                if score < 0 or score > 10:
                    raise ValueError(f"score out of range for {key}: {score}")

        keys = set(self.scores.keys())
        if keys == required:
            validate_range(self.scores)
            return self

        suffix_a = {f"{k}_a" for k in required}
        suffix_b = {f"{k}_b" for k in required}
        if keys == suffix_a | suffix_b:
            winner = self.winner
            normalized = {k: int(self.scores[f"{k}_{winner}"]) for k in required}
            validate_range(normalized)
            self.scores = normalized
            return self

        prefix_a = {f"a_{k}" for k in required}
        prefix_b = {f"b_{k}" for k in required}
        if keys == prefix_a | prefix_b:
            winner = self.winner
            normalized = {k: int(self.scores[f"{winner}_{k}"]) for k in required}
            validate_range(normalized)
            self.scores = normalized
            return self

        # Nested format: {"feasibility": {"a": 7, "b": 6}, ...}
        if keys == required and all(isinstance(v, dict) for v in self.scores.values()):
            winner = self.winner
            normalized: dict[str, int] = {}
            for key in required:
                pair = self.scores.get(key, {})
                if not isinstance(pair, dict) or set(pair.keys()) != {"a", "b"}:
                    raise ValueError("scores must contain feasibility/testability/governance/architecture/clarity")
                normalized[key] = int(pair[winner])
            validate_range(normalized)
            self.scores = normalized
            return self

        raise ValueError("scores must contain feasibility/testability/governance/architecture/clarity")


class FreezeTradeoff(StrictModel):
    topic: str
    winner_reason: str
    runner_up_reason: str


class FreezeRecordPayload(StrictModel):
    winner_candidate_ref: str
    frozen_plan_ref: str
    why_winner: list[str]
    tradeoffs: list[FreezeTradeoff]
    judge_summary_refs: list[str]
    validator_summary_ref: str
    remaining_open_questions: list[str]
    accepted_risks: list[str]


class RunMetricsPayload(StrictModel):
    run_id: str
    stage_durations_sec: dict[str, float]
    candidate_count: int
    repaired_candidate_count: int
    validator_pass_count: int
    validator_fail_count: int
    frozen_count: int
    total_duration_sec: float


class ArtifactEnvelope(StrictModel):
    artifact_type: ArtifactType
    artifact_id: str
    schema_version: str
    created_at: datetime
    source_run_id: UUID
    parents: list[str]
    payload: dict[str, Any]


def deterministic_json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def payload_model_for(artifact_type: ArtifactType) -> type[StrictModel]:
    mapping: dict[ArtifactType, type[StrictModel]] = {
        "project_capsule": ProjectCapsulePayload,
        "clarification_plan": ClarificationPlanPayload,
        "requirements_draft": RequirementsDraftPayload,
        "requirements_merge": RequirementsMergePayload,
        "requirements_canonical": RequirementsCanonicalPayload,
        "acceptance_draft": AcceptanceDraftPayload,
        "acceptance_canonical": AcceptanceCanonicalPayload,
        "architecture_options": ArchitectureOptionsPayload,
        "architecture_options_draft": ArchitectureOptionsPayload,
        "architecture_merge": ArchitectureMergePayload,
        "architecture_canonical": ArchitectureCanonicalPayload,
        "qa_strategy": QaStrategyPayload,
        "qa_templates_draft": QaTemplatesDraftPayload,
        "qa_strategy_canonical": QaStrategyCanonicalPayload,
        "governance_draft": GovernanceDraftPayload,
        "risk_gov_draft": RiskGovDraftPayload,
        "risk_gov_merge": RiskGovMergePayload,
        "risk_gov_canonical": RiskGovCanonicalPayload,
        "alignment_warnings": AlignmentWarningsPayload,
        "branch_bundle": BranchBundlePayload,
        "branch_set": BranchSetPayload,
        "candidate_delta": CandidateDeltaPayload,
        "plan_candidate": PlanCandidatePayload,
        "plan_package": PlanCandidatePayload,
        "plan_frozen": PlanFrozenPayload,
        "validator_report": ValidatorReportPayload,
        "triage_findings": TriageFindingsPayload,
        "triage_report": TriageFindingsPayload,
        "patch_or_reroute": PatchOrReroutePayload,
        "patch_apply_result": PatchApplyResultPayload,
        "consistency_findings": ConsistencyFindingsPayload,
        "trace_graph_findings": TraceGraphFindingsPayload,
        "failure_mode_findings": FailureModeFindingsPayload,
        "judge_pairwise_result": JudgePairwisePayload,
        "arbitration_decision": ArbitrationDecisionPayload,
        "freeze_record": FreezeRecordPayload,
        "decision_record": DecisionRecordPayload,
        "markdown_render": MarkdownRenderPayload,
        "run_metrics": RunMetricsPayload,
    }
    return mapping[artifact_type]


def validate_artifact_payload(envelope: ArtifactEnvelope) -> None:
    model = payload_model_for(envelope.artifact_type)
    model.model_validate(envelope.payload)


SCHEMA_FILE_REGISTRY: dict[str, type[StrictModel]] = {
    "project_capsule.v1.json": ProjectCapsulePayload,
    "clarification_plan.v1.json": ClarificationPlanPayload,
    "requirements_draft.v1.json": RequirementsDraftPayload,
    "requirements_merge.v1.json": RequirementsMergePayload,
    "requirements_canonical.v1.json": RequirementsCanonicalPayload,
    "architecture_options_draft.v1.json": ArchitectureOptionsPayload,
    "architecture_merge.v1.json": ArchitectureMergePayload,
    "architecture_canonical.v1.json": ArchitectureCanonicalPayload,
    "qa_templates_draft.v1.json": QaTemplatesDraftPayload,
    "qa_strategy_canonical.v1.json": QaStrategyCanonicalPayload,
    "risk_gov_draft.v1.json": RiskGovDraftPayload,
    "risk_gov_merge.v1.json": RiskGovMergePayload,
    "risk_gov_canonical.v1.json": RiskGovCanonicalPayload,
    "acceptance_draft.v1.json": AcceptanceDraftPayload,
    "acceptance_canonical.v1.json": AcceptanceCanonicalPayload,
    "alignment_warnings.v1.json": AlignmentWarningsPayload,
    "branch_set.v1.json": BranchSetPayload,
    "candidate_delta.v1.json": CandidateDeltaPayload,
    "plan_package.v1.json": PlanCandidatePayload,
    "consistency_findings.v1.json": ConsistencyFindingsPayload,
    "trace_graph_findings.v1.json": TraceGraphFindingsPayload,
    "validator_report.v1.json": ValidatorReportPayload,
    "triage_report.v1.json": TriageFindingsPayload,
    "patch_or_reroute.v1.json": PatchOrReroutePayload,
    "patch_apply_result.v1.json": PatchApplyResultPayload,
    "failure_mode_findings.v1.json": FailureModeFindingsPayload,
    "judge_pairwise_result.v1.json": JudgePairwisePayload,
    "arbitration_decision.v1.json": ArbitrationDecisionPayload,
    "freeze_record.v1.json": FreezeRecordPayload,
    "decision_record.v1.json": DecisionRecordPayload,
    "markdown_render.v1.json": MarkdownRenderPayload,
}


def schema_model_for(schema_path: str) -> type[StrictModel]:
    name = Path(schema_path).name
    model = SCHEMA_FILE_REGISTRY.get(name)
    if model is None:
        raise KeyError(f"Unknown schema file: {schema_path}")
    return model


def export_schema_files(schema_dir: Path) -> None:
    schema_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMA_FILE_REGISTRY.items():
        schema_path = schema_dir / filename
        payload = model.model_json_schema()
        schema_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

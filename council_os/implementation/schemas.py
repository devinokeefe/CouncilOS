from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

IMPL_SCHEMA_VERSION_V1 = "1.0.0"
IMPL_SCHEMA_VERSION_V2 = "2.0.0"
IMPL_SCHEMA_VERSION_V21 = "2.1.0"
IMPL_SCHEMA_VERSION = IMPL_SCHEMA_VERSION_V1

ImplementationArtifactType = Literal[
    "plan_package_final",
    "planning_handoff_bundle",
    "repo_context",
    "workspace_context",
    "work_plan",
    "patchset",
    "repo_snapshot",
    "test_results",
    "quality_reports",
    "trace_report",
    "review_findings",
    "release_bundle",
    "decision_record",
    "stage_handoff",
    "workspace_index",
    "context_pack",
    "change_request",
    "conflict_report",
    "integration_report",
    "execution_profile_catalog",
    "tool_registry",
    "tool_probe_results",
    "expectation_registry",
    "evidence_index",
    "diff_report",
    "remote_ops_manifest",
    "remote_ops_results",
    "remote_op_events",
    "remote_run_cache_index",
    "attestation_bundle",
    "research_contracts",
    "experiment_manifest",
    "experiment_results",
    "replication_report",
    "portfolio_decisions",
    "program_graph",
    "verification_plan",
    "work_graph",
    "work_graph_events",
    "assumption_registry",
    "surface_leases",
    "job_spec",
    "job_result",
    "job_events",
    "change_intent",
    "apply_record",
    "world_state_snapshot",
    "drift_report",
    "diagnosis_report",
    "interface_contracts",
]

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


class EvidencePointer(StrictModel):
    artifact_ref: str
    entity_id: str | None = None
    json_pointer: str
    note: str | None = None

    @field_validator("json_pointer")
    @classmethod
    def _validate_json_pointer(cls, value: str) -> str:
        if _is_valid_json_pointer(value):
            return value
        raise ValueError("json_pointer must be a valid RFC 6901 JSON Pointer")


class RepoBuildEntrypoint(StrictModel):
    name: str
    command: list[str]
    working_dir: str | None = None


class RepoValidatorEntrypoint(StrictModel):
    id: str
    name: str
    command: list[str] | None = None
    mode: Literal["command", "mock"] = "command"
    outcomes: list[Literal["pass", "fail"]] = Field(default_factory=list)
    maps_to_acceptance_tests: list[str] = Field(default_factory=list)
    retryable: bool = True


class RepoContextPayload(StrictModel):
    repo_root: str
    base_commit: str
    head_commit: str
    branch_name: str
    build_entrypoints: list[RepoBuildEntrypoint]
    validator_entrypoints: list[RepoValidatorEntrypoint]
    protected_paths: list[str]
    forbidden_paths: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionProfilesConfig(StrictModel):
    catalog_ref: str | None = None
    allowed_profile_ids: list[str] = Field(default_factory=list)
    default_profile_id_by_stage: dict[str, str] = Field(default_factory=dict)


class ValidatorRunner(StrictModel):
    name: str
    cmd: str
    profile_id: str


class DependencyPolicy(StrictModel):
    network_installs_allowed: bool = False
    required_lockfiles: list[str] = Field(default_factory=list)


class SurfaceBudget(StrictModel):
    max_bytes: int | None = None
    max_files: int | None = None
    max_breaking_changes: int | None = None


class SurfaceDefinition(StrictModel):
    surface_id: str
    type: str
    path_globs: list[str] = Field(default_factory=list)
    interface_contract_id: str | None = None
    change_budget: SurfaceBudget | None = None


class RepoContextPayloadV2(StrictModel):
    repo_root: str
    base_commit: str
    head_commit: str
    branch_name: str
    build_entrypoints: list[RepoBuildEntrypoint]
    validator_entrypoints: list[RepoValidatorEntrypoint]
    protected_paths: list[str]
    forbidden_paths: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    execution_profiles: ExecutionProfilesConfig | None = None
    validator_runners: list[ValidatorRunner] = Field(default_factory=list)
    dependency_policy: DependencyPolicy | None = None


class RepoContextPayloadV21(StrictModel):
    repo_root: str
    base_commit: str
    head_commit: str
    branch_name: str
    build_entrypoints: list[RepoBuildEntrypoint]
    validator_entrypoints: list[RepoValidatorEntrypoint]
    protected_paths: list[str]
    forbidden_paths: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    execution_profiles: ExecutionProfilesConfig | None = None
    validator_runners: list[ValidatorRunner] = Field(default_factory=list)
    dependency_policy: DependencyPolicy | None = None
    surfaces: list[SurfaceDefinition] = Field(default_factory=list)
    default_expectations: list["Expectation"] = Field(default_factory=list)


class NamespaceRule(StrictModel):
    name: str
    path: str
    purpose: str
    canonical: bool = True


class PartitionRule(StrictModel):
    canonical_prefixes: list[str]
    non_canonical_prefixes: list[str]


class RetentionPolicy(StrictModel):
    max_age_days: int
    max_size_mb: int
    compaction_strategy: str


class AccessRule(StrictModel):
    role: str
    permissions: list[Literal["read", "write", "admin"]]


class ContextPackLimits(StrictModel):
    max_items: int
    max_bytes: int


class WorkspaceContextPayload(StrictModel):
    workspace_root: str
    namespace_rules: list[NamespaceRule]
    partition_rules: PartitionRule
    retention_policy: RetentionPolicy
    access_control: list[AccessRule]
    context_pack_limits: ContextPackLimits


class WorkLane(StrictModel):
    lane_id: str
    name: str
    description: str


class WorkTask(StrictModel):
    task_id: str
    title: str
    lane_id: str
    depends_on: list[str] = Field(default_factory=list)
    maps_to_requirements: list[str] = Field(default_factory=list)
    maps_to_acceptance_tests: list[str] = Field(default_factory=list)


class WorkTaskV2(StrictModel):
    task_id: str
    title: str
    lane_id: str
    depends_on: list[str] = Field(default_factory=list)
    maps_to_requirements: list[str] = Field(default_factory=list)
    maps_to_acceptance_tests: list[str] = Field(default_factory=list)
    profile_id: str
    satisfies_expectations: list[str] = Field(default_factory=list)
    verifies_expectations: list[str] = Field(default_factory=list)
    remote_ops: list[str] = Field(default_factory=list)


class PatchsetLimits(StrictModel):
    max_operations: int
    max_bytes: int
    max_operations_total: int | None = None


class PlanningHandoffBundlePayload(StrictModel):
    decision_record: dict[str, Any]
    validator_reports: list[dict[str, Any]]
    failure_mode_findings: list[dict[str, Any]]


class WorkPlanPayload(StrictModel):
    lanes: list[WorkLane]
    tasks: list[WorkTask]
    patchset_limits: PatchsetLimits
    generated_at: datetime


class WorkPlanPayloadV2(StrictModel):
    lanes: list[WorkLane]
    tasks: list[WorkTaskV2]
    patchset_limits: PatchsetLimits
    generated_at: datetime


class RuntimeSpec(StrictModel):
    lang: str
    version: str


class BaseImageSpec(StrictModel):
    name: str
    digest: str


class DependencyPin(StrictModel):
    name: str
    version: str


class DependencyRange(StrictModel):
    name: str
    version_range: str


class DependencyName(StrictModel):
    name: str


class DependencySpec(StrictModel):
    required: list[DependencyPin] = Field(default_factory=list)
    allowed: list[DependencyRange] = Field(default_factory=list)
    forbidden: list[DependencyName] = Field(default_factory=list)


class HardwareSpec(StrictModel):
    gpu: bool = False
    min_vram_gb: int | None = None


class NetworkPolicy(StrictModel):
    default: Literal["deny", "allow"]
    allowlist_domains: list[str] = Field(default_factory=list)
    allowlist_ports: list[int] = Field(default_factory=list)


class PackageManagerPolicy(StrictModel):
    pip_install: Literal["allow", "deny"] = "deny"
    conda_install: Literal["allow", "deny"] = "deny"


class ExecutionProfile(StrictModel):
    profile_id: str
    profile_version: str
    runtime: RuntimeSpec
    base_image: BaseImageSpec
    deps: DependencySpec
    hardware: HardwareSpec | None = None
    network: NetworkPolicy
    package_manager: PackageManagerPolicy | None = None
    tool_adapters: list[str] = Field(default_factory=list)
    env: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_image")
    @classmethod
    def _validate_base_image(cls, value: BaseImageSpec) -> BaseImageSpec:
        if not value.digest:
            raise ValueError("base_image.digest is required")
        if ":" not in value.digest:
            raise ValueError("base_image.digest must be a content digest (e.g. sha256:...)")
        return value

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: NetworkPolicy) -> NetworkPolicy:
        if value.default not in {"deny", "allow"}:
            raise ValueError("network.default must be set")
        if value.default != "deny":
            raise ValueError("network.default must be 'deny' for allowlist-only policy")
        return value


class ExecutionProfileCatalogPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    catalog_id: str
    profiles: list[ExecutionProfile]


class ToolCapabilitySpec(StrictModel):
    declared: list[str] = Field(default_factory=list)
    observed: list[str] = Field(default_factory=list)


class ToolLimitsSpec(StrictModel):
    quota: str | None = None
    rate: str | None = None


class ToolSchemaRefs(StrictModel):
    request_schema_ref: str | None = None
    response_schema_ref: str | None = None


class ToolReliabilitySpec(StrictModel):
    success_rate: float | None = None
    last_failure: str | None = None


class ToolEndpointSpec(StrictModel):
    name: str
    url: str | None = None
    method: str | None = None


class ToolCard(StrictModel):
    tool_id: str
    tool_version: str
    endpoints: list[ToolEndpointSpec] = Field(default_factory=list)
    capabilities: ToolCapabilitySpec = Field(default_factory=ToolCapabilitySpec)
    limits: ToolLimitsSpec = Field(default_factory=ToolLimitsSpec)
    schemas: ToolSchemaRefs = Field(default_factory=ToolSchemaRefs)
    reliability: ToolReliabilitySpec = Field(default_factory=ToolReliabilitySpec)
    last_probe: EvidencePointer | None = None
    probe_status: Literal["probed", "unprobed", "exempt"] = "probed"


class ToolRegistryPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    registry_id: str
    tools: list[ToolCard] = Field(default_factory=list)


class ToolProbeResult(StrictModel):
    probe_run_id: str
    tool_id: str
    tool_version: str
    timestamp: datetime
    request_params: dict[str, Any] = Field(default_factory=dict)
    response_schema_hash: str | None = None
    latency_ms: float | None = None
    status: str
    quota_headers: dict[str, Any] = Field(default_factory=dict)
    attestation_ref: str


class ToolProbeResultsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    probes: list[ToolProbeResult] = Field(default_factory=list)


class ExpectationSource(StrictModel):
    type: str
    id: str


class ExpectationSubject(StrictModel):
    kind: str
    artifact_type: str | None = None
    name: str | None = None


class OracleCheck(StrictModel):
    type: str
    path: str | None = None
    value: Any | None = None
    metric: str | None = None


class OracleSpec(StrictModel):
    type: str
    schema_ref: str | None = None
    checks: list[OracleCheck] = Field(default_factory=list)
    expected_hash: str | None = None
    baseline_ref: str | None = None
    metric: str | None = None
    threshold: float | None = None


class ExpectationInput(StrictModel):
    dataset_snapshot_ref: str


class ToleranceSpec(StrictModel):
    type: str
    abs: float | None = None
    rel: float | None = None


class Expectation(StrictModel):
    expectation_id: str
    level: Literal["plan", "subsystem", "test", "component", "artifact"]
    source: ExpectationSource
    subject: ExpectationSubject
    oracle: OracleSpec
    tolerance: ToleranceSpec | None = None
    inputs: list[ExpectationInput] = Field(default_factory=list)
    parents: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    must_exist_before_remote_ops: bool = False


class ExpectationRegistryPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    registry_id: str
    expectations: list[Expectation] = Field(default_factory=list)


class EvidenceIndexEntry(StrictModel):
    expectation_id: str
    status: Literal["pass", "fail", "waived", "missing", "unknown"]
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)
    waiver_approvals: list[str] = Field(default_factory=list)


class EvidenceIndexPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    evidence: list[EvidenceIndexEntry] = Field(default_factory=list)


class DiffReportPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    expectation_id: str
    run_id: str
    summary: str
    classification: str | None = None
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidencePointer] = Field(default_factory=list)


class RemoteOpBudget(StrictModel):
    time_sec: float | None = None
    cost_usd: float | None = None
    max_calls: int | None = None


class RemoteOpInputs(StrictModel):
    snapshot_refs: list[str] = Field(default_factory=list)


class RemoteOpCanary(StrictModel):
    inputs_subset: list[str] = Field(default_factory=list)
    cost_cap: float | None = None


class RemoteOpScale(StrictModel):
    max_cost: float | None = None
    max_calls: int | None = None


class RemoteOpSpec(StrictModel):
    op_id: str
    task_id: str | None = None
    profile_id: str
    tool_id: str
    idempotency_key: str | None = None
    budget: RemoteOpBudget | None = None
    inputs: RemoteOpInputs | None = None
    expected: list[str] = Field(default_factory=list)
    canary: RemoteOpCanary | None = None
    scale: RemoteOpScale | None = None
    oracle_override: OracleSpec | None = None
    mock_output: Any | None = None
    mock_canary_output: Any | None = None
    mock_attempts: list[str] = Field(default_factory=list)
    mock_cost: float | None = None


class RemoteOpsManifestPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    ops: list[RemoteOpSpec] = Field(default_factory=list)


class RemoteOpAttempt(StrictModel):
    attempt_id: str
    status: str
    error_class: str | None = None
    latency_ms: float | None = None


class RemoteOpResult(StrictModel):
    op_id: str
    status: str
    attempts: list[RemoteOpAttempt] = Field(default_factory=list)
    cost_usd: float | None = None
    time_sec: float | None = None
    calls_used: int | None = None
    output_ref: str | None = None
    output_hash: str | None = None
    evaluator_verdict: Literal["pass", "fail"] | None = None
    diff_report_ref: str | None = None
    attestation_ref: str | None = None


class RemoteOpsResultsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    results: list[RemoteOpResult] = Field(default_factory=list)


class RemoteOpEvent(StrictModel):
    timestamp: datetime
    op_id: str
    event: str
    attempt_id: str | None = None
    status: str | None = None
    detail: str | None = None


class RemoteOpEventsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    events: list[RemoteOpEvent] = Field(default_factory=list)


class RemoteRunCacheEntry(StrictModel):
    idempotency_key: str
    result_ref: str
    created_at: datetime


class RemoteRunCacheIndexPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    entries: list[RemoteRunCacheEntry] = Field(default_factory=list)


class AttestationInputRef(StrictModel):
    artifact_ref: str
    hash: str | None = None


class AttestationBundlePayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    run_id: str
    stage: str
    lane_id: str | None = None
    code_sha: dict[str, str]
    profile_id: str
    base_image_digest: str
    tool_id: str | None = None
    inputs: list[AttestationInputRef] = Field(default_factory=list)
    spec_versions: dict[str, str] = Field(default_factory=dict)
    timestamps: dict[str, str] = Field(default_factory=dict)
    runner_version: dict[str, str] = Field(default_factory=dict)


class ResearchContract(StrictModel):
    contract_id: str
    hypothesis: str
    baseline: str
    evaluation_metrics: list[str]
    decision_rule: str
    replication_rule: str


class ResearchContractsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    contracts: list[ResearchContract] = Field(default_factory=list)


class ExperimentManifestPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    experiment_id: str
    dataset_snapshot_ids: list[str]
    profile_id: str
    seeds: list[int]
    metrics: list[str]
    budget_caps: dict[str, Any] = Field(default_factory=dict)


class ExperimentResultMetric(StrictModel):
    seed: int
    metrics: dict[str, float]


class ExperimentResultsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    experiment_id: str
    results: list[ExperimentResultMetric]
    aggregate_stats: dict[str, float] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    attestation_ref: str


class ReplicationReportPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    experiment_id: str
    comparison: dict[str, Any]
    status: Literal["pass", "fail"]


class PortfolioDecisionsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V2
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class ProgramGraphRequiredEvidence(StrictModel):
    artifact_type: str
    env_class: str | None = None


class ProgramGraphPolicyGate(StrictModel):
    gate_id: str
    requires_hitl: bool = False
    waiver_allowed: bool = True


class ProgramGraphNode(StrictModel):
    id: str
    type: str
    parent_id: str | None = None
    expectation_ids: list[str] = Field(default_factory=list)
    required_evidence: list[ProgramGraphRequiredEvidence] = Field(default_factory=list)
    policy_gates: list[ProgramGraphPolicyGate] = Field(default_factory=list)


class ProgramGraphPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    program_graph_id: str
    nodes: list[ProgramGraphNode] = Field(default_factory=list)


class VerificationSuite(StrictModel):
    suite_id: str
    env_class: str
    runner: str
    gating: Literal["must", "should", "may"] = "must"


class VerificationEvidenceRequirement(StrictModel):
    artifact_type: str
    env_class: str | None = None
    note: str | None = None


class VerificationCheckRequirement(StrictModel):
    program_check_id: str
    required_suites: list[VerificationSuite] = Field(default_factory=list)
    evidence_requirements: list[VerificationEvidenceRequirement] = Field(default_factory=list)


class VerificationPlanPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    verification_plan_id: str
    check_requirements: list[VerificationCheckRequirement] = Field(default_factory=list)


class WorkGraphBudget(StrictModel):
    max_runtime_sec: float | None = None
    max_cost_usd: float | None = None
    max_calls: int | None = None


class WorkGraphTask(StrictModel):
    task_id: str
    kind: str
    deps: list[str] = Field(default_factory=list)
    profile_id: str
    program_check_ids: list[str] = Field(default_factory=list)
    expectation_ids: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)
    job_specs: list[str] = Field(default_factory=list)
    risk_class: Severity = "low"
    budget: WorkGraphBudget | None = None
    rollback_or_comp_plan_ref: str | None = None
    priority: int = 0


class WorkGraphPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    work_graph_id: str
    tasks: list[WorkGraphTask] = Field(default_factory=list)
    generated_at: datetime


class WorkGraphEvent(StrictModel):
    timestamp: datetime
    task_id: str
    event: str
    status: str | None = None
    detail: str | None = None


class WorkGraphEventsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    events: list[WorkGraphEvent] = Field(default_factory=list)


class AssumptionEntry(StrictModel):
    assumption_id: str
    statement: str
    impact: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    validation_task_ids: list[str] = Field(default_factory=list)
    status: Literal["unvalidated", "validated", "rejected", "accepted_with_monitoring"]
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)


class AssumptionRegistryPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    assumptions: list[AssumptionEntry] = Field(default_factory=list)


class SurfaceRef(StrictModel):
    type: str
    id: str


class LeaseHolder(StrictModel):
    run_id: str
    lane_id: str | None = None
    task_id: str | None = None


class SurfaceLeaseEvent(StrictModel):
    lease_id: str
    surface: SurfaceRef
    holder: LeaseHolder
    ttl_seconds: int
    granted_at: datetime
    expires_at: datetime
    status: Literal["granted", "denied", "renewed", "released", "preempted", "expired"]
    conflicts: list[str] = Field(default_factory=list)
    decision_reason_code: str


class SurfaceLeasesPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    events: list[SurfaceLeaseEvent] = Field(default_factory=list)


class JobSpecInputRef(StrictModel):
    artifact_ref: str | None = None
    snapshot_ref: str | None = None
    note: str | None = None


class JobBudget(StrictModel):
    max_runtime_sec: float | None = None
    max_cost_usd: float | None = None
    max_calls: int | None = None


class JobSpecPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    job_id: str
    job_type: str
    profile_id: str
    inputs: list[JobSpecInputRef] = Field(default_factory=list)
    idempotency_key: str
    budget: JobBudget | None = None
    side_effects: Literal["none", "reversible", "irreversible"] = "none"
    compensation: str | None = None
    expected_expectation_ids: list[str] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    tool_id: str | None = None
    change_intent_id: str | None = None
    mock_output: Any | None = None
    mock_attempts: list[str] = Field(default_factory=list)
    priority: int = 0

    @field_validator("compensation")
    @classmethod
    def _validate_compensation(cls, value: str | None, info: Any) -> str | None:
        side_effects = info.data.get("side_effects") if isinstance(info.data, dict) else "none"
        if side_effects != "none" and not value:
            raise ValueError("compensation required for side_effects != none")
        return value


class JobAttempt(StrictModel):
    attempt_id: str
    status: str
    error_class: str | None = None
    latency_ms: float | None = None


class JobOutputRef(StrictModel):
    artifact_ref: str
    hash: str | None = None


class JobEvaluator(StrictModel):
    verdict: Literal["pass", "fail"] | None = None
    diff_report_ref: str | None = None


class JobResultPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    job_id: str
    status: str
    attempts: list[JobAttempt] = Field(default_factory=list)
    outputs: list[JobOutputRef] = Field(default_factory=list)
    evaluator: JobEvaluator | None = None
    attestation_ref: str | None = None
    attestation: dict[str, Any] | None = None


class JobEvent(StrictModel):
    timestamp: datetime
    job_id: str
    event: str
    status: str | None = None
    detail: str | None = None


class JobEventsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    events: list[JobEvent] = Field(default_factory=list)


class ChangeIntentPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    intent_id: str
    summary: str
    rationale: str
    risk_class: Severity = "medium"
    surfaces: list[str] = Field(default_factory=list)
    rollback_plan: str
    required_approvals: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    status: Literal["proposed", "approved", "rejected"] = "proposed"


class ApplyRecordPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    intent_id: str
    applied_at: datetime
    status: Literal["applied", "failed", "rolled_back"]
    pre_snapshot_ref: str | None = None
    post_snapshot_ref: str | None = None
    drift_report_ref: str | None = None
    rollback_status: str | None = None
    evidence: list[EvidencePointer] = Field(default_factory=list)


class WorldStateResource(StrictModel):
    resource_id: str
    hash: str | None = None


class WorldStateSnapshotPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    snapshot_id: str
    tool_ids: list[str] = Field(default_factory=list)
    resources: list[WorldStateResource] = Field(default_factory=list)
    timestamp: datetime
    env_class: str


class DriftReportPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    drift_id: str
    pre_snapshot_ref: str
    post_snapshot_ref: str
    summary: str
    classification: str
    diffs: list[dict[str, Any]] = Field(default_factory=list)


class DiagnosisBlockedTask(StrictModel):
    task_id: str
    reason: str


class DiagnosisReportPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    status: Literal["stalled"]
    reason_codes: list[str] = Field(default_factory=list)
    blocked_tasks: list[DiagnosisBlockedTask] = Field(default_factory=list)
    replan_tickets: list[str] = Field(default_factory=list)
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)


class InterfaceContract(StrictModel):
    contract_id: str
    description: str
    surfaces: list[str] = Field(default_factory=list)
    expectations: list[str] = Field(default_factory=list)


class InterfaceContractsPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    contracts: list[InterfaceContract] = Field(default_factory=list)


class PatchChange(StrictModel):
    path: str
    action: Literal["add", "modify", "delete"]
    before: str | None = None
    after: str | None = None

    @field_validator("before", "after")
    @classmethod
    def _normalize_newlines(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.replace("\r\n", "\n")

    @field_validator("after")
    @classmethod
    def _validate_after(cls, value: str | None, info: Any) -> str | None:
        action = info.data.get("action") if isinstance(info.data, dict) else None
        if action == "add" and value is None:
            raise ValueError("add action requires after content")
        if action == "modify" and value is None:
            raise ValueError("modify action requires after content")
        return value

    @field_validator("before")
    @classmethod
    def _validate_before(cls, value: str | None, info: Any) -> str | None:
        action = info.data.get("action") if isinstance(info.data, dict) else None
        if action in {"modify", "delete"} and value is None:
            raise ValueError("modify/delete action requires before content")
        return value


class PatchsetPayload(StrictModel):
    patchset_id: str
    lane_id: str | None = None
    base_commit: str
    changes: list[PatchChange]
    apply_order: list[int] = Field(default_factory=list)
    maps_to_requirements: list[str] = Field(default_factory=list)
    maps_to_acceptance_tests: list[str] = Field(default_factory=list)
    semantic_change: bool = False
    change_request_ref: str | None = None
    operation_count: int
    bytes_changed: int


class RepoSnapshotPayload(StrictModel):
    base_commit: str
    head_commit: str
    changed_files: list[str]
    file_hashes: dict[str, str]


class TestOutcome(StrictModel):
    name: str
    status: Literal["pass", "fail", "skipped"]
    duration_sec: float | None = None
    maps_to_acceptance_tests: list[str] = Field(default_factory=list)
    attempts: int | None = None
    quarantined: bool = False


class TestResultsPayload(StrictModel):
    results: list[TestOutcome]
    overall_status: Literal["PASS", "FAIL"]
    quarantined_validators: list[str] = Field(default_factory=list)


class QualityCheck(StrictModel):
    name: str
    status: Literal["pass", "fail", "warn"]
    details: str | None = None


class QualityReportsPayload(StrictModel):
    checks: list[QualityCheck]
    overall_status: Literal["PASS", "FAIL"]


class ReviewFinding(StrictModel):
    id: str
    severity: Severity
    summary: str
    evidence: list[EvidencePointer]
    violated_gate: str


class ReviewFindingsPayload(StrictModel):
    findings: list[ReviewFinding]


class TraceReportPayload(StrictModel):
    trace_complete: bool
    missing_links: list[str]
    evidence: list[EvidencePointer]
    recommendation: Literal["freeze", "no_freeze"]


class ReleaseArtifact(StrictModel):
    name: str
    path: str
    checksum: str | None = None


class ReleaseBundlePayload(StrictModel):
    version: str
    artifacts: list[ReleaseArtifact]
    notes: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class DecisionRecordPayload(StrictModel):
    decision_id: str
    summary: str
    reasons: list[str]
    what_would_change: list[str]
    evidence: list[EvidencePointer] = Field(default_factory=list)


class StageHandoffPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V1
    handoff_id: str
    from_stage: str
    to_stage: str
    objectives: list[str]
    completed_refs: list[str]
    pending_blockers: list[str]
    next_actions: list[str]
    evidence: list[EvidencePointer]


class WorkspaceItem(StrictModel):
    path: str
    item_type: str
    version: str
    provenance: dict[str, str]
    canonical: bool


class WorkspaceIndexPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V1
    items: list[WorkspaceItem]


class WorkspaceProvenanceV21(StrictModel):
    created_by_stage: str
    run_id: str
    hash: str
    role: str | None = None


class WorkspaceItemV21(StrictModel):
    path: str
    item_type: str
    version: str
    provenance: WorkspaceProvenanceV21
    canonical: bool


class WorkspaceIndexPayloadV21(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V21
    items: list[WorkspaceItemV21]


class ContextPackPayload(StrictModel):
    schema_version: str = IMPL_SCHEMA_VERSION_V1
    pack_id: str
    stage: str
    role: str
    artifact_refs: list[str]
    evidence: list[EvidencePointer]
    max_items: int
    max_bytes: int
    size_bytes: int


class ChangeRequestPayload(StrictModel):
    change_id: str
    description: str
    affected_entities: list[str]
    rationale: str
    approvals: list[str]
    status: Literal["proposed", "approved", "rejected"]
    evidence: list[EvidencePointer] = Field(default_factory=list)


class ConflictItem(StrictModel):
    path: str
    reason: str


class ConflictReportPayload(StrictModel):
    conflicts: list[ConflictItem]
    escalation: str


class IntegrationReportPayload(StrictModel):
    merge_order: list[str]
    base_commit: str
    head_commit: str
    skipped_patchsets: list[str] = Field(default_factory=list)


class ImplementationArtifactEnvelope(StrictModel):
    artifact_type: ImplementationArtifactType
    artifact_id: str
    schema_version: str
    created_at: datetime
    source_run_id: UUID
    parents: list[str]
    payload: dict[str, Any]


def deterministic_json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def payload_model_for(artifact_type: ImplementationArtifactType, schema_version: str) -> type[StrictModel]:
    if schema_version.startswith("2.1"):
        mapping: dict[ImplementationArtifactType, type[StrictModel]] = {
            "plan_package_final": StrictModel,
            "planning_handoff_bundle": PlanningHandoffBundlePayload,
            "repo_context": RepoContextPayloadV21,
            "workspace_context": WorkspaceContextPayload,
            "work_plan": WorkPlanPayloadV2,
            "patchset": PatchsetPayload,
            "repo_snapshot": RepoSnapshotPayload,
            "test_results": TestResultsPayload,
            "quality_reports": QualityReportsPayload,
            "trace_report": TraceReportPayload,
            "review_findings": ReviewFindingsPayload,
            "release_bundle": ReleaseBundlePayload,
            "decision_record": DecisionRecordPayload,
            "stage_handoff": StageHandoffPayload,
            "workspace_index": WorkspaceIndexPayloadV21,
            "context_pack": ContextPackPayload,
            "change_request": ChangeRequestPayload,
            "conflict_report": ConflictReportPayload,
            "integration_report": IntegrationReportPayload,
            "execution_profile_catalog": ExecutionProfileCatalogPayload,
            "tool_registry": ToolRegistryPayload,
            "tool_probe_results": ToolProbeResultsPayload,
            "expectation_registry": ExpectationRegistryPayload,
            "evidence_index": EvidenceIndexPayload,
            "diff_report": DiffReportPayload,
            "remote_ops_manifest": RemoteOpsManifestPayload,
            "remote_ops_results": RemoteOpsResultsPayload,
            "remote_op_events": RemoteOpEventsPayload,
            "remote_run_cache_index": RemoteRunCacheIndexPayload,
            "attestation_bundle": AttestationBundlePayload,
            "research_contracts": ResearchContractsPayload,
            "experiment_manifest": ExperimentManifestPayload,
            "experiment_results": ExperimentResultsPayload,
            "replication_report": ReplicationReportPayload,
            "portfolio_decisions": PortfolioDecisionsPayload,
            "program_graph": ProgramGraphPayload,
            "verification_plan": VerificationPlanPayload,
            "work_graph": WorkGraphPayload,
            "work_graph_events": WorkGraphEventsPayload,
            "assumption_registry": AssumptionRegistryPayload,
            "surface_leases": SurfaceLeasesPayload,
            "job_spec": JobSpecPayload,
            "job_result": JobResultPayload,
            "job_events": JobEventsPayload,
            "change_intent": ChangeIntentPayload,
            "apply_record": ApplyRecordPayload,
            "world_state_snapshot": WorldStateSnapshotPayload,
            "drift_report": DriftReportPayload,
            "diagnosis_report": DiagnosisReportPayload,
            "interface_contracts": InterfaceContractsPayload,
        }
    elif schema_version.startswith("2"):
        mapping: dict[ImplementationArtifactType, type[StrictModel]] = {
            "plan_package_final": StrictModel,
            "planning_handoff_bundle": PlanningHandoffBundlePayload,
            "repo_context": RepoContextPayloadV2,
            "workspace_context": WorkspaceContextPayload,
            "work_plan": WorkPlanPayloadV2,
            "patchset": PatchsetPayload,
            "repo_snapshot": RepoSnapshotPayload,
            "test_results": TestResultsPayload,
            "quality_reports": QualityReportsPayload,
            "trace_report": TraceReportPayload,
            "review_findings": ReviewFindingsPayload,
            "release_bundle": ReleaseBundlePayload,
            "decision_record": DecisionRecordPayload,
            "stage_handoff": StageHandoffPayload,
            "workspace_index": WorkspaceIndexPayload,
            "context_pack": ContextPackPayload,
            "change_request": ChangeRequestPayload,
            "conflict_report": ConflictReportPayload,
            "integration_report": IntegrationReportPayload,
            "execution_profile_catalog": ExecutionProfileCatalogPayload,
            "tool_registry": ToolRegistryPayload,
            "tool_probe_results": ToolProbeResultsPayload,
            "expectation_registry": ExpectationRegistryPayload,
            "evidence_index": EvidenceIndexPayload,
            "diff_report": DiffReportPayload,
            "remote_ops_manifest": RemoteOpsManifestPayload,
            "remote_ops_results": RemoteOpsResultsPayload,
            "remote_op_events": RemoteOpEventsPayload,
            "remote_run_cache_index": RemoteRunCacheIndexPayload,
            "attestation_bundle": AttestationBundlePayload,
            "research_contracts": ResearchContractsPayload,
            "experiment_manifest": ExperimentManifestPayload,
            "experiment_results": ExperimentResultsPayload,
            "replication_report": ReplicationReportPayload,
            "portfolio_decisions": PortfolioDecisionsPayload,
        }
    else:
        mapping = {
            "plan_package_final": StrictModel,
            "planning_handoff_bundle": PlanningHandoffBundlePayload,
            "repo_context": RepoContextPayload,
            "workspace_context": WorkspaceContextPayload,
            "work_plan": WorkPlanPayload,
            "patchset": PatchsetPayload,
            "repo_snapshot": RepoSnapshotPayload,
            "test_results": TestResultsPayload,
            "quality_reports": QualityReportsPayload,
            "trace_report": TraceReportPayload,
            "review_findings": ReviewFindingsPayload,
            "release_bundle": ReleaseBundlePayload,
            "decision_record": DecisionRecordPayload,
            "stage_handoff": StageHandoffPayload,
            "workspace_index": WorkspaceIndexPayload,
            "context_pack": ContextPackPayload,
            "change_request": ChangeRequestPayload,
            "conflict_report": ConflictReportPayload,
            "integration_report": IntegrationReportPayload,
        }
    if artifact_type not in mapping:
        raise KeyError(f"Unknown artifact type: {artifact_type}")
    return mapping[artifact_type]


def validate_artifact_payload(envelope: ImplementationArtifactEnvelope) -> None:
    model = payload_model_for(envelope.artifact_type, envelope.schema_version)
    if model is StrictModel and envelope.artifact_type == "plan_package_final":
        if not isinstance(envelope.payload, dict):
            raise ValueError("plan_package_final payload must be an object")
        return
    model.model_validate(envelope.payload)

SCHEMA_FILE_REGISTRY_V1: dict[str, type[StrictModel]] = {
    "repo_context.v1.json": RepoContextPayload,
    "workspace_context.v1.json": WorkspaceContextPayload,
    "work_plan.v1.json": WorkPlanPayload,
    "planning_handoff_bundle.v1.json": PlanningHandoffBundlePayload,
    "patchset.v1.json": PatchsetPayload,
    "repo_snapshot.v1.json": RepoSnapshotPayload,
    "test_results.v1.json": TestResultsPayload,
    "quality_reports.v1.json": QualityReportsPayload,
    "trace_report.v1.json": TraceReportPayload,
    "review_findings.v1.json": ReviewFindingsPayload,
    "release_bundle.v1.json": ReleaseBundlePayload,
    "decision_record.v1.json": DecisionRecordPayload,
    "stage_handoff.v1.json": StageHandoffPayload,
    "workspace_index.v1.json": WorkspaceIndexPayload,
    "context_pack.v1.json": ContextPackPayload,
    "change_request.v1.json": ChangeRequestPayload,
    "conflict_report.v1.json": ConflictReportPayload,
    "integration_report.v1.json": IntegrationReportPayload,
}

SCHEMA_FILE_REGISTRY_V2: dict[str, type[StrictModel]] = {
    "repo_context.v2.json": RepoContextPayloadV2,
    "workspace_context.v2.json": WorkspaceContextPayload,
    "work_plan.v2.json": WorkPlanPayloadV2,
    "planning_handoff_bundle.v2.json": PlanningHandoffBundlePayload,
    "patchset.v2.json": PatchsetPayload,
    "repo_snapshot.v2.json": RepoSnapshotPayload,
    "test_results.v2.json": TestResultsPayload,
    "quality_reports.v2.json": QualityReportsPayload,
    "trace_report.v2.json": TraceReportPayload,
    "review_findings.v2.json": ReviewFindingsPayload,
    "release_bundle.v2.json": ReleaseBundlePayload,
    "decision_record.v2.json": DecisionRecordPayload,
    "stage_handoff.v2.json": StageHandoffPayload,
    "workspace_index.v2.json": WorkspaceIndexPayload,
    "context_pack.v2.json": ContextPackPayload,
    "change_request.v2.json": ChangeRequestPayload,
    "conflict_report.v2.json": ConflictReportPayload,
    "integration_report.v2.json": IntegrationReportPayload,
    "execution_profile_catalog.v2.json": ExecutionProfileCatalogPayload,
    "tool_registry.v2.json": ToolRegistryPayload,
    "tool_probe_results.v2.json": ToolProbeResultsPayload,
    "expectation_registry.v2.json": ExpectationRegistryPayload,
    "evidence_index.v2.json": EvidenceIndexPayload,
    "diff_report.v2.json": DiffReportPayload,
    "remote_ops_manifest.v2.json": RemoteOpsManifestPayload,
    "remote_ops_results.v2.json": RemoteOpsResultsPayload,
    "remote_op_events.v2.json": RemoteOpEventsPayload,
    "remote_run_cache_index.v2.json": RemoteRunCacheIndexPayload,
    "attestation_bundle.v2.json": AttestationBundlePayload,
    "research_contracts.v2.json": ResearchContractsPayload,
    "experiment_manifest.v2.json": ExperimentManifestPayload,
    "experiment_results.v2.json": ExperimentResultsPayload,
    "replication_report.v2.json": ReplicationReportPayload,
    "portfolio_decisions.v2.json": PortfolioDecisionsPayload,
    "repo_context.schema.json": RepoContextPayloadV2,
    "workspace_context.schema.json": WorkspaceContextPayload,
    "work_plan.schema.json": WorkPlanPayloadV2,
    "planning_handoff_bundle.schema.json": PlanningHandoffBundlePayload,
    "patchset.schema.json": PatchsetPayload,
    "repo_snapshot.schema.json": RepoSnapshotPayload,
    "test_results.schema.json": TestResultsPayload,
    "quality_reports.schema.json": QualityReportsPayload,
    "trace_report.schema.json": TraceReportPayload,
    "review_findings.schema.json": ReviewFindingsPayload,
    "release_bundle.schema.json": ReleaseBundlePayload,
    "decision_record.schema.json": DecisionRecordPayload,
    "stage_handoff.schema.json": StageHandoffPayload,
    "workspace_index.schema.json": WorkspaceIndexPayload,
    "context_pack.schema.json": ContextPackPayload,
    "change_request.schema.json": ChangeRequestPayload,
    "conflict_report.schema.json": ConflictReportPayload,
    "integration_report.schema.json": IntegrationReportPayload,
    "execution_profile_catalog.schema.json": ExecutionProfileCatalogPayload,
    "tool_registry.schema.json": ToolRegistryPayload,
    "tool_probe_results.schema.json": ToolProbeResultsPayload,
    "expectation_registry.schema.json": ExpectationRegistryPayload,
    "evidence_index.schema.json": EvidenceIndexPayload,
    "diff_report.schema.json": DiffReportPayload,
    "remote_ops_manifest.schema.json": RemoteOpsManifestPayload,
    "remote_ops_results.schema.json": RemoteOpsResultsPayload,
    "remote_op_events.schema.json": RemoteOpEventsPayload,
    "remote_run_cache_index.schema.json": RemoteRunCacheIndexPayload,
    "attestation_bundle.schema.json": AttestationBundlePayload,
    "research_contracts.schema.json": ResearchContractsPayload,
    "experiment_manifest.schema.json": ExperimentManifestPayload,
    "experiment_results.schema.json": ExperimentResultsPayload,
    "replication_report.schema.json": ReplicationReportPayload,
    "portfolio_decisions.schema.json": PortfolioDecisionsPayload,
}

SCHEMA_FILE_REGISTRY_V21: dict[str, type[StrictModel]] = {
    "repo_context.v2_1.json": RepoContextPayloadV21,
    "workspace_context.v2_1.json": WorkspaceContextPayload,
    "work_plan.v2_1.json": WorkPlanPayloadV2,
    "planning_handoff_bundle.v2_1.json": PlanningHandoffBundlePayload,
    "patchset.v2_1.json": PatchsetPayload,
    "repo_snapshot.v2_1.json": RepoSnapshotPayload,
    "test_results.v2_1.json": TestResultsPayload,
    "quality_reports.v2_1.json": QualityReportsPayload,
    "trace_report.v2_1.json": TraceReportPayload,
    "review_findings.v2_1.json": ReviewFindingsPayload,
    "release_bundle.v2_1.json": ReleaseBundlePayload,
    "decision_record.v2_1.json": DecisionRecordPayload,
    "stage_handoff.v2_1.json": StageHandoffPayload,
    "workspace_index.v2_1.json": WorkspaceIndexPayloadV21,
    "context_pack.v2_1.json": ContextPackPayload,
    "change_request.v2_1.json": ChangeRequestPayload,
    "conflict_report.v2_1.json": ConflictReportPayload,
    "integration_report.v2_1.json": IntegrationReportPayload,
    "execution_profile_catalog.v2_1.json": ExecutionProfileCatalogPayload,
    "tool_registry.v2_1.json": ToolRegistryPayload,
    "tool_probe_results.v2_1.json": ToolProbeResultsPayload,
    "expectation_registry.v2_1.json": ExpectationRegistryPayload,
    "evidence_index.v2_1.json": EvidenceIndexPayload,
    "diff_report.v2_1.json": DiffReportPayload,
    "remote_ops_manifest.v2_1.json": RemoteOpsManifestPayload,
    "remote_ops_results.v2_1.json": RemoteOpsResultsPayload,
    "remote_op_events.v2_1.json": RemoteOpEventsPayload,
    "remote_run_cache_index.v2_1.json": RemoteRunCacheIndexPayload,
    "attestation_bundle.v2_1.json": AttestationBundlePayload,
    "research_contracts.v2_1.json": ResearchContractsPayload,
    "experiment_manifest.v2_1.json": ExperimentManifestPayload,
    "experiment_results.v2_1.json": ExperimentResultsPayload,
    "replication_report.v2_1.json": ReplicationReportPayload,
    "portfolio_decisions.v2_1.json": PortfolioDecisionsPayload,
    "repo_context.schema.json": RepoContextPayloadV21,
    "workspace_context.schema.json": WorkspaceContextPayload,
    "work_plan.schema.json": WorkPlanPayloadV2,
    "planning_handoff_bundle.schema.json": PlanningHandoffBundlePayload,
    "patchset.schema.json": PatchsetPayload,
    "repo_snapshot.schema.json": RepoSnapshotPayload,
    "test_results.schema.json": TestResultsPayload,
    "quality_reports.schema.json": QualityReportsPayload,
    "trace_report.schema.json": TraceReportPayload,
    "review_findings.schema.json": ReviewFindingsPayload,
    "release_bundle.schema.json": ReleaseBundlePayload,
    "decision_record.schema.json": DecisionRecordPayload,
    "stage_handoff.schema.json": StageHandoffPayload,
    "workspace_index.schema.json": WorkspaceIndexPayloadV21,
    "context_pack.schema.json": ContextPackPayload,
    "change_request.schema.json": ChangeRequestPayload,
    "conflict_report.schema.json": ConflictReportPayload,
    "integration_report.schema.json": IntegrationReportPayload,
    "execution_profile_catalog.schema.json": ExecutionProfileCatalogPayload,
    "tool_registry.schema.json": ToolRegistryPayload,
    "tool_probe_results.schema.json": ToolProbeResultsPayload,
    "expectation_registry.schema.json": ExpectationRegistryPayload,
    "evidence_index.schema.json": EvidenceIndexPayload,
    "diff_report.schema.json": DiffReportPayload,
    "remote_ops_manifest.schema.json": RemoteOpsManifestPayload,
    "remote_ops_results.schema.json": RemoteOpsResultsPayload,
    "remote_op_events.schema.json": RemoteOpEventsPayload,
    "remote_run_cache_index.schema.json": RemoteRunCacheIndexPayload,
    "attestation_bundle.schema.json": AttestationBundlePayload,
    "research_contracts.schema.json": ResearchContractsPayload,
    "experiment_manifest.schema.json": ExperimentManifestPayload,
    "experiment_results.schema.json": ExperimentResultsPayload,
    "replication_report.schema.json": ReplicationReportPayload,
    "portfolio_decisions.schema.json": PortfolioDecisionsPayload,
    "program_graph.schema.json": ProgramGraphPayload,
    "verification_plan.schema.json": VerificationPlanPayload,
    "work_graph.schema.json": WorkGraphPayload,
    "work_graph_events.schema.json": WorkGraphEventsPayload,
    "work_graph_events.schema.jsonl": WorkGraphEvent,
    "assumption_registry.schema.json": AssumptionRegistryPayload,
    "surface_leases.schema.json": SurfaceLeasesPayload,
    "surface_lease_event.schema.jsonl": SurfaceLeaseEvent,
    "job_spec.schema.json": JobSpecPayload,
    "job_result.schema.json": JobResultPayload,
    "job_events.schema.json": JobEventsPayload,
    "job_event.schema.jsonl": JobEvent,
    "change_intent.schema.json": ChangeIntentPayload,
    "apply_record.schema.json": ApplyRecordPayload,
    "world_state_snapshot.schema.json": WorldStateSnapshotPayload,
    "drift_report.schema.json": DriftReportPayload,
    "diagnosis_report.schema.json": DiagnosisReportPayload,
    "interface_contracts.schema.json": InterfaceContractsPayload,
}

SCHEMA_FILE_REGISTRY: dict[str, type[StrictModel]] = {
    **SCHEMA_FILE_REGISTRY_V1,
    **SCHEMA_FILE_REGISTRY_V2,
    **SCHEMA_FILE_REGISTRY_V21,
}


def schema_model_for(schema_path: str) -> type[StrictModel]:
    path = Path(schema_path)
    name = path.name
    if "v2_1" in path.parts:
        model = SCHEMA_FILE_REGISTRY_V21.get(name)
    elif "v2" in path.parts:
        model = SCHEMA_FILE_REGISTRY_V2.get(name)
    else:
        model = SCHEMA_FILE_REGISTRY_V1.get(name)
    if model is None:
        model = SCHEMA_FILE_REGISTRY.get(name)
    if model is None:
        raise KeyError(f"Unknown schema file: {schema_path}")
    return model


def export_schema_files(schema_dir: Path) -> None:
    schema_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMA_FILE_REGISTRY_V1.items():
        schema_path = schema_dir / filename
        payload = model.model_json_schema()
        schema_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    v2_dir = schema_dir / "implementation" / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMA_FILE_REGISTRY_V2.items():
        if not filename.endswith(".schema.json"):
            continue
        schema_path = v2_dir / filename
        payload = model.model_json_schema()
        schema_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    v21_dir = schema_dir / "implementation" / "v2_1"
    v21_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMA_FILE_REGISTRY_V21.items():
        if not filename.endswith(".schema.json"):
            continue
        schema_path = v21_dir / filename
        payload = model.model_json_schema()
        schema_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

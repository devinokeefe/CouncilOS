from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from council_os.implementation.schemas import (
    AssumptionRegistryPayload,
    ApplyRecordPayload,
    ContextPackPayload,
    EvidenceIndexPayload,
    EvidenceIndexEntry,
    ExpectationRegistryPayload,
    JobSpecPayload,
    ProgramGraphPayload,
    RepoContextPayloadV21,
    SurfaceLeaseEvent,
    ToolRegistryPayload,
    VerificationPlanPayload,
    WorkLane,
    WorkPlanPayloadV2,
    WorkTaskV2,
    PatchsetLimits,
)
from council_os.implementation.validators import (
    ValidationError,
    validate_expectation_registry,
    validate_tool_registry,
    validate_profile_compliance,
)


def validate_profile_compliance_v21(
    repo_root: Path,
    profile_catalog,
    repo_context: RepoContextPayloadV21,
    job_specs: list[JobSpecPayload],
) -> None:
    tasks: list[WorkTaskV2] = []
    for idx, spec in enumerate(job_specs, start=1):
        tasks.append(
            WorkTaskV2(
                task_id=f"T{idx}",
                title=f"Job {spec.job_id}",
                lane_id="L1",
                depends_on=[],
                maps_to_requirements=[],
                maps_to_acceptance_tests=[],
                profile_id=spec.profile_id,
                satisfies_expectations=spec.expected_expectation_ids,
                verifies_expectations=[],
                remote_ops=[],
            )
        )
    work_plan = WorkPlanPayloadV2(
        lanes=[WorkLane(lane_id="L1", name="Lane", description="")],
        tasks=tasks,
        patchset_limits=PatchsetLimits(max_operations=1, max_bytes=1),
        generated_at=datetime.now(UTC),
    )
    checks = validate_profile_compliance(
        repo_root,
        profile_catalog,
        repo_context,
        work_plan,
        remote_ops_manifest=None,
        require_validator_runners=False,
    )
    failures = [check for check in checks if check.status == "fail"]
    if failures:
        details = ", ".join(check.name for check in failures)
        raise ValidationError(f"Profile compliance failed: {details}")


def validate_tool_registry_v21(
    tool_registry: ToolRegistryPayload,
    job_specs: list[JobSpecPayload],
    artifacts: dict[str, dict[str, object]],
    repo_context: RepoContextPayloadV21 | None,
    profile_catalog,
    run_id: str | None,
    allow_unprobed: bool = False,
) -> None:
    tool_ids = {tool.tool_id for tool in tool_registry.tools}
    for spec in job_specs:
        if spec.tool_id and spec.tool_id not in tool_ids:
            raise ValidationError(f"JobSpec references missing tool: {spec.tool_id}")
    if allow_unprobed:
        for tool in tool_registry.tools:
            if tool.probe_status == "unprobed":
                tool.probe_status = "exempt"
    else:
        for tool in tool_registry.tools:
            if tool.last_probe is None and tool.probe_status == "probed":
                tool.probe_status = "unprobed"
        missing = [tool.tool_id for tool in tool_registry.tools if tool.last_probe is None]
        if missing:
            raise ValidationError(f"Tool probe evidence missing: {missing}")
    validate_tool_registry(tool_registry, artifacts, repo_context=repo_context, profile_catalog=profile_catalog, run_id=run_id)


def validate_expectations_v21(
    registry: ExpectationRegistryPayload,
    artifacts: dict[str, dict[str, object]],
) -> None:
    validate_expectation_registry(registry, artifacts, remote_ops_manifest=None)


def validate_evidence_coverage_v21(
    verification_plan: VerificationPlanPayload,
    evidence_index: EvidenceIndexPayload,
    required_expectation_ids: set[str] | None = None,
) -> None:
    index = {entry.expectation_id: entry for entry in evidence_index.evidence}
    if required_expectation_ids is None:
        required_expectation_ids = set(index.keys())
    failing = [exp_id for exp_id in required_expectation_ids if index.get(exp_id) and index[exp_id].status == "fail"]
    if failing:
        raise ValidationError(f"Evidence coverage failed for {failing}")
    missing = [
        exp_id
        for exp_id in required_expectation_ids
        if index.get(exp_id) is None or index[exp_id].status in {"unknown", "missing"}
    ]
    if missing:
        raise ValidationError(f"Evidence coverage missing: {missing}")


def validate_assumption_gate(registry: AssumptionRegistryPayload) -> None:
    for assumption in registry.assumptions:
        if assumption.impact == "high" and assumption.status in {"unvalidated", "rejected"}:
            raise ValidationError(f"High-impact assumption unresolved: {assumption.assumption_id}")


def validate_context_pack_adequacy(pack: ContextPackPayload, required_refs: list[str]) -> None:
    missing = [ref for ref in required_refs if ref not in pack.artifact_refs]
    if missing:
        raise ValidationError(f"Context pack missing required refs: {missing}")


def validate_verification_plan_coverage_v21(
    program_graph: ProgramGraphPayload,
    verification_plan: VerificationPlanPayload,
    evidence_index: EvidenceIndexPayload,
) -> None:
    check_expectations = {node.id: node.expectation_ids for node in program_graph.nodes if node.type == "check"}
    required: set[str] = set()
    for check in verification_plan.check_requirements:
        if check.program_check_id not in check_expectations:
            raise ValidationError(f"Verification plan references unknown check: {check.program_check_id}")
        required.update(check_expectations.get(check.program_check_id, []))
    validate_evidence_coverage_v21(verification_plan, evidence_index, required_expectation_ids=required)


def validate_lease_compliance_v21(
    required: list[tuple[str, str]],
    lease_events: list[SurfaceLeaseEvent],
) -> None:
    granted = {
        (event.surface.id, event.holder.task_id or "")
        for event in lease_events
        if event.status == "granted"
    }
    missing = [f"{surface}:{task}" for surface, task in required if (surface, task) not in granted]
    if missing:
        raise ValidationError(f"Lease compliance failed: {missing}")


def validate_side_effects_v21(
    job_specs: list[JobSpecPayload],
    change_intent_ids: set[str],
    apply_records: list[ApplyRecordPayload],
) -> None:
    applied = {record.intent_id for record in apply_records}
    for spec in job_specs:
        if spec.side_effects == "none":
            continue
        if not spec.change_intent_id or spec.change_intent_id not in change_intent_ids:
            raise ValidationError(f"Side-effect job missing change intent: {spec.job_id}")
        if spec.change_intent_id not in applied:
            raise ValidationError(f"Side-effect job missing apply record: {spec.job_id}")


def validate_world_state_v21(
    apply_records: list[ApplyRecordPayload],
    artifacts: dict[str, dict[str, object]],
) -> None:
    for record in apply_records:
        for ref in (record.pre_snapshot_ref, record.post_snapshot_ref, record.drift_report_ref):
            if ref and ref not in artifacts:
                raise ValidationError(f"World state artifact missing: {ref}")

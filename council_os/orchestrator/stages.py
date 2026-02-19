from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from council_os.agents.schemas import (
    SCHEMA_VERSION,
    AcceptanceTest,
    ArchitectureChosen,
    ArchitectureOption,
    ArchitectureSection,
    ArtifactEnvelope,
    Assumption,
    BranchBundlePayload,
    CandidateInputs,
    Component,
    Constraint,
    DecisionLogItem,
    EvidencePointer,
    FailureModeClosure,
    FailureModeItem,
    FreezeRecordPayload,
    FreezeTradeoff,
    GovernanceSection,
    HitlPolicy,
    Interface,
    Milestone,
    MilestoneDeliverable,
    Mitigation,
    OpenQuestion,
    PlanCandidatePayload,
    PlanFrozenPayload,
    PlanMeta,
    PlanPackage,
    ProjectCapsulePayload,
    QaStrategyPayload,
    Requirement,
    RequirementsDraftPayload,
    RiskItem,
    ToolPolicy,
    ToolPolicyStage,
    Tradeoffs,
)


def _now() -> datetime:
    return datetime.now(UTC)


def project_capsule(run_id: UUID, brief: str) -> ArtifactEnvelope:
    payload = ProjectCapsulePayload(
        brief=brief,
        problem_statement="Build a planning system that turns vague briefs into frozen plans.",
        goals=["Produce deterministic frozen plans", "Preserve strong auditability"],
        non_goals=["Autonomous code execution in v1"],
        constraints=[Constraint(id="CNS1", type="tech", text="Python 3.12")],
        assumptions=[
            Assumption(
                id="A1",
                text="Users provide trusted prompts",
                impact="medium",
                confidence="high",
                needs_confirmation=False,
            )
        ],
        open_questions=[OpenQuestion(id="Q1", text="Need tools layer in v2?", impact="medium", blocking=False)],
        success_metrics=["At least one candidate frozen", "Checkpoint resume works"],
    )
    return ArtifactEnvelope(
        artifact_type="project_capsule",
        artifact_id="capsule_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[],
        payload=payload.model_dump(by_alias=True),
    )


def requirements_draft(run_id: UUID, parent_id: str) -> ArtifactEnvelope:
    payload = RequirementsDraftPayload(
        requirements=[
            Requirement(
                id="R1",
                priority="MUST",
                text="System must freeze a valid plan.",
                rationale="Core value",
            ),
            Requirement(
                id="R2",
                priority="SHOULD",
                text="System should support resume.",
                rationale="Reliability",
            ),
        ]
    )
    return ArtifactEnvelope(
        artifact_type="requirements_draft",
        artifact_id="requirements_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[parent_id],
        payload=payload.model_dump(by_alias=True),
    )


def acceptance_draft(run_id: UUID, parent_id: str) -> ArtifactEnvelope:
    payload = {
        "acceptance_tests": [
            {
                "id": "AT1",
                "maps_to_requirements": ["R1"],
                "type": "system",
                "procedure": "Run council pipeline to freeze.",
                "pass_criteria": "plan_frozen exists",
            }
        ]
    }
    return ArtifactEnvelope(
        artifact_type="acceptance_draft",
        artifact_id="acceptance_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[parent_id],
        payload=payload,
    )


def architecture_options(run_id: UUID, parent_id: str) -> ArtifactEnvelope:
    option = ArchitectureOption(
        id="O1",
        summary="Deterministic orchestrator with immutable artifacts",
        components=[
            Component(id="C1", name="Orchestrator", responsibilities=["Stage transitions", "Routing"]),
            Component(id="C2", name="ArtifactStore", responsibilities=["Immutable writes"]),
        ],
        interfaces=[Interface(id="IF1", **{"from": "C1"}, to="C2", contract="write_artifact/read_artifact")],
        tradeoffs=Tradeoffs(pros=["Auditability"], cons=["Higher implementation complexity"]),
        risks=["K1"],
    )
    payload = {"options": [option.model_dump(by_alias=True)]}
    return ArtifactEnvelope(
        artifact_type="architecture_options",
        artifact_id="architecture_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[parent_id],
        payload=payload,
    )


def qa_strategy(run_id: UUID, parent_id: str) -> ArtifactEnvelope:
    payload = QaStrategyPayload(
        test_strategy="Unit and deterministic integration tests",
        quality_gates=["ruff", "mypy", "pytest"],
        observability_plan=["Event log per stage"],
        nonfunctional_test_areas=["Determinism", "Immutability"],
    )
    return ArtifactEnvelope(
        artifact_type="qa_strategy",
        artifact_id="qa_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[parent_id],
        payload=payload.model_dump(by_alias=True),
    )


def governance_draft(run_id: UUID, parent_id: str) -> ArtifactEnvelope:
    payload = {
        "risk_register": [
            {
                "id": "K1",
                "severity": "high",
                "description": "Candidate may miss requirement coverage",
                "mitigation": {
                    "text": "Run hard validators",
                    "owner": "orchestrator",
                    "status": "planned",
                },
                "acceptance": None,
            }
        ],
        "tool_policy": {
            "stage_policies": [
                {
                    "stage": "PLANNING",
                    "allowlisted_tools": [],
                    "restrictions": [],
                    "hitl_triggers": [
                        "network_access",
                        "write_outside_workspace",
                        "shell_exec",
                        "secrets_access",
                    ],
                }
            ]
        },
        "hitl_policy": {
            "when_to_interrupt": ["blocking_unknowns", "high_severity_decision", "tool_approval_required"],
            "approval_roles": ["user", "human_reviewer"],
        },
    }
    return ArtifactEnvelope(
        artifact_type="governance_draft",
        artifact_id="governance_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[parent_id],
        payload=payload,
    )


def branch_bundle(run_id: UUID, parent_id: str, branch_number: int, contrarian_rule: bool = False) -> ArtifactEnvelope:
    branch_id = f"B{branch_number}"
    score = max(0.5, 0.9 - (0.05 * (branch_number - 1)))
    style = "thin_slice"
    if contrarian_rule and branch_number % 2 == 0:
        style = "big_bang"
    payload = BranchBundlePayload(
        branch_id=branch_id,
        selected_arch_option="O1",
        milestone_strategy={"style": style, "notes": f"Branch {branch_id} staged rollout"},
        acceptance_strategy={"notes": "Cover MUST requirements first"},
        governance_posture={"tooling_level": "none", "hitl_strictness": "high" if style == "thin_slice" else "medium"},
        risk_posture={"notes": "Prefer explicit closure"},
        pre_synthesis_scores={
            "feasibility": score,
            "complexity_risk": 1 - score,
            "clarity": score,
            "overall": score,
        },
        rationale=f"Deterministic branch generation for {branch_id}.",
    )
    return ArtifactEnvelope(
        artifact_type="branch_bundle",
        artifact_id=f"branch_{branch_id}_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[parent_id],
        payload=payload.model_dump(by_alias=True),
    )


def plan_candidate(
    run_id: UUID,
    capsule: ProjectCapsulePayload,
    branch_ref: str,
    branch_id: str,
    synthesizer_id: str,
    draft_refs: list[str],
    candidate_suffix: str = "",
) -> ArtifactEnvelope:
    candidate_id = f"{branch_id}-{synthesizer_id}{candidate_suffix}"
    plan_meta = PlanMeta(
        plan_id=uuid4(),
        version="v1",
        created_at=_now(),
        source_run_id=run_id,
        schema_version=SCHEMA_VERSION,
        candidate_id=candidate_id,
        branch_id=branch_id,
        plan_status="CANDIDATE",
    )

    option = ArchitectureOption(
        id="O1",
        summary="Deterministic orchestrator with immutable artifacts",
        components=[
            Component(id="C1", name="Orchestrator", responsibilities=["Stage machine"]),
            Component(id="C2", name="ArtifactStore", responsibilities=["Immutable writes"]),
        ],
        interfaces=[Interface(id="IF1", **{"from": "C1"}, to="C2", contract="write/read artifacts")],
        tradeoffs=Tradeoffs(pros=["Determinism"], cons=["Extra boilerplate"]),
        risks=["K1"],
    )

    plan = PlanPackage(
        meta=plan_meta,
        project_capsule=capsule,
        requirements=[
            Requirement(id="R1", priority="MUST", text="Freeze plan", rationale="Primary outcome"),
            Requirement(
                id="R2",
                priority="SHOULD",
                text="Support resume",
                rationale="Operational resilience",
            ),
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT1",
                maps_to_requirements=["R1"],
                type="system",
                procedure="Run council pipeline",
                pass_criteria="plan_frozen exists",
            )
        ],
        architecture=ArchitectureSection(
            options=[option],
            chosen=ArchitectureChosen(
                option_id="O1",
                rationale="Balances determinism and practical implementation",
                high_level_dataflow="brief -> drafts -> candidate -> validate -> freeze",
                key_design_decisions=["DEC1"],
            ),
        ),
        milestones=[
            Milestone(
                id="M1",
                name="Phase 1",
                deliverables=[MilestoneDeliverable(id="DL1", text="End-to-end frozen plan pipeline")],
                exit_criteria=["CLI run command produces frozen plan"],
                depends_on=[],
            )
        ],
        risk_register=[
            RiskItem(
                id="K1",
                severity="high",
                description="Coverage regressions",
                mitigation=Mitigation(text="Hard validator checks", owner="orchestrator", status="planned"),
                acceptance=None,
            )
        ],
        governance=GovernanceSection(
            failure_modes=[
                FailureModeItem(
                    id="FM1",
                    taxonomy="CUSTOM_V1",
                    category="coverage",
                    finding="MUST requirements may be untested",
                    severity="high",
                    closure=FailureModeClosure(
                        status="mitigated",
                        rationale="Coverage validator enforces mapping",
                        signoff="system",
                    ),
                )
            ],
            tool_policy=ToolPolicy(
                stage_policies=[
                    ToolPolicyStage(
                        stage="PLANNING",
                        allowlisted_tools=[],
                        restrictions=[],
                        hitl_triggers=[
                            "network_access",
                            "write_outside_workspace",
                            "shell_exec",
                            "secrets_access",
                        ],
                    )
                ]
            ),
            hitl_policy=HitlPolicy(
                when_to_interrupt=["blocking_unknowns", "high_severity_decision", "tool_approval_required"],
                approval_roles=["user", "human_reviewer"],
            ),
        ),
        decision_log=[
            DecisionLogItem(
                id="DEC1",
                question="Which architecture should v1 use?",
                choice="Deterministic orchestrator + immutable store",
                rationale="Improves auditability and replay",
                alternatives_considered=["Chat-log driven planner"],
                what_would_change_this_decision=["Need lower implementation overhead"],
                evidence_pointers=[
                    EvidencePointer(
                        candidate_id=candidate_id,
                        artifact_ref=branch_ref,
                        artifact_id="DEC1",
                        json_pointer="/plan_package/architecture/chosen",
                        note="Selected architecture rationale",
                    )
                ],
            )
        ],
    )

    payload = PlanCandidatePayload(
        candidate_id=candidate_id,
        branch_id=branch_id,
        synthesizer_id=synthesizer_id,
        inputs=CandidateInputs(capsule_ref="capsule_v1", draft_refs=draft_refs, branch_ref=branch_ref),
        plan_package=plan,
    )
    return ArtifactEnvelope(
        artifact_type="plan_candidate",
        artifact_id=f"candidate_{candidate_id}_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[branch_ref],
        payload=payload.model_dump(by_alias=True),
    )


def freeze_record(
    run_id: UUID,
    winner_candidate_ref: str,
    winner_candidate_id: str,
    validator_summary_ref: str,
    judge_summary_refs: list[str],
    remaining_open_questions: list[str],
    accepted_risks: list[str],
) -> ArtifactEnvelope:
    payload = FreezeRecordPayload(
        winner_candidate_ref=winner_candidate_ref,
        frozen_plan_ref=f"frozen_{winner_candidate_id}_vFinal",
        why_winner=["Passed validators", "Deterministic output"],
        tradeoffs=[FreezeTradeoff(topic="complexity", winner_reason="Higher rigor", runner_up_reason="none")],
        judge_summary_refs=judge_summary_refs,
        validator_summary_ref=validator_summary_ref,
        remaining_open_questions=remaining_open_questions,
        accepted_risks=accepted_risks,
    )
    return ArtifactEnvelope(
        artifact_type="freeze_record",
        artifact_id="freeze_v1",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[winner_candidate_ref],
        payload=payload.model_dump(by_alias=True),
    )


def plan_frozen(
    run_id: UUID,
    candidate_artifact_id: str,
    candidate_payload: PlanCandidatePayload,
    freeze_record_ref: str,
) -> ArtifactEnvelope:
    candidate_payload.plan_package.meta.version = "vFinal"
    candidate_payload.plan_package.meta.plan_status = "FROZEN"
    payload = PlanFrozenPayload(
        candidate_id=candidate_payload.candidate_id,
        branch_id=candidate_payload.branch_id,
        synthesizer_id=candidate_payload.synthesizer_id,
        inputs=candidate_payload.inputs,
        plan_package=candidate_payload.plan_package,
        plan_status="FROZEN",
        freeze_record_ref=freeze_record_ref,
    )
    return ArtifactEnvelope(
        artifact_type="plan_frozen",
        artifact_id=f"frozen_{candidate_payload.candidate_id}_vFinal",
        schema_version=SCHEMA_VERSION,
        created_at=_now(),
        source_run_id=run_id,
        parents=[candidate_artifact_id],
        payload=payload.model_dump(by_alias=True),
    )

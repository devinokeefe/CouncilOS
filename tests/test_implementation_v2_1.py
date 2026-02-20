from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from council_os.agents.schemas import (
    AcceptanceTest,
    ArchitectureChosen,
    ArchitectureOption,
    ArchitectureSection,
    Component,
    Constraint,
    EvidencePointer,
    FailureModeClosure,
    FailureModeItem,
    GovernanceSection,
    HitlPolicy,
    Interface,
    Mitigation,
    OpenQuestion,
    PlanMeta,
    PlanPackage,
    ProjectCapsulePayload,
    Requirement,
    RiskItem,
    ToolPolicy,
    ToolPolicyStage,
    Tradeoffs,
)
from council_os.implementation.engine import ImplementationEngine
from council_os.implementation.schemas import (
    AssumptionRegistryPayload,
    ContextPackLimits,
    DependencyPolicy,
    ExecutionProfileCatalogPayload,
    ExecutionProfilesConfig,
    JobSpecPayload,
    PatchsetLimits,
    RepoContextPayloadV21,
    RepoValidatorEntrypoint,
    RetentionPolicy,
    LeaseHolder,
    SurfaceRef,
    ToolRegistryPayload,
    ValidatorRunner,
    WorkspaceContextPayload,
    WorkGraphPayload,
    WorkGraphTask,
)
from council_os.handoff.hashing import artifact_hash, plan_content_hash
from council_os.handoff.schemas import (
    ApprovalRef,
    ClarificationsRef,
    HandoffArtifactRef,
    HumanFeedbackBundle,
    PlanFreezeRecord,
    PlanningHandoffBundle,
    RepoSnapshot,
)
from council_os.orchestrator.handoff.bundle import compute_handoff_digest
from council_os.implementation.store import ImplementationArtifactStore
from council_os.implementation.v2_1.governance.leases import LeaseManager


def _make_plan() -> PlanPackage:
    return PlanPackage(
        meta=PlanMeta(
            plan_id=uuid4(),
            version="vFinal",
            created_at=datetime.now(UTC),
            source_run_id=uuid4(),
            schema_version="1.0.0",
            candidate_id="B1-SA",
            branch_id="B1",
            plan_status="FROZEN",
        ),
        project_capsule=ProjectCapsulePayload(
            brief="Brief",
            problem_statement="Problem",
            goals=["G1"],
            non_goals=["NG1"],
            constraints=[Constraint(id="C1", type="tech", text="X")],
            assumptions=[],
            open_questions=[OpenQuestion(id="Q1", text="Q", impact="low", blocking=False)],
            success_metrics=["M1"],
        ),
        requirements=[Requirement(id="R1", priority="MUST", text="Do thing", rationale="Rationale")],
        acceptance_tests=[
            AcceptanceTest(
                id="AT1",
                maps_to_requirements=["R1"],
                type="system",
                procedure="Proc",
                pass_criteria="Pass",
            )
        ],
        architecture=ArchitectureSection(
            options=[
                ArchitectureOption(
                    id="O1",
                    summary="Option",
                    components=[Component(id="C1", name="Comp", responsibilities=["X"])],
                    interfaces=[Interface(id="IF1", **{"from": "C1"}, to="C1", contract="none")],
                    tradeoffs=Tradeoffs(pros=["p"], cons=["c"]),
                    risks=["K1"],
                )
            ],
            chosen=ArchitectureChosen(
                option_id="O1",
                rationale="Best",
                high_level_dataflow="flow",
                key_design_decisions=["DEC1"],
            ),
        ),
        milestones=[],
        risk_register=[
            RiskItem(
                id="K1",
                severity="high",
                description="Risk",
                mitigation=Mitigation(text="Mitigate", owner="owner", status="planned"),
                acceptance=None,
            )
        ],
        governance=GovernanceSection(
            failure_modes=[
                FailureModeItem(
                    id="FM1",
                    taxonomy="CUSTOM",
                    category="cat",
                    finding="finding",
                    severity="high",
                    closure=FailureModeClosure(status="mitigated", rationale="ok", signoff="owner"),
                )
            ],
            tool_policy=ToolPolicy(
                stage_policies=[
                    ToolPolicyStage(stage="IMPLEMENTATION", allowlisted_tools=[], restrictions=[], hitl_triggers=[])
                ]
            ),
            hitl_policy=HitlPolicy(when_to_interrupt=[], approval_roles=[]),
        ),
        decision_log=[
            {
                "id": "DEC1",
                "question": "Q",
                "choice": "C",
                "rationale": "R",
                "alternatives_considered": ["A"],
                "what_would_change_this_decision": ["W"],
                "evidence_pointers": [EvidencePointer(artifact_ref="plan_package_final_v1", json_pointer="/")],
            }
        ],
    )


def _write_plan(path: Path) -> PlanPackage:
    plan = _make_plan()
    path.write_text(json.dumps(plan.model_dump(by_alias=True), default=str), encoding="utf-8")
    return plan


def _write_handoff_bundle(
    path: Path,
    *,
    plan_path: Path,
    repo_context_path: Path,
    workspace_context_path: Path,
) -> None:
    planning_root = path.parent
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_hash = plan_content_hash(plan_payload)

    config_snapshot = {"schema_version": "2.1.0", "source": "test"}
    config_snapshot_path = planning_root / "config_snapshot.json"
    config_snapshot_path.write_text(json.dumps(config_snapshot, sort_keys=True), encoding="utf-8")

    human_feedback = HumanFeedbackBundle(
        clarifications=[],
        plan_review_rounds=[],
        notes=["test"],
    )
    human_feedback_path = planning_root / "human_feedback_bundle.json"
    human_feedback_path.write_text(human_feedback.model_dump_json(), encoding="utf-8")

    plan_ref = HandoffArtifactRef(path="planning/plan_package_final.json", sha256=artifact_hash(plan_payload))
    config_ref = HandoffArtifactRef(path="planning/config_snapshot.json", sha256=artifact_hash(config_snapshot))
    human_feedback_ref = HandoffArtifactRef(
        path="planning/human_feedback_bundle.json",
        sha256=artifact_hash(human_feedback.model_dump()),
    )
    freeze_record = PlanFreezeRecord(
        plan_id=str(plan_payload.get("meta", {}).get("plan_id", "")),
        planning_run_id="test-run",
        frozen_at=datetime.now(UTC).isoformat(),
        plan_package_final_ref=plan_ref,
        plan_content_hash=plan_hash,
        interactive_flags={"clarify_intent_enabled": False, "plan_review_enabled": False},
        clarification_resolutions_ref=None,
        plan_approval_ref=None,
        approved_plan_hash=None,
        config_snapshot_ref=config_ref,
        human_feedback_bundle_ref=human_feedback_ref,
        notes="test",
    )
    freeze_path = planning_root / "plan_freeze_record.json"
    freeze_path.write_text(freeze_record.model_dump_json(), encoding="utf-8")
    freeze_ref = HandoffArtifactRef(
        path="planning/plan_freeze_record.json",
        sha256=artifact_hash(freeze_record.model_dump()),
    )

    repo_context_payload = json.loads(repo_context_path.read_text(encoding="utf-8"))
    workspace_context_payload = json.loads(workspace_context_path.read_text(encoding="utf-8"))
    repo_context_ref = HandoffArtifactRef(path="repo_context.json", sha256=artifact_hash(repo_context_payload))
    workspace_context_ref = HandoffArtifactRef(
        path="workspace_context.json", sha256=artifact_hash(workspace_context_payload)
    )

    bundle = PlanningHandoffBundle(
        handoff_bundle_id="HB-test",
        planning_run_id="test-run",
        implementation_run_id=None,
        final_plan={
            "path": plan_ref.path,
            "sha256": plan_ref.sha256,
            "plan_content_hash": plan_hash,
        },
        freeze_record=freeze_ref,
        approval=ApprovalRef(required=False),
        clarifications=ClarificationsRef(required=False),
        human_feedback_bundle=human_feedback_ref,
        config_snapshot=config_ref,
        repo_snapshot=RepoSnapshot(commit_sha="unknown", branch="unknown", dirty=True),
        required_artifacts=[
            plan_ref,
            freeze_ref,
            config_ref,
            repo_context_ref,
            workspace_context_ref,
        ],
        optional_artifacts=[human_feedback_ref],
        handoff_digest="",
    )
    bundle = bundle.model_copy(update={"handoff_digest": compute_handoff_digest(bundle)})
    path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")


def _write_repo_context_v21(path: Path, repo_root: Path, profile_id: str) -> None:
    validator = RepoValidatorEntrypoint(
        id="VAL1",
        name="mock-validator",
        mode="mock",
        outcomes=["pass"],
        maps_to_acceptance_tests=["AT1"],
    )
    payload = RepoContextPayloadV21(
        repo_root=str(repo_root),
        base_commit="base",
        head_commit="head",
        branch_name="main",
        build_entrypoints=[],
        validator_entrypoints=[validator],
        protected_paths=[".git/*"],
        forbidden_paths=["secrets/*"],
        metadata={},
        execution_profiles=ExecutionProfilesConfig(
            catalog_ref="artifact_ref:execution_profile_catalog@v2_1",
            allowed_profile_ids=[profile_id],
            default_profile_id_by_stage={"Validate": profile_id},
        ),
        validator_runners=[ValidatorRunner(name="mock-validator", cmd="pytest -q", profile_id=profile_id)],
        dependency_policy=DependencyPolicy(network_installs_allowed=False, required_lockfiles=[]),
        surfaces=[],
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def _write_workspace_context(path: Path, workspace_root: Path) -> None:
    payload = WorkspaceContextPayload(
        workspace_root=str(workspace_root),
        namespace_rules=[],
        partition_rules={"canonical_prefixes": ["canonical/"], "non_canonical_prefixes": ["non_canonical/"]},
        retention_policy=RetentionPolicy(max_age_days=7, max_size_mb=10, compaction_strategy="none"),
        access_control=[],
        context_pack_limits=ContextPackLimits(max_items=20, max_bytes=4000),
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def _build_execution_profile_catalog(profile_id: str = "legacy_v1", forbidden: list[str] | None = None) -> dict[str, object]:
    forbidden = forbidden or []
    payload = ExecutionProfileCatalogPayload(
        schema_version="2.1.0",
        catalog_id="profiles_default",
        profiles=[
            {
                "profile_id": profile_id,
                "profile_version": "1.0.0",
                "runtime": {"lang": "python", "version": "3.12"},
                "base_image": {"name": "test/image", "digest": "sha256:deadbeef"},
                "deps": {
                    "required": [],
                    "allowed": [],
                    "forbidden": [{"name": name} for name in forbidden],
                },
                "hardware": {"gpu": False},
                "network": {"default": "deny", "allowlist_domains": [], "allowlist_ports": [443]},
                "package_manager": {"pip_install": "deny", "conda_install": "deny"},
                "tool_adapters": [],
                "env": {"vars": {"PYTHONHASHSEED": "0"}},
            }
        ],
    )
    return payload.model_dump()


def _build_tool_registry(tool_id: str = "tool1", probe_status: str = "unprobed") -> dict[str, object]:
    tools = [{"tool_id": tool_id, "tool_version": "1.0.0", "probe_status": probe_status}]
    payload = ToolRegistryPayload(schema_version="2.1.0", registry_id="default", tools=tools)
    return payload.model_dump()


def _write_config(
    path: Path,
    storage_root: Path,
    v2_inputs: dict[str, object],
    tool_registry_policy: dict[str, object] | None = None,
) -> None:
    config: dict[str, object] = {
        "roles": {
            "writer_1": {
                "model_provider": "mock_writer",
                "model_name": "writer-v1",
                "temperature": 0.0,
                "max_tokens": 100,
                "prompt_version_hash": "w1",
                "tools_allowed": [],
            },
            "reviewer_1": {
                "model_provider": "mock_reviewer",
                "model_name": "reviewer-v1",
                "temperature": 0.0,
                "max_tokens": 100,
                "prompt_version_hash": "r1",
                "tools_allowed": [],
            },
            "judge_1": {
                "model_provider": "mock_judge",
                "model_name": "judge-v1",
                "temperature": 0.0,
                "max_tokens": 100,
                "prompt_version_hash": "j1",
                "tools_allowed": [],
            },
        },
        "schemas_version": "2.1.0",
        "features": {"v2_1": True},
        "patchset_limits": {"max_operations": 10, "max_bytes": 1000},
        "retry_policy": {"max_retries": 1},
        "tool_permissions": {
            "intake": ["read"],
            "work_planning": ["read"],
            "generate": ["read"],
            "patch_apply": ["read"],
            "validate": ["read"],
            "review": ["read"],
            "judge": ["read"],
            "freeze": ["read"],
        },
        "tool_calls": [],
        "storage_root": storage_root.as_posix(),
        "v2_inputs": v2_inputs,
    }
    if tool_registry_policy:
        config["tool_registry_policy"] = tool_registry_policy
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _setup_run_inputs(tmp_path: Path, v2_inputs: dict[str, object]) -> dict[str, Path]:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "README.md").write_text("hello", encoding="utf-8")
    planning_root = tmp_path / "planning"
    planning_root.mkdir()
    plan_path = planning_root / "plan_package_final.json"
    handoff_bundle_path = planning_root / "planning_handoff_bundle.json"
    repo_context_path = tmp_path / "repo_context.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_repo_context_v21(repo_context_path, repo_root, profile_id="legacy_v1")
    _write_workspace_context(workspace_context_path, Path("workspace"))
    _write_handoff_bundle(
        handoff_bundle_path,
        plan_path=plan_path,
        repo_context_path=repo_context_path,
        workspace_context_path=workspace_context_path,
    )
    _write_config(config_path, storage_root, v2_inputs=v2_inputs)
    return {
        "storage_root": storage_root,
        "repo_root": repo_root,
        "plan_path": plan_path,
        "handoff_bundle_path": handoff_bundle_path,
        "repo_context_path": repo_context_path,
        "workspace_context_path": workspace_context_path,
        "config_path": config_path,
    }


def _latest_run_root(storage_root: Path) -> Path:
    runs = sorted([p for p in storage_root.iterdir() if p.is_dir()])
    assert runs
    return runs[-1]


def test_vnext_kernel_artifacts_written(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job_vnext_1",
            job_type="validate",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="vnext:job1",
            budget=None,
            expected_expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
        )
    ]
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={"job_specs": [job.model_dump() for job in job_specs]},
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
        allow_repo_override=True,
    )
    run_root = inputs["storage_root"] / str(result.run_id)
    artifacts_root = run_root / "implementation" / "artifacts"
    assert (artifacts_root / "compiler_inputs.json").exists()
    assert (artifacts_root / "work_graph.json").exists()
    assert (artifacts_root / "verification_plan.json").exists()
    assert (artifacts_root / "execution_plan.json").exists()
    assert (artifacts_root / "expectation_registry.json").exists()
    assert (artifacts_root / "evidence_index.json").exists()
    assert (artifacts_root / "decision_record.json").exists()
    jobs_root = run_root / "implementation" / "jobs"
    assert list((jobs_root / "specs").glob("*.json"))
    assert list((jobs_root / "results").glob("*.json"))
    assert list((jobs_root / "run_records").glob("*.jsonl"))
    events_path = run_root / "implementation" / "events.jsonl"
    assert events_path.exists()


def test_at24_profiles_enforced_forbidden_dep(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={"execution_profile_catalog": _build_execution_profile_catalog(forbidden=["tensorflow"])},
    )
    (inputs["repo_root"] / "bad.py").write_text("import tensorflow\n", encoding="utf-8")
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )


def test_at25_expectations_required_for_jobs(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job1",
            job_type="validate_local",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="job1",
            budget=None,
            side_effects="none",
            compensation=None,
            expected_expectation_ids=["EXP-MISSING"],
            surfaces=[],
            assumption_ids=[],
            tool_id=None,
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        )
    ]
    work_graph = WorkGraphPayload(
        schema_version="2.1.0",
        work_graph_id="wg1",
        tasks=[
            WorkGraphTask(
                task_id="task1",
                kind="validate_local",
                deps=[],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-MISSING"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            )
        ],
        generated_at=datetime.now(UTC),
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "work_graph": work_graph.model_dump(),
            "job_specs": [job.model_dump() for job in job_specs],
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(RuntimeError):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )


def test_at26_tool_probe_required(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job1",
            job_type="validate_local",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="job1",
            budget=None,
            side_effects="none",
            compensation=None,
            expected_expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
            surfaces=[],
            assumption_ids=[],
            tool_id="tool1",
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        )
    ]
    work_graph = WorkGraphPayload(
        schema_version="2.1.0",
        work_graph_id="wg1",
        tasks=[
            WorkGraphTask(
                task_id="task1",
                kind="validate_local",
                deps=[],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            )
        ],
        generated_at=datetime.now(UTC),
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "work_graph": work_graph.model_dump(),
            "job_specs": [job.model_dump() for job in job_specs],
        },
    )
    config_data = yaml.safe_load(inputs["config_path"].read_text(encoding="utf-8"))
    config_data["tool_registry_policy"] = {"allow_unprobed": False}
    inputs["config_path"].write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )


def test_at27_job_attestation_emitted(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={"execution_profile_catalog": _build_execution_profile_catalog()},
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
        allow_repo_override=True,
    )
    store = ImplementationArtifactStore(inputs["storage_root"], result.run_id)
    job_results = store.list_artifacts("job_result")
    assert job_results
    job_result_payload = job_results[0].payload
    att_ref = job_result_payload.get("attestation_ref")
    assert att_ref
    attestation = store.read_artifact(att_ref).payload
    assert attestation["code_sha"]["base_commit"] == "base"


def test_at28_freeze_denied_without_coverage(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job1",
            job_type="validate_local",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="job1",
            budget=None,
            side_effects="none",
            compensation=None,
            expected_expectation_ids=[],
            surfaces=[],
            assumption_ids=[],
            tool_id=None,
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        )
    ]
    work_graph = WorkGraphPayload(
        schema_version="2.1.0",
        work_graph_id="wg1",
        tasks=[
            WorkGraphTask(
                task_id="task1",
                kind="validate_local",
                deps=[],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=[],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            )
        ],
        generated_at=datetime.now(UTC),
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "work_graph": work_graph.model_dump(),
            "job_specs": [job.model_dump() for job in job_specs],
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )


def test_at29_idempotency_cache_reuse(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job1",
            job_type="validate_local",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="dup-key",
            budget=None,
            side_effects="none",
            compensation=None,
            expected_expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
            surfaces=[],
            assumption_ids=[],
            tool_id=None,
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        ),
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job2",
            job_type="validate_local",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="dup-key",
            budget=None,
            side_effects="none",
            compensation=None,
            expected_expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
            surfaces=[],
            assumption_ids=[],
            tool_id=None,
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        ),
    ]
    work_graph = WorkGraphPayload(
        schema_version="2.1.0",
        work_graph_id="wg1",
        tasks=[
            WorkGraphTask(
                task_id="task1",
                kind="validate_local",
                deps=[],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=1,
            ),
            WorkGraphTask(
                task_id="task2",
                kind="validate_local",
                deps=["task1"],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-PLAN-R1", "EXP-AT-AT1"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job2"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            ),
        ],
        generated_at=datetime.now(UTC),
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "work_graph": work_graph.model_dump(),
            "job_specs": [job.model_dump() for job in job_specs],
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
        allow_repo_override=True,
    )
    events_path = result.run_root / "workspace" / "canonical" / "job_events.jsonl"
    events = events_path.read_text(encoding="utf-8")
    assert "cache_hit" in events


def test_at30_surface_lease_conflict() -> None:
    manager = LeaseManager()
    event1, _ = manager.request_lease(
        lease_id="lease1",
        surface=SurfaceRef(type="surface", id="surface1"),
        holder=LeaseHolder(run_id="run1", lane_id=None, task_id="task1"),
        ttl_seconds=60,
    )
    event2, _ = manager.request_lease(
        lease_id="lease2",
        surface=SurfaceRef(type="surface", id="surface1"),
        holder=LeaseHolder(run_id="run1", lane_id=None, task_id="task2"),
        ttl_seconds=60,
    )
    assert event1.status == "granted"
    assert event2.status == "denied"


def test_at31_side_effect_job_requires_intent(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job1",
            job_type="remote_op",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="job1",
            budget=None,
            side_effects="irreversible",
            compensation="rollback",
            expected_expectation_ids=["EXP-PLAN-R1"],
            surfaces=[],
            assumption_ids=[],
            tool_id=None,
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        )
    ]
    work_graph = WorkGraphPayload(
        schema_version="2.1.0",
        work_graph_id="wg1",
        tasks=[
            WorkGraphTask(
                task_id="task1",
                kind="remote_op",
                deps=[],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-PLAN-R1"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            )
        ],
        generated_at=datetime.now(UTC),
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "work_graph": work_graph.model_dump(),
            "job_specs": [job.model_dump() for job in job_specs],
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )
    run_root = _latest_run_root(inputs["storage_root"])
    assert (run_root / "artifacts" / "diagnosis_report").exists()


def test_at32_assumption_gate_blocks(tmp_path: Path) -> None:
    assumption_registry = AssumptionRegistryPayload(
        schema_version="2.1.0",
        assumptions=[
            {
                "assumption_id": "A1",
                "statement": "High impact",
                "impact": "high",
                "confidence": "low",
                "validation_task_ids": [],
                "status": "unvalidated",
                "evidence_pointers": [],
            }
        ],
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "assumption_registry": assumption_registry.model_dump(),
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )


def test_at33_anti_stall_emits_diagnosis(tmp_path: Path) -> None:
    job_specs = [
        JobSpecPayload(
            schema_version="2.1.0",
            job_id="job1",
            job_type="validate_local",
            profile_id="legacy_v1",
            inputs=[],
            idempotency_key="job1",
            budget=None,
            side_effects="none",
            compensation=None,
            expected_expectation_ids=["EXP-PLAN-R1"],
            surfaces=[],
            assumption_ids=[],
            tool_id=None,
            change_intent_id=None,
            mock_output={"status": "ok"},
            mock_attempts=[],
            priority=0,
        )
    ]
    work_graph = WorkGraphPayload(
        schema_version="2.1.0",
        work_graph_id="wg1",
        tasks=[
            WorkGraphTask(
                task_id="task1",
                kind="validate_local",
                deps=["task2"],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-PLAN-R1"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            ),
            WorkGraphTask(
                task_id="task2",
                kind="validate_local",
                deps=["task1"],
                profile_id="legacy_v1",
                program_check_ids=[],
                expectation_ids=["EXP-PLAN-R1"],
                assumption_ids=[],
                surfaces=[],
                job_specs=["job1"],
                risk_class="low",
                budget=None,
                rollback_or_comp_plan_ref=None,
                priority=0,
            ),
        ],
        generated_at=datetime.now(UTC),
    )
    inputs = _setup_run_inputs(
        tmp_path,
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "work_graph": work_graph.model_dump(),
            "job_specs": [job.model_dump() for job in job_specs],
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
            allow_repo_override=True,
        )
    run_root = _latest_run_root(inputs["storage_root"])
    assert (run_root / "artifacts" / "diagnosis_report").exists()

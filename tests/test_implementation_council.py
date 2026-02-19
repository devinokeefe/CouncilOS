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
from council_os.implementation.event_log import append_event, new_event
from council_os.implementation.manifest import ManifestInput, create_manifest
from council_os.implementation.patches import (
    apply_patchset,
    PatchApplyError,
    PatchLimitError,
    PatchsetPayload,
    select_smallest_patchset,
)
from council_os.implementation.schemas import (
    ContextPackLimits,
    ContextPackPayload,
    DependencyPolicy,
    ExecutionProfilesConfig,
    ExecutionProfileCatalogPayload,
    EvidenceIndexEntry,
    EvidenceIndexPayload,
    ExpectationRegistryPayload,
    PatchChange,
    PlanningHandoffBundlePayload,
    ReviewFinding,
    EvidencePointer as ImplEvidencePointer,
    RepoContextPayload,
    RepoContextPayloadV2,
    RepoValidatorEntrypoint,
    RetentionPolicy,
    ImplementationArtifactEnvelope,
    RemoteOpBudget,
    RemoteOpSpec,
    RemoteOpsManifestPayload,
    ToolProbeResult,
    ToolProbeResultsPayload,
    ToolRegistryPayload,
    ValidatorRunner,
    StageHandoffPayload,
    WorkspaceContextPayload,
)
from council_os.implementation.content import MarkdownDetectedError
from council_os.implementation.secrets import SecretDetectedError
from council_os.implementation.store import (
    ImplementationArtifactStore,
    ImplementationArtifactStoreError,
)
from council_os.implementation.validators import (
    ValidationError,
    validate_evidence_pointers,
    validate_patchset_semantics,
)
from council_os.implementation.workspace import (
    WorkspaceError,
    WorkspaceIndexError,
    WorkspaceManager,
)
from council_os.orchestrator.checkpoints import list_checkpoints


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
        requirements=[
            Requirement(id="R1", priority="MUST", text="Do thing", rationale="Rationale"),
        ],
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
                "evidence_pointers": [
                    EvidencePointer(artifact_ref="plan_package_final_v1", json_pointer="/")
                ],
            }
        ],
    )


def _write_plan(path: Path) -> PlanPackage:
    plan = _make_plan()
    path.write_text(json.dumps(plan.model_dump(by_alias=True), default=str), encoding="utf-8")
    return plan


def _write_handoff_bundle(path: Path) -> None:
    payload = PlanningHandoffBundlePayload(
        decision_record={"id": "DEC-PLAN-1"},
        validator_reports=[],
        failure_mode_findings=[],
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def _write_repo_context(path: Path, repo_root: Path, outcomes: list[str] | None = None) -> None:
    validator = RepoValidatorEntrypoint(
        id="VAL1",
        name="mock-validator",
        mode="mock",
        outcomes=outcomes or ["pass"],
        maps_to_acceptance_tests=["AT1"],
    )
    payload = RepoContextPayload(
        repo_root=str(repo_root),
        base_commit="base",
        head_commit="head",
        branch_name="main",
        build_entrypoints=[],
        validator_entrypoints=[validator],
        protected_paths=[".git/*"],
        forbidden_paths=["secrets/*"],
        metadata={},
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def _write_repo_context_v2(path: Path, repo_root: Path, outcomes: list[str] | None = None, profile_id: str = "legacy_v1") -> None:
    validator = RepoValidatorEntrypoint(
        id="VAL1",
        name="mock-validator",
        mode="mock",
        outcomes=outcomes or ["pass"],
        maps_to_acceptance_tests=["AT1"],
    )
    payload = RepoContextPayloadV2(
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
            catalog_ref="artifact_ref:execution_profile_catalog@v2",
            allowed_profile_ids=[profile_id],
            default_profile_id_by_stage={"Generate": profile_id, "Validate": profile_id, "RemoteValidate": profile_id},
        ),
        validator_runners=[ValidatorRunner(name="mock-validator", cmd="pytest -q", profile_id=profile_id)],
        dependency_policy=DependencyPolicy(network_installs_allowed=False, required_lockfiles=[]),
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def _build_execution_profile_catalog(profile_id: str = "legacy_v1", forbidden: list[str] | None = None) -> dict[str, object]:
    forbidden = forbidden or []
    payload = ExecutionProfileCatalogPayload(
        schema_version="2.0.0",
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


def _build_tool_registry(tool_id: str = "tool1") -> dict[str, object]:
    payload = ToolRegistryPayload(schema_version="2.0.0", registry_id="default", tools=[{"tool_id": tool_id, "tool_version": "1.0.0"}])
    return payload.model_dump()


def _build_tool_probe_results(tool_id: str = "tool1") -> dict[str, object]:
    payload = ToolProbeResultsPayload(
        schema_version="2.0.0",
        probes=[
            ToolProbeResult(
                probe_run_id="probe1",
                tool_id=tool_id,
                tool_version="1.0.0",
                timestamp=datetime.now(UTC),
                request_params={},
                response_schema_hash="hash",
                latency_ms=1.0,
                status="ok",
                quota_headers={},
                attestation_ref="",
            )
        ],
    )
    return payload.model_dump()


def _build_expectation_registry(expectation_ids: list[str]) -> dict[str, object]:
    expectations = []
    for exp_id in expectation_ids:
        expectations.append(
            {
                "expectation_id": exp_id,
                "level": "plan",
                "source": {"type": "R", "id": exp_id},
                "subject": {"kind": "artifact", "name": exp_id},
                "oracle": {"type": "exists"},
                "tolerance": None,
                "inputs": [],
                "parents": [],
                "children": [],
                "must_exist_before_remote_ops": True,
            }
        )
    payload = ExpectationRegistryPayload(schema_version="2.0.0", registry_id="default", expectations=expectations)
    return payload.model_dump()


def _build_remote_ops_manifest(op_id: str, expectation_id: str, idempotency_key: str) -> dict[str, object]:
    payload = RemoteOpsManifestPayload(
        schema_version="2.0.0",
        ops=[
            RemoteOpSpec(
                op_id=op_id,
                profile_id="legacy_v1",
                tool_id="tool1",
                idempotency_key=idempotency_key,
                budget=RemoteOpBudget(time_sec=10, cost_usd=10, max_calls=1),
                inputs=None,
                expected=[expectation_id],
                canary=None,
                scale=None,
                oracle_override=None,
                mock_output={"metric": 1},
                mock_attempts=["success"],
                mock_cost=1.0,
            )
        ],
    )
    return payload.model_dump()


def _write_workspace_context(path: Path, workspace_root: Path) -> None:
    payload = WorkspaceContextPayload(
        workspace_root=str(workspace_root),
        namespace_rules=[],
        partition_rules={"canonical_prefixes": ["canonical/"], "non_canonical_prefixes": ["non_canonical/"]},
        retention_policy=RetentionPolicy(max_age_days=7, max_size_mb=10, compaction_strategy="none"),
        access_control=[],
        context_pack_limits=ContextPackLimits(max_items=10, max_bytes=2000),
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def _write_config(
    path: Path,
    storage_root: Path,
    tool_calls: list[dict[str, object]] | None = None,
    roles: dict[str, dict[str, object]] | None = None,
    change_requests: list[dict[str, object]] | None = None,
    patchset_limits: dict[str, object] | None = None,
    retry_policy: dict[str, object] | None = None,
    repo_context_v2: bool = False,
    profile_id: str = "legacy_v1",
    schemas_version: str = "1.0.0",
    features: dict[str, object] | None = None,
    v2_inputs: dict[str, object] | None = None,
    tool_registry_policy: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    tool_calls = tool_calls or []
    roles = roles or {
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
    }
    config: dict[str, object] = {
        "roles": roles,
        "schemas_version": schemas_version,
        "patchset_limits": patchset_limits or {"max_operations": 10, "max_bytes": 1000},
        "retry_policy": retry_policy or {"max_retries": 1},
        "tool_permissions": {
            "intake": ["read"],
            "generate": ["read"],
            "validate": ["read"],
            "review": ["read"],
            "judge": ["read"],
            "freeze": ["read"],
            "remote_validate": ["read"],
        },
        "tool_calls": tool_calls,
        "storage_root": storage_root.as_posix(),
    }
    if features:
        config["features"] = features
    if v2_inputs:
        config["v2_inputs"] = v2_inputs
    if tool_registry_policy:
        config["tool_registry_policy"] = tool_registry_policy
    if change_requests:
        config["change_requests"] = change_requests
    if extra:
        config.update(extra)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _setup_run_inputs(
    tmp_path: Path,
    *,
    outcomes: list[str] | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    roles: dict[str, dict[str, object]] | None = None,
    change_requests: list[dict[str, object]] | None = None,
    patchset_limits: dict[str, object] | None = None,
    retry_policy: dict[str, object] | None = None,
    repo_context_v2: bool = False,
    profile_id: str = "legacy_v1",
    schemas_version: str = "1.0.0",
    features: dict[str, object] | None = None,
    v2_inputs: dict[str, object] | None = None,
    tool_registry_policy: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, Path]:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "README.md").write_text("hello", encoding="utf-8")
    plan_path = tmp_path / "plan_package_final.json"
    handoff_bundle_path = tmp_path / "planning_handoff_bundle.json"
    repo_context_path = tmp_path / "repo_context.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_handoff_bundle(handoff_bundle_path)
    if repo_context_v2:
        _write_repo_context_v2(repo_context_path, repo_root, outcomes=outcomes, profile_id=profile_id)
    else:
        _write_repo_context(repo_context_path, repo_root, outcomes=outcomes)
    _write_workspace_context(workspace_context_path, Path("workspace"))
    _write_config(
        config_path,
        storage_root,
        tool_calls=tool_calls,
        roles=roles,
        change_requests=change_requests,
        patchset_limits=patchset_limits,
        retry_policy=retry_policy,
        schemas_version=schemas_version,
        features=features,
        v2_inputs=v2_inputs,
        tool_registry_policy=tool_registry_policy,
        extra=extra,
    )
    return {
        "storage_root": storage_root,
        "repo_root": repo_root,
        "plan_path": plan_path,
        "handoff_bundle_path": handoff_bundle_path,
        "repo_context_path": repo_context_path,
        "workspace_context_path": workspace_context_path,
        "config_path": config_path,
    }


def test_implementation_run_success(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "README.md").write_text("hello", encoding="utf-8")
    plan_path = tmp_path / "plan_package_final.json"
    handoff_bundle_path = tmp_path / "planning_handoff_bundle.json"
    repo_context_path = tmp_path / "repo_context.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_handoff_bundle(handoff_bundle_path)
    _write_repo_context(repo_context_path, repo_root)
    _write_workspace_context(workspace_context_path, Path("workspace"))
    _write_config(config_path, storage_root)

    engine = ImplementationEngine(storage_root=storage_root)
    result = engine.run(plan_path, repo_context_path, workspace_context_path, config_path, handoff_bundle_path)
    run_root = result.run_root
    assert (run_root / "implementation_run_manifest.json").exists()
    assert (run_root / "implementation_event_log.jsonl").exists()
    assert (run_root / "workspace_index.json").exists()
    assert (run_root / "workspace" / "context_packs").exists()
    assert (run_root / "workspace" / "stage_handoffs").exists()
    # Required artifacts present
    assert (run_root / "artifacts" / "work_plan" / "work_plan_v1.json").exists()
    assert (run_root / "artifacts" / "repo_snapshot" / "repo_snapshot_v1.json").exists()
    assert (run_root / "artifacts" / "test_results" / "test_results_v1.json").exists()
    assert (run_root / "artifacts" / "quality_reports" / "quality_reports_v1.json").exists()
    assert (run_root / "artifacts" / "integration_report" / "integration_report_v1.json").exists()
    assert (run_root / "artifacts" / "trace_report" / "trace_report_v1.json").exists()
    assert (run_root / "artifacts" / "review_findings" / "review_findings_v1.json").exists()
    assert (run_root / "artifacts" / "release_bundle" / "release_bundle_v1.json").exists()
    assert (run_root / "artifacts" / "decision_record" / "decision_record_v1.json").exists()


def test_missing_repo_context_rejected(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = tmp_path / "plan_package_final.json"
    handoff_bundle_path = tmp_path / "planning_handoff_bundle.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_handoff_bundle(handoff_bundle_path)
    _write_workspace_context(workspace_context_path, Path("workspace"))
    _write_config(config_path, storage_root)
    engine = ImplementationEngine(storage_root=storage_root)
    with pytest.raises(FileNotFoundError):
        engine.run(
            plan_path,
            tmp_path / "missing_repo_context.json",
            workspace_context_path,
            config_path,
            handoff_bundle_path,
        )


def test_invalid_workspace_context_rejected(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = tmp_path / "plan_package_final.json"
    handoff_bundle_path = tmp_path / "planning_handoff_bundle.json"
    repo_context_path = tmp_path / "repo_context.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_handoff_bundle(handoff_bundle_path)
    _write_repo_context(repo_context_path, repo_root)
    workspace_context_path.write_text("{}", encoding="utf-8")
    _write_config(config_path, storage_root)
    engine = ImplementationEngine(storage_root=storage_root)
    with pytest.raises(Exception):
        engine.run(plan_path, repo_context_path, workspace_context_path, config_path, handoff_bundle_path)


def test_json_only_artifact_enforced(tmp_path: Path) -> None:
    store = ImplementationArtifactStore(tmp_path, uuid4())
    with pytest.raises(Exception):
        store.write_artifact(
            ImplementationArtifactEnvelope(
                artifact_type="work_plan",
                artifact_id="bad",
                schema_version="1.0.0",
                created_at=datetime.now(UTC),
                source_run_id=uuid4(),
                parents=[],
                payload="not json",  # type: ignore[arg-type]
            )
        )


def test_json_patch_updates_artifact(tmp_path: Path) -> None:
    run_id = uuid4()
    store = ImplementationArtifactStore(tmp_path, run_id)
    envelope = ImplementationArtifactEnvelope(
        artifact_type="workspace_index",
        artifact_id="idx_v1",
        schema_version="1.0.0",
        created_at=datetime.now(UTC),
        source_run_id=run_id,
        parents=[],
        payload={"items": []},
    )
    store.write_artifact(envelope)
    patched = store.patch_artifact(
        artifact_id="idx_v1",
        new_artifact_id="idx_v2",
        patch_ops=[{"op": "add", "path": "/items/0", "value": {"path": "x", "item_type": "t", "version": "v1", "provenance": {}, "canonical": True}}],
        run_id=run_id,
    )
    assert patched.exists()


def test_markdown_rejected_in_artifact(tmp_path: Path) -> None:
    store = ImplementationArtifactStore(tmp_path, uuid4())
    with pytest.raises(MarkdownDetectedError):
        store.write_artifact(
            ImplementationArtifactEnvelope(
                artifact_type="work_plan",
                artifact_id="markdown",
                schema_version="1.0.0",
                created_at=datetime.now(UTC),
                source_run_id=uuid4(),
                parents=[],
                payload={"note": "# Heading"},
            )
        )


def test_direct_repair_rejected(tmp_path: Path) -> None:
    run_id = uuid4()
    store = ImplementationArtifactStore(tmp_path, run_id)
    envelope = ImplementationArtifactEnvelope(
        artifact_type="workspace_index",
        artifact_id="idx_v1",
        schema_version="1.0.0",
        created_at=datetime.now(UTC),
        source_run_id=run_id,
        parents=[],
        payload={"items": []},
    )
    store.write_artifact(envelope)
    with pytest.raises(ImplementationArtifactStoreError):
        store.write_artifact(
            ImplementationArtifactEnvelope(
                artifact_type="workspace_index",
                artifact_id="idx_v2",
                schema_version="1.0.0",
                created_at=datetime.now(UTC),
                source_run_id=run_id,
                parents=["idx_v1"],
                payload={"items": []},
            )
        )


def test_smallest_patchset_selection() -> None:
    small = PatchsetPayload(
        patchset_id="p1",
        lane_id=None,
        base_commit="base",
        changes=[PatchChange(path="a.txt", action="add", after="x")],
        apply_order=[0],
        maps_to_requirements=["R1"],
        maps_to_acceptance_tests=[],
        semantic_change=False,
        change_request_ref=None,
        operation_count=1,
        bytes_changed=1,
    )
    big = PatchsetPayload(
        patchset_id="p2",
        lane_id=None,
        base_commit="base",
        changes=[PatchChange(path="b.txt", action="add", after="x" * 10)],
        apply_order=[0],
        maps_to_requirements=["R1"],
        maps_to_acceptance_tests=[],
        semantic_change=False,
        change_request_ref=None,
        operation_count=1,
        bytes_changed=10,
    )
    chosen = select_smallest_patchset([big, small])
    assert chosen.patchset_id == "p1"


def test_change_request_required_for_semantic_change() -> None:
    plan = _make_plan()
    patchset = PatchsetPayload(
        patchset_id="p1",
        lane_id=None,
        base_commit="base",
        changes=[PatchChange(path="x.txt", action="add", after="x")],
        apply_order=[0],
        maps_to_requirements=["R999"],
        maps_to_acceptance_tests=[],
        semantic_change=True,
        change_request_ref="CR1",
        operation_count=1,
        bytes_changed=1,
    )
    with pytest.raises(ValidationError):
        validate_patchset_semantics(patchset, plan, set(), set())
    with pytest.raises(ValidationError):
        validate_patchset_semantics(patchset, plan, {"CR1"}, set())
    validate_patchset_semantics(patchset, plan, {"CR1"}, {"CR1"})


def test_tool_permission_block(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = tmp_path / "plan_package_final.json"
    handoff_bundle_path = tmp_path / "planning_handoff_bundle.json"
    repo_context_path = tmp_path / "repo_context.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_handoff_bundle(handoff_bundle_path)
    _write_repo_context(repo_context_path, repo_root)
    _write_workspace_context(workspace_context_path, Path("workspace"))
    _write_config(
        config_path,
        storage_root,
        tool_calls=[{"stage": "generate", "tool_name": "net", "capabilities": ["network"], "approved": False}],
    )
    engine = ImplementationEngine(storage_root=storage_root)
    with pytest.raises(RuntimeError):
        engine.run(plan_path, repo_context_path, workspace_context_path, config_path, handoff_bundle_path)


def test_secrets_blocked(tmp_path: Path) -> None:
    store = ImplementationArtifactStore(tmp_path, uuid4())
    with pytest.raises(SecretDetectedError):
        store.write_artifact(
            ImplementationArtifactEnvelope(
                artifact_type="work_plan",
                artifact_id="secret",
                schema_version="1.0.0",
                created_at=datetime.now(UTC),
                source_run_id=uuid4(),
                parents=[],
                payload={"token": "sk-SECRETSECRETSECRET"},
            )
        )


def test_retry_policy_records(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = tmp_path / "plan_package_final.json"
    handoff_bundle_path = tmp_path / "planning_handoff_bundle.json"
    repo_context_path = tmp_path / "repo_context.json"
    workspace_context_path = tmp_path / "workspace_context.json"
    config_path = tmp_path / "config.yml"
    _write_plan(plan_path)
    _write_handoff_bundle(handoff_bundle_path)
    _write_repo_context(repo_context_path, repo_root, outcomes=["fail", "pass"])
    _write_workspace_context(workspace_context_path, Path("workspace"))
    _write_config(config_path, storage_root)
    engine = ImplementationEngine(storage_root=storage_root)
    result = engine.run(plan_path, repo_context_path, workspace_context_path, config_path, handoff_bundle_path)
    events = (result.run_root / "implementation_event_log.jsonl").read_text(encoding="utf-8").splitlines()
    validator_events = [json.loads(line) for line in events if "\"validator_result\"" in line]
    assert len(validator_events) >= 2


def test_handoff_bundle_required_and_plan_rejected(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path)
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )

    chat_path = tmp_path / "chat.log"
    chat_path.write_text("chat log", encoding="utf-8")
    with pytest.raises(Exception):
        engine.run(
            chat_path,
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )

    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    with pytest.raises(Exception):
        engine.run(
            bad_path,
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )

    with pytest.raises(FileNotFoundError):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            tmp_path / "missing_handoff.json",
        )


def test_invalid_repo_context_rejected(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path)
    inputs["repo_context_path"].write_text("{}", encoding="utf-8")
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )


def test_missing_workspace_context_rejected(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path)
    inputs["workspace_context_path"].unlink()
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(FileNotFoundError):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )


def test_stage_sequence_and_checkpoints(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path)
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )
    events = [
        json.loads(line)
        for line in (result.run_root / "implementation_event_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    stages = [event["stage"] for event in events if event["type"] == "stage_transition"]
    assert stages == ["intake", "generate", "validate", "review", "judge", "freeze"]
    checkpoints = list_checkpoints(result.run_root)
    stage_names = {cp.stage_name for cp in checkpoints}
    assert {"intake", "generate", "validate", "review", "judge", "freeze"}.issubset(stage_names)


def test_stage_handoffs_and_evidence(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path)
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )
    handoff_paths = list((result.run_root / "workspace" / "stage_handoffs").glob("*.json"))
    assert handoff_paths
    pairs = set()
    store = ImplementationArtifactStore(inputs["storage_root"], result.run_id)
    artifacts = {artifact.artifact_id: artifact.payload for artifact in store.list_artifacts()}
    for path in handoff_paths:
        handoff = StageHandoffPayload.model_validate_json(path.read_text(encoding="utf-8"))
        pairs.add((handoff.from_stage, handoff.to_stage))
        validate_evidence_pointers(handoff.evidence, artifacts)
    assert ("intake", "generate") in pairs
    assert ("generate", "validate") in pairs
    assert ("validate", "review") in pairs
    assert ("review", "judge") in pairs
    assert ("judge", "freeze") in pairs


def test_manifest_and_event_log_immutable(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path)
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )
    with pytest.raises(FileExistsError):
        create_manifest(
            result.run_root,
            ManifestInput(
                run_id=result.run_id,
                code_version="test",
                schema_versions={"implementation": "1.0.0"},
                run_config_version="",
                stage_machine_version="1.0.0",
                validator_suite_version="1.0.0",
                model_portfolio={},
                toolchain_versions={},
                policy_knobs={},
                base_commit="base",
                head_commit="head",
                prompt_pack_hash="",
                config_raw="",
            ),
        )
    event_log_path = result.run_root / "implementation_event_log.jsonl"
    before = event_log_path.read_text(encoding="utf-8").splitlines()
    append_event(
        result.run_root,
        new_event(
            run_id=result.run_id,
            stage="intake",
            actor={"kind": "orchestrator", "role": "test"},
            event_type="decision",
            payload={"note": "append"},
        ),
    )
    after = event_log_path.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before) + 1


def test_evidence_pointer_validation() -> None:
    with pytest.raises(ValueError):
        ImplEvidencePointer(artifact_ref="a", json_pointer="bad")
    pointer = ImplEvidencePointer(artifact_ref="missing", json_pointer="/")
    with pytest.raises(ValidationError):
        validate_evidence_pointers([pointer], {})


def test_non_json_evidence_rejected() -> None:
    with pytest.raises(Exception):
        ReviewFinding(
            id="RF1",
            severity="low",
            summary="bad",
            evidence=[{"path": "file.txt"}],  # type: ignore[list-item]
            violated_gate="none",
        )


def test_patchset_action_rejected() -> None:
    with pytest.raises(Exception):
        PatchChange(path="x.txt", action="write", after="x")  # type: ignore[arg-type]


def test_forbidden_paths_and_op_caps(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    forbidden_patchset = PatchsetPayload(
        patchset_id="p_forbidden",
        lane_id=None,
        base_commit="base",
        changes=[PatchChange(path="secrets/hidden.txt", action="add", after="x")],
        apply_order=[0],
        maps_to_requirements=[],
        maps_to_acceptance_tests=[],
        semantic_change=False,
        change_request_ref=None,
        operation_count=1,
        bytes_changed=1,
    )
    with pytest.raises(PatchApplyError):
        apply_patchset(repo_root, forbidden_patchset, ["secrets/*"], [".git/*"], 10)
    protected_patchset = PatchsetPayload(
        patchset_id="p_protected",
        lane_id=None,
        base_commit="base",
        changes=[PatchChange(path=".git/config", action="add", after="x")],
        apply_order=[0],
        maps_to_requirements=[],
        maps_to_acceptance_tests=[],
        semantic_change=False,
        change_request_ref=None,
        operation_count=1,
        bytes_changed=1,
    )
    with pytest.raises(PatchApplyError):
        apply_patchset(repo_root, protected_patchset, [], [".git/*"], 10)
    apply_patchset(repo_root, protected_patchset, [], [".git/*"], 10, allow_protected=True)
    large_patchset = PatchsetPayload(
        patchset_id="p_big",
        lane_id=None,
        base_commit="base",
        changes=[
            PatchChange(path="a.txt", action="add", after="x"),
            PatchChange(path="b.txt", action="add", after="y"),
        ],
        apply_order=[0, 1],
        maps_to_requirements=[],
        maps_to_acceptance_tests=[],
        semantic_change=False,
        change_request_ref=None,
        operation_count=2,
        bytes_changed=2,
    )
    with pytest.raises(PatchLimitError):
        apply_patchset(repo_root, large_patchset, [], [], 1)


def test_change_request_decision_record_emitted(tmp_path: Path) -> None:
    change_requests = [
        {
            "change_id": "CR1",
            "description": "Need semantic change",
            "affected_entities": ["R1"],
            "rationale": "Test",
            "approvals": ["user"],
            "status": "approved",
        }
    ]
    inputs = _setup_run_inputs(tmp_path, change_requests=change_requests)
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )
    decision_path = result.run_root / "artifacts" / "decision_record" / "decision_record_CR1_v1.json"
    assert decision_path.exists()
    events = (result.run_root / "implementation_event_log.jsonl").read_text(encoding="utf-8")
    assert "\"change_request_id\":\"CR1\"" in events


def test_workspace_partition_rules_and_immutability(tmp_path: Path) -> None:
    context = WorkspaceContextPayload(
        workspace_root=str(tmp_path / "workspace"),
        namespace_rules=[],
        partition_rules={"canonical_prefixes": ["canonical/"], "non_canonical_prefixes": ["non_canonical/"]},
        retention_policy=RetentionPolicy(max_age_days=7, max_size_mb=10, compaction_strategy="none"),
        access_control=[],
        context_pack_limits=ContextPackLimits(max_items=10, max_bytes=2000),
    )
    workspace = WorkspaceManager(tmp_path, context)
    bad_path = workspace.root / "misc" / "note.json"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("{}", encoding="utf-8")
    with pytest.raises(WorkspaceIndexError):
        workspace._record_item(bad_path, "canonical_note", "v1", {}, canonical=True)
    workspace.write_non_canonical_note("note.json", {"a": 1}, {"stage": "test"})
    with pytest.raises(WorkspaceError):
        workspace.write_non_canonical_note("note.json", {"a": 2}, {"stage": "test"})
    workspace.freeze()
    with pytest.raises(WorkspaceError):
        workspace.write_canonical_note("new.json", {"a": 1}, {"stage": "test"})
    with pytest.raises(WorkspaceError):
        workspace.write_non_canonical_note("late.json", {"a": 2}, {"stage": "test"})


def test_workspace_index_and_context_pack_bounds(tmp_path: Path) -> None:
    context = WorkspaceContextPayload(
        workspace_root=str(tmp_path / "workspace"),
        namespace_rules=[],
        partition_rules={"canonical_prefixes": ["canonical/"], "non_canonical_prefixes": ["non_canonical/"]},
        retention_policy=RetentionPolicy(max_age_days=7, max_size_mb=10, compaction_strategy="none"),
        access_control=[],
        context_pack_limits=ContextPackLimits(max_items=1, max_bytes=1),
    )
    workspace = WorkspaceManager(tmp_path, context)
    payload = ContextPackPayload(
        pack_id="context_test",
        stage="test",
        role="tester",
        artifact_refs=["a"],
        evidence=[],
        max_items=1,
        max_bytes=1,
        size_bytes=10,
    )
    with pytest.raises(WorkspaceIndexError):
        workspace.write_context_pack(payload, {"stage": "test"})
    orphan = workspace.context_packs_root / "orphan.json"
    orphan.write_text("{}", encoding="utf-8")
    with pytest.raises(WorkspaceIndexError):
        workspace.assert_index_complete()


def test_freeze_gate_blocks_on_failures(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(tmp_path, outcomes=["fail"], retry_policy={"max_retries": 0})
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )


def test_role_separation_enforced(tmp_path: Path) -> None:
    roles = {
        "writer_1": {
            "model_provider": "same_provider",
            "model_name": "writer-v1",
            "temperature": 0.0,
            "max_tokens": 100,
            "prompt_version_hash": "w1",
            "tools_allowed": [],
        },
        "reviewer_1": {
            "model_provider": "same_provider",
            "model_name": "reviewer-v1",
            "temperature": 0.0,
            "max_tokens": 100,
            "prompt_version_hash": "r1",
            "tools_allowed": [],
        },
        "judge_1": {
            "model_provider": "judge_provider",
            "model_name": "judge-v1",
            "temperature": 0.0,
            "max_tokens": 100,
            "prompt_version_hash": "j1",
            "tools_allowed": [],
        },
    }
    inputs = _setup_run_inputs(tmp_path, roles=roles)
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(ValueError):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )


def test_v2_profile_compliance_forbidden_import(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": False,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(forbidden=["tensorflow"]),
            "tool_registry": _build_tool_registry(),
            "tool_probe_results": _build_tool_probe_results(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
        },
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
        )


def test_v2_expectations_required_for_remote_ops(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": True,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "tool_probe_results": _build_tool_probe_results(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
            "remote_ops_manifest": _build_remote_ops_manifest("ROP1", "EXP-MISSING", "key-1"),
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
        )


def test_v2_tool_registry_probe_required(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": False,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
        },
        tool_registry_policy={"allow_unprobed": False},
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )


def test_v2_attestations_emitted(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": True,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "tool_probe_results": _build_tool_probe_results(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
            "remote_ops_manifest": _build_remote_ops_manifest("ROP1", "EXP-PLAN-R1", "key-1"),
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )
    store = ImplementationArtifactStore(inputs["storage_root"], result.run_id)
    attestations = store.list_artifacts("attestation_bundle")
    assert attestations


def test_v2_expectation_coverage_gate(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": False,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "tool_probe_results": _build_tool_probe_results(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
        },
    )
    plan = _make_plan()
    plan.acceptance_tests[0].maps_to_requirements = []
    inputs["plan_path"].write_text(json.dumps(plan.model_dump(by_alias=True), default=str), encoding="utf-8")
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    with pytest.raises(Exception):
        engine.run(
            inputs["plan_path"],
            inputs["repo_context_path"],
            inputs["workspace_context_path"],
            inputs["config_path"],
            inputs["handoff_bundle_path"],
        )


def test_v2_remote_ops_cache_hit(tmp_path: Path) -> None:
    manifest = RemoteOpsManifestPayload(
        schema_version="2.0.0",
        ops=[
            RemoteOpSpec(
                op_id="ROP1",
                profile_id="legacy_v1",
                tool_id="tool1",
                idempotency_key="dup-key",
                budget=RemoteOpBudget(time_sec=10, cost_usd=10, max_calls=1),
                inputs=None,
                expected=["EXP-PLAN-R1"],
                mock_output={"metric": 1},
                mock_attempts=["success"],
                mock_cost=1.0,
            ),
            RemoteOpSpec(
                op_id="ROP2",
                profile_id="legacy_v1",
                tool_id="tool1",
                idempotency_key="dup-key",
                budget=RemoteOpBudget(time_sec=10, cost_usd=10, max_calls=1),
                inputs=None,
                expected=["EXP-PLAN-R1"],
                mock_output={"metric": 1},
                mock_attempts=["success"],
                mock_cost=1.0,
            ),
        ],
    )
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": True,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "tool_probe_results": _build_tool_probe_results(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
            "remote_ops_manifest": manifest.model_dump(),
        },
    )
    engine = ImplementationEngine(storage_root=inputs["storage_root"])
    result = engine.run(
        inputs["plan_path"],
        inputs["repo_context_path"],
        inputs["workspace_context_path"],
        inputs["config_path"],
        inputs["handoff_bundle_path"],
    )
    events_path = result.run_root / "artifacts" / "remote_op_events" / "remote_op_events_v2.json"
    events_payload = json.loads(events_path.read_text(encoding="utf-8"))
    events = events_payload.get("payload", {}).get("events", [])
    assert any(event.get("event") == "cache_hit" for event in events)


def test_v2_research_replication_gate(tmp_path: Path) -> None:
    inputs = _setup_run_inputs(
        tmp_path,
        repo_context_v2=True,
        schemas_version="2.0.0",
        features={
            "v2_profiles": True,
            "v2_tool_registry": True,
            "v2_expectations": True,
            "v2_remote_ops": False,
            "v2_research": True,
        },
        v2_inputs={
            "execution_profile_catalog": _build_execution_profile_catalog(),
            "tool_registry": _build_tool_registry(),
            "tool_probe_results": _build_tool_probe_results(),
            "expectation_registry": _build_expectation_registry(["EXP-PLAN-R1"]),
            "research_contracts": {"schema_version": "2.0.0", "contracts": []},
            "experiment_results": {
                "schema_version": "2.0.0",
                "experiment_id": "EXP1",
                "results": [],
                "aggregate_stats": {},
                "artifacts": [],
                "attestation_ref": "attestation_missing",
            },
            "replication_report": {"schema_version": "2.0.0", "experiment_id": "EXP1", "comparison": {}, "status": "fail"},
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
        )

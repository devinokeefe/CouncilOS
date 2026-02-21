from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from council_os.handoff.hashing import artifact_hash, default_hash_spec, plan_content_hash
from council_os.handoff.schemas import (
    ApprovalRef,
    ClarificationsRef,
    HandoffArtifactRef,
    HandoffManifest,
    HumanFeedbackBundle,
    ManifestArtifactRef,
    ManifestPointers,
    PlanFreezeRecord,
    PlanningHandoffBundle,
    RepoAcquisitionSpec,
    RepoSnapshot,
)
from council_os.handoff.manifest import compute_manifest_digest
from council_os.implementation.handoff.accept import accept_handoff, accept_handoff_manifest
from council_os.implementation.handoff.errors import HandoffRejected
from council_os.orchestrator.feedback.config import FeedbackConfig
from council_os.orchestrator.feedback.schemas import PlanApproval
from council_os.orchestrator.handoff.bundle import compute_handoff_digest
from council_os.orchestrator.handoff.finalize import finalize_and_write_handoff


def _sample_plan(plan_status: str = "FROZEN") -> dict[str, object]:
    return {
        "meta": {
            "plan_id": "plan-1",
            "version": "vFinal",
            "plan_version": 1,
            "created_at": "2026-02-19T00:00:00Z",
            "source_run_id": "run-1",
            "schema_version": "1.0.0",
            "candidate_id": "B1",
            "branch_id": "B1",
            "plan_status": plan_status,
        },
        "requirements": [
            {"id": "R1", "priority": "MUST", "text": "Do thing", "rationale": "Because"}
        ],
    }


def _init_git_repo(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init"], cwd=repo_root)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=repo_root)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=repo_root)
    (repo_root / "README.md").write_text("hello", encoding="utf-8")
    subprocess.check_call(["git", "add", "README.md"], cwd=repo_root)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo_root)


def _write_handoff_bundle(
    tmp_path: Path,
    *,
    approval_required: bool = False,
    approval_hash: str | None = None,
    include_approval: bool = False,
    repo_root: Path | None = None,
    repo_snapshot: RepoSnapshot | None = None,
) -> Path:
    planning_root = tmp_path / "planning"
    planning_root.mkdir(parents=True, exist_ok=True)
    plan_payload = _sample_plan("FROZEN")
    plan_path = planning_root / "plan_package_final.json"
    plan_path.write_text(json.dumps(plan_payload, sort_keys=True), encoding="utf-8")
    plan_hash = plan_content_hash(plan_payload)

    config_snapshot = {"schema_version": "2.1.0", "source": "test"}
    config_snapshot_path = planning_root / "config_snapshot.json"
    config_snapshot_path.write_text(json.dumps(config_snapshot, sort_keys=True), encoding="utf-8")

    human_feedback = HumanFeedbackBundle(clarifications=[], plan_review_rounds=[], notes=["test"])
    human_feedback_path = planning_root / "human_feedback_bundle.json"
    human_feedback_path.write_text(human_feedback.model_dump_json(), encoding="utf-8")

    artifacts_root = planning_root / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    hash_spec = default_hash_spec().model_dump()
    (planning_root / "hash_spec.json").write_text(json.dumps(hash_spec, sort_keys=True), encoding="utf-8")
    (artifacts_root / "hash_spec.json").write_text(json.dumps(hash_spec, sort_keys=True), encoding="utf-8")
    env_snapshot = {
        "schema_version": "env_snapshot.v1",
        "captured_at": "2026-02-19T00:00:00Z",
        "platform": "test",
        "python_version": "3.12.0",
        "python_executable": "python",
        "runtime": {"implementation": "cpython"},
        "notes": [],
    }
    (planning_root / "env_snapshot.json").write_text(json.dumps(env_snapshot, sort_keys=True), encoding="utf-8")
    (artifacts_root / "env_snapshot.json").write_text(json.dumps(env_snapshot, sort_keys=True), encoding="utf-8")

    plan_ref = HandoffArtifactRef(path="planning/plan_package_final.json", sha256=artifact_hash(plan_payload))
    config_ref = HandoffArtifactRef(path="planning/config_snapshot.json", sha256=artifact_hash(config_snapshot))
    human_feedback_ref = HandoffArtifactRef(
        path="planning/human_feedback_bundle.json",
        sha256=artifact_hash(human_feedback.model_dump()),
    )

    approval_ref = None
    approved_hash = approval_hash or plan_hash
    if approval_required and include_approval:
        approval = PlanApproval(
            approved=True,
            approved_plan_ref="planning/plan_package_final.json",
            approved_plan_hash=approved_hash,
            approved_by="tester",
        )
        approval_path = planning_root / "plan_approval.json"
        approval_path.write_text(json.dumps(approval.model_dump(), sort_keys=True), encoding="utf-8")
        approval_ref = HandoffArtifactRef(
            path="planning/plan_approval.json",
            sha256=artifact_hash(approval.model_dump()),
        )

    repo_snapshot_payload = (repo_snapshot or RepoSnapshot(commit_sha="unknown", branch="unknown", dirty=True)).model_dump()
    (artifacts_root / "repo_snapshot.json").write_text(
        json.dumps(repo_snapshot_payload, sort_keys=True), encoding="utf-8"
    )
    hash_spec_ref = HandoffArtifactRef(
        path="artifacts/hash_spec.json",
        sha256=artifact_hash(hash_spec),
    )
    env_snapshot_ref = HandoffArtifactRef(
        path="artifacts/env_snapshot.json",
        sha256=artifact_hash(env_snapshot),
    )
    repo_snapshot_ref = HandoffArtifactRef(
        path="artifacts/repo_snapshot.json",
        sha256=artifact_hash(repo_snapshot_payload),
    )

    freeze_record = PlanFreezeRecord(
        plan_id=str(plan_payload.get("meta", {}).get("plan_id", "")),
        planning_run_id="run-1",
        frozen_at=datetime.now(UTC).isoformat(),
        plan_package_final_ref=plan_ref,
        plan_content_hash=plan_hash,
        hash_spec_version="1",
        selected_draft_ref="planning/plan_package_draft_v1.json",
        selected_draft_hash=plan_hash,
        interactive_flags={"clarify_intent_enabled": False, "plan_review_enabled": approval_required},
        clarification_resolutions_ref=None,
        plan_approval_ref=approval_ref,
        approved_plan_hash=approved_hash if approval_ref is not None else None,
        repo_snapshot_ref=repo_snapshot_ref,
        env_snapshot_ref=env_snapshot_ref,
        hash_spec_ref=hash_spec_ref,
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

    repo_root = repo_root or (tmp_path / "repo")
    repo_root.mkdir(parents=True, exist_ok=True)
    repo_context_payload = {"repo_root": str(repo_root)}
    repo_context_path = tmp_path / "repo_context.json"
    repo_context_path.write_text(json.dumps(repo_context_payload, sort_keys=True), encoding="utf-8")
    workspace_context_payload = {"workspace_root": "workspace"}
    workspace_context_path = tmp_path / "workspace_context.json"
    workspace_context_path.write_text(json.dumps(workspace_context_payload, sort_keys=True), encoding="utf-8")


    repo_context_ref = HandoffArtifactRef(path="repo_context.json", sha256=artifact_hash(repo_context_payload))
    workspace_context_ref = HandoffArtifactRef(
        path="workspace_context.json", sha256=artifact_hash(workspace_context_payload)
    )

    approval_payload = ApprovalRef(required=approval_required)
    if approval_ref is not None:
        approval_payload = ApprovalRef(
            required=True,
            path=approval_ref.path,
            sha256=approval_ref.sha256,
            approved_plan_hash=approved_hash,
        )

    bundle = PlanningHandoffBundle(
        handoff_bundle_id="HB-test",
        planning_run_id="run-1",
        implementation_run_id=None,
        final_plan={
            "path": plan_ref.path,
            "sha256": plan_ref.sha256,
            "plan_content_hash": plan_hash,
        },
        freeze_record=freeze_ref,
        approval=approval_payload,
        clarifications=ClarificationsRef(required=False),
        human_feedback_bundle=human_feedback_ref,
        config_snapshot=config_ref,
        repo_snapshot=repo_snapshot or RepoSnapshot(commit_sha="unknown", branch="unknown", dirty=True),
        required_artifacts=[
            plan_ref,
            freeze_ref,
            config_ref,
            repo_snapshot_ref,
            env_snapshot_ref,
            hash_spec_ref,
            repo_context_ref,
            workspace_context_ref,
        ],
        optional_artifacts=[human_feedback_ref],
        handoff_digest="",
    )
    bundle = bundle.model_copy(update={"handoff_digest": compute_handoff_digest(bundle)})
    bundle_path = planning_root / "planning_handoff_bundle.json"
    bundle_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    return bundle_path


def test_artifact_hash_deterministic() -> None:
    left = {"b": 2, "a": 1}
    right = {"a": 1, "b": 2}
    assert artifact_hash(left) == artifact_hash(right)


def test_plan_content_hash_ignores_volatile_fields() -> None:
    plan = _sample_plan("CANDIDATE")
    baseline = plan_content_hash(plan)
    mutated = deepcopy(plan)
    meta = mutated.get("meta", {})
    if isinstance(meta, dict):
        meta["created_at"] = "2026-02-19T12:00:00Z"
        meta["source_run_id"] = "run-2"
        meta["plan_status"] = "FROZEN"
    assert plan_content_hash(mutated) == baseline
    semantic_change = deepcopy(plan)
    semantic_change["requirements"] = [
        {"id": "R1", "priority": "MUST", "text": "Changed", "rationale": "Because"}
    ]
    assert plan_content_hash(semantic_change) != baseline


def test_handoff_digest_recompute_changes_on_content() -> None:
    plan_ref = HandoffArtifactRef(path="planning/plan_package_final.json", sha256="sha256:aaa")
    freeze_ref = HandoffArtifactRef(path="planning/plan_freeze_record.json", sha256="sha256:bbb")
    config_ref = HandoffArtifactRef(path="planning/config_snapshot.json", sha256="sha256:ccc")
    repo_context_ref = HandoffArtifactRef(path="repo_context.json", sha256="sha256:ddd")
    workspace_context_ref = HandoffArtifactRef(path="workspace_context.json", sha256="sha256:eee")
    bundle = PlanningHandoffBundle(
        handoff_bundle_id="HB-test",
        planning_run_id="run-1",
        implementation_run_id=None,
        final_plan={
            "path": plan_ref.path,
            "sha256": plan_ref.sha256,
            "plan_content_hash": "sha256:plan",
        },
        freeze_record=freeze_ref,
        approval=ApprovalRef(required=False),
        clarifications=ClarificationsRef(required=False),
        human_feedback_bundle=HandoffArtifactRef(
            path="planning/human_feedback_bundle.json", sha256="sha256:fff"
        ),
        config_snapshot=config_ref,
        repo_snapshot=RepoSnapshot(commit_sha="abc", branch="main", dirty=False),
        required_artifacts=[plan_ref, freeze_ref, config_ref, repo_context_ref, workspace_context_ref],
        optional_artifacts=[],
        handoff_digest="",
    )
    digest = compute_handoff_digest(bundle)
    assert digest.startswith("sha256:")
    bundle_with_digest = bundle.model_copy(update={"handoff_digest": digest})
    assert compute_handoff_digest(bundle_with_digest) == digest
    changed = bundle.model_copy(
        update={"repo_snapshot": RepoSnapshot(commit_sha="abc", branch="main", dirty=True)}
    )
    assert compute_handoff_digest(changed) != digest


def test_manifest_digest_recompute_changes_on_content() -> None:
    manifest = HandoffManifest(
        hash_spec_version="1",
        pointers=ManifestPointers(
            plan_ref="artifacts/plan.json",
            freeze_record_ref="artifacts/freeze.json",
            hash_spec_ref="artifacts/hash_spec.json",
        ),
        repo_acquisition_spec=RepoAcquisitionSpec(
            repo_url="file:///repo",
            git_commit="abc123",
            git_tree_hash="deadbeef",
            submodules="NONE",
        ),
        artifacts=[
            ManifestArtifactRef(
                ref="artifacts/plan.json",
                digest="sha256:aaa",
                schema_version="plan_package.v1",
                role="REQUIRED",
            )
        ],
        env_requirements_ref=None,
        handoff_digest="",
    )
    digest = compute_manifest_digest(manifest)
    assert digest.startswith("sha256:")
    manifest_with_digest = manifest.model_copy(update={"handoff_digest": digest})
    assert compute_manifest_digest(manifest_with_digest) == digest
    changed = manifest.model_copy(
        update={
            "repo_acquisition_spec": RepoAcquisitionSpec(
                repo_url="file:///repo",
                git_commit="def456",
                git_tree_hash="deadbeef",
                submodules="NONE",
            )
        }
    )
    assert compute_manifest_digest(changed) != digest


def test_accept_handoff_rejects_approval_hash_mismatch(tmp_path: Path) -> None:
    bundle_path = _write_handoff_bundle(
        tmp_path,
        approval_required=True,
        approval_hash="sha256:bad",
        include_approval=True,
    )
    run_root = tmp_path / "impl"
    run_root.mkdir()
    with pytest.raises(HandoffRejected):
        accept_handoff(
            bundle_path=bundle_path,
            run_root=run_root,
            implementation_run_id="impl-1",
            implementation_engine_version="test",
            allow_repo_override=True,
        )
    assert (run_root / "handoff_rejection.json").exists()


def test_accept_handoff_rejects_required_artifact_hash_mismatch(tmp_path: Path) -> None:
    bundle_path = _write_handoff_bundle(tmp_path)
    plan_path = bundle_path.parent / "plan_package_final.json"
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["requirements"] = [
        {"id": "R1", "priority": "MUST", "text": "Tampered", "rationale": "Because"}
    ]
    plan_path.write_text(json.dumps(plan_payload, sort_keys=True), encoding="utf-8")

    run_root = tmp_path / "impl"
    run_root.mkdir()
    with pytest.raises(HandoffRejected):
        accept_handoff(
            bundle_path=bundle_path,
            run_root=run_root,
            implementation_run_id="impl-1",
            implementation_engine_version="test",
            allow_repo_override=True,
        )


def test_accept_handoff_rejects_repo_snapshot_mismatch(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    bundle_path = _write_handoff_bundle(
        tmp_path,
        repo_root=repo_root,
        repo_snapshot=RepoSnapshot(commit_sha="deadbeef", branch="main", dirty=False),
    )
    run_root = tmp_path / "impl"
    run_root.mkdir()
    with pytest.raises(HandoffRejected):
        accept_handoff(
            bundle_path=bundle_path,
            run_root=run_root,
            implementation_run_id="impl-1",
            implementation_engine_version="test",
        )


def test_accept_handoff_ack_idempotency(tmp_path: Path) -> None:
    bundle_path = _write_handoff_bundle(tmp_path)
    run_root = tmp_path / "impl"
    run_root.mkdir()
    impl_run_id = str(uuid4())
    accepted = accept_handoff(
        bundle_path=bundle_path,
        run_root=run_root,
        implementation_run_id=impl_run_id,
        implementation_engine_version="test",
        allow_repo_override=True,
    )
    assert (run_root / "handoff_ack.json").exists()
    accepted_again = accept_handoff(
        bundle_path=bundle_path,
        run_root=run_root,
        implementation_run_id=impl_run_id,
        implementation_engine_version="test",
        allow_repo_override=True,
    )
    assert accepted_again.handoff_digest == accepted.handoff_digest

    bundle_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle_payload["handoff_digest"] = "sha256:deadbeef"
    bundle_path.write_text(json.dumps(bundle_payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(HandoffRejected):
        accept_handoff(
            bundle_path=bundle_path,
            run_root=run_root,
            implementation_run_id=impl_run_id,
            implementation_engine_version="test",
            allow_repo_override=True,
        )


def test_finalize_requires_approval_when_review_enabled(tmp_path: Path) -> None:
    run_root = tmp_path / "planning_run"
    run_root.mkdir()
    plan = _sample_plan("CANDIDATE")
    feedback_cfg = FeedbackConfig(plan_review=True, clarify=False)
    with pytest.raises(RuntimeError):
        finalize_and_write_handoff(
            run_id="run-1",
            run_root=run_root,
            plan=plan,
            feedback_cfg=feedback_cfg,
            config_snapshot={"schema_version": "2.1.0"},
            repo_context_path=None,
            workspace_context_path=None,
        )


def test_finalize_and_accept_handoff_roundtrip(tmp_path: Path) -> None:
    run_root = tmp_path / "planning_run"
    run_root.mkdir()
    plan = _sample_plan("CANDIDATE")
    plan_hash = plan_content_hash(plan)

    planning_root = run_root / "planning"
    planning_root.mkdir(parents=True, exist_ok=True)
    approval = PlanApproval(
        approved=True,
        approved_plan_ref="planning/plan_package_draft_v1.json",
        approved_plan_hash=plan_hash,
        approved_by="tester",
    )
    (planning_root / "plan_approval.json").write_text(
        json.dumps(approval.model_dump(), sort_keys=True), encoding="utf-8"
    )

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    repo_context_path = tmp_path / "repo_context.json"
    repo_context_path.write_text(json.dumps({"repo_root": str(repo_root)}, sort_keys=True), encoding="utf-8")
    workspace_context_path = tmp_path / "workspace_context.json"
    workspace_context_path.write_text(json.dumps({"workspace_root": "workspace"}, sort_keys=True), encoding="utf-8")

    bundle = finalize_and_write_handoff(
        run_id="run-1",
        run_root=run_root,
        plan=plan,
        feedback_cfg=FeedbackConfig(plan_review=True, clarify=False),
        config_snapshot={"schema_version": "2.1.0", "source": "test"},
        repo_context_path=repo_context_path,
        workspace_context_path=workspace_context_path,
    )
    manifest_path = run_root / "planning" / "handoff_manifest.json"
    assert manifest_path.exists()

    impl_root = tmp_path / "impl"
    impl_root.mkdir()
    impl_run_id = str(uuid4())
    accepted = accept_handoff_manifest(
        manifest_path=manifest_path,
        run_root=impl_root,
        implementation_run_id=impl_run_id,
        implementation_engine_version="test",
    )
    assert (impl_root / "handoff_ack.json").exists()
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert accepted.handoff_digest == manifest_payload["handoff_digest"]


def test_finalize_uses_selected_draft_over_latest(tmp_path: Path) -> None:
    run_root = tmp_path / "planning_run"
    run_root.mkdir()
    planning_root = run_root / "planning"
    planning_root.mkdir(parents=True, exist_ok=True)

    draft_v1 = _sample_plan("CANDIDATE")
    draft_v1["requirements"] = [
        {"id": "R1", "priority": "MUST", "text": "Draft V1", "rationale": "Because"}
    ]
    draft_v2 = _sample_plan("CANDIDATE")
    draft_v2["requirements"] = [
        {"id": "R1", "priority": "MUST", "text": "Draft V2", "rationale": "Because"}
    ]

    (planning_root / "plan_package_draft_v1.json").write_text(
        json.dumps(draft_v1, sort_keys=True), encoding="utf-8"
    )
    (planning_root / "plan_package_draft_v2.json").write_text(
        json.dumps(draft_v2, sort_keys=True), encoding="utf-8"
    )

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    repo_context_path = tmp_path / "repo_context.json"
    repo_context_path.write_text(json.dumps({"repo_root": str(repo_root)}, sort_keys=True), encoding="utf-8")
    workspace_context_path = tmp_path / "workspace_context.json"
    workspace_context_path.write_text(json.dumps({"workspace_root": "workspace"}, sort_keys=True), encoding="utf-8")

    finalize_and_write_handoff(
        run_id="run-1",
        run_root=run_root,
        plan=draft_v2,
        feedback_cfg=FeedbackConfig(plan_review=False, clarify=False),
        config_snapshot={"schema_version": "2.1.0", "source": "test"},
        repo_context_path=repo_context_path,
        workspace_context_path=workspace_context_path,
        selected_draft_name="plan_package_draft_v1.json",
    )

    final_path = planning_root / "plan_package_final.json"
    final_payload = json.loads(final_path.read_text(encoding="utf-8"))
    assert final_payload["requirements"][0]["text"] == "Draft V1"

    freeze_path = planning_root / "plan_freeze_record.json"
    freeze_payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    assert freeze_payload["selected_draft_ref"].endswith("plan_package_draft_v1.json")


def test_finalize_uses_change_request_lineage(tmp_path: Path) -> None:
    run_root = tmp_path / "planning_run"
    run_root.mkdir()
    planning_root = run_root / "planning"
    planning_root.mkdir(parents=True, exist_ok=True)

    change_request_payload = {
        "schema_version": "change_request.v1",
        "meta": {"impl_run_id": "impl-1", "source_plan_hash": "sha256:parent"},
        "reason": {"code": "SCOPE_CHANGE", "summary": "Scope expanded"},
        "blocked_entities": {"task_ids": [], "job_ids": [], "check_ids": []},
        "evidence_refs": [{"type": "patchset", "ref": "patchset-1"}],
        "proposed_plan_edits_ref": "artifacts/plan_edits_v1.json",
        "impact": {"milestones_affected": ["MS-001"], "checks_affected": []},
    }
    impl_artifacts = run_root / "implementation" / "artifacts"
    impl_artifacts.mkdir(parents=True, exist_ok=True)
    (impl_artifacts / "change_request_v1.json").write_text(
        json.dumps(change_request_payload, sort_keys=True), encoding="utf-8"
    )

    plan = _sample_plan("CANDIDATE")
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    repo_context_path = tmp_path / "repo_context.json"
    repo_context_path.write_text(json.dumps({"repo_root": str(repo_root)}, sort_keys=True), encoding="utf-8")
    workspace_context_path = tmp_path / "workspace_context.json"
    workspace_context_path.write_text(json.dumps({"workspace_root": "workspace"}, sort_keys=True), encoding="utf-8")

    finalize_and_write_handoff(
        run_id="run-1",
        run_root=run_root,
        plan=plan,
        feedback_cfg=FeedbackConfig(plan_review=False, clarify=False),
        config_snapshot={"schema_version": "2.1.0", "source": "test"},
        repo_context_path=repo_context_path,
        workspace_context_path=workspace_context_path,
    )

    final_payload = json.loads((planning_root / "plan_package_final.json").read_text(encoding="utf-8"))
    assert final_payload["meta"]["parent_plan_hash"] == "sha256:parent"
    assert final_payload["meta"]["amendment_id"]

    migration_payload = json.loads((planning_root / "plan_migration_record.json").read_text(encoding="utf-8"))
    assert migration_payload["parent_plan_hash"] == "sha256:parent"
    assert migration_payload["notes"]

    manifest_payload = json.loads((planning_root / "handoff_manifest.json").read_text(encoding="utf-8"))
    assert any(
        artifact.get("schema_version") == "change_request.v1" for artifact in manifest_payload.get("artifacts", [])
    )

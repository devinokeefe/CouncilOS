from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from council_os.handoff.hashing import artifact_hash, default_hash_spec, plan_content_hash
from council_os.handoff.manifest import compute_manifest_digest
from council_os.handoff.schemas import (
    HandoffAck,
    HandoffManifest,
    HandoffOverride,
    HandoffRejection,
    PlanFreezeRecord,
    PlanningHandoffBundle,
    ImportedArtifactRef,
    ManifestArtifactRef,
    HashSpec,
)
from council_os.implementation.event_log import append_event, new_event
from council_os.implementation.handoff.errors import HandoffRejected
from council_os.implementation.handoff.importer import copy_with_provenance
from council_os.orchestrator.feedback.schemas import ClarificationResolutions, PlanApproval
from council_os.orchestrator.feedback.utils import stable_json_dumps
from council_os.orchestrator.handoff.repo_snapshot import capture_repo_snapshot
from council_os.vnext.events import append_vnext_event


@dataclass
class AcceptedHandoff:
    bundle_path: Path
    plan_path: Path
    repo_context_path: Path
    workspace_context_path: Path
    config_snapshot_path: Path
    plan_content_hash: str
    handoff_digest: str


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _bundle_base_root(bundle_path: Path) -> Path:
    if bundle_path.parent.name == "planning":
        return bundle_path.parent.parent
    return bundle_path.parent


def _resolve_path(bundle_path: Path, rel_path: str) -> Path:
    base_root = _bundle_base_root(bundle_path)
    return (base_root / rel_path).resolve()


def _artifact_ref(name: str) -> str:
    return f"artifacts/{name}"


def _manifest_base_root(manifest_path: Path) -> Path:
    if manifest_path.parent.name == "artifacts":
        return manifest_path.parent.parent
    return manifest_path.parent


def _resolve_manifest_ref(manifest_path: Path, ref: str) -> Path:
    base_root = _manifest_base_root(manifest_path)
    if ref.startswith("artifacts/"):
        return (base_root / ref).resolve()
    if ref.startswith("planning/"):
        return (base_root.parent / ref).resolve()
    return (manifest_path.parent / ref).resolve()


def _load_hash_spec(path: Path | None) -> HashSpec:
    if path is None or not path.exists():
        return default_hash_spec()
    return HashSpec.model_validate_json(path.read_text(encoding="utf-8"))


def _find_manifest_artifact(
    manifest: HandoffManifest, *, schema_version: str
) -> ManifestArtifactRef | None:
    for artifact in manifest.artifacts:
        if artifact.schema_version == schema_version:
            return artifact
    return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(stable_json_dumps(payload), encoding="utf-8")


def _write_rejection(
    run_root: Path,
    reason: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    reason_code: str | None = None,
    remediation_hints: list[str] | None = None,
    run_id: str | None = None,
    plan_hash: str | None = None,
) -> Path:
    rejection = HandoffRejection(
        rejected_at=datetime.now(UTC).isoformat(),
        reason=reason,
        reason_code=reason_code,
        expected=expected,
        actual=actual,
        remediation_hints=remediation_hints or [],
    )
    path = run_root / "handoff_rejection.json"
    _write_json(path, rejection.model_dump())
    if run_id:
        append_vnext_event(
            run_root,
            "implementation",
            "HANDOFF_REJECTED",
            run_id=run_id,
            plan_hash=plan_hash,
            payload={"reason": reason, "reason_code": reason_code},
        )
    return path


def _run_git(args: list[str], cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()


def _ensure_repo_acquired(repo_root: Path, repo_url: str | None) -> None:
    if repo_root.exists():
        return
    if not repo_url:
        raise RuntimeError("repo_root missing and repo_url not provided")
    repo_root.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "clone", repo_url, str(repo_root)], stderr=subprocess.DEVNULL)


def _resolve_repo_checkout(primary_root: Path, spec: "RepoAcquisitionSpec", index: int) -> Path:
    if index == 0:
        return primary_root
    if spec.checkout_path:
        candidate = Path(spec.checkout_path)
        if candidate.is_absolute():
            return candidate
        return (primary_root / candidate).resolve()
    if spec.repo_id:
        return (primary_root / "repos" / spec.repo_id).resolve()
    return (primary_root / "repos" / f"repo_{index}").resolve()


def _git_has_commit(repo_root: Path, commit: str) -> bool:
    try:
        _run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_root)
    except Exception:
        return False
    return True


def _checkout_commit(repo_root: Path, commit: str) -> bool:
    try:
        _run_git(["checkout", commit], cwd=repo_root)
    except Exception:
        return False
    return True


def _write_override(
    run_root: Path,
    override_type: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    rationale: str | None = None,
    new_baseline_snapshot_ref: str | None = None,
) -> Path:
    override = HandoffOverride(
        override_type=override_type,
        expected=expected,
        actual=actual,
        rationale=rationale,
        new_baseline_snapshot_ref=new_baseline_snapshot_ref,
    )
    path = run_root / "handoff_override.json"
    _write_json(path, override.model_dump())
    return path


def _load_override(run_root: Path, override_path: Path | None = None) -> tuple[HandoffOverride | None, Path | None]:
    candidate = override_path or (run_root / "handoff_override.json")
    if candidate.exists():
        return HandoffOverride.model_validate_json(candidate.read_text(encoding="utf-8")), candidate
    return None, None


def _compute_handoff_digest(bundle: PlanningHandoffBundle) -> str:
    payload = bundle.model_dump()
    payload["handoff_digest"] = ""
    return artifact_hash(payload)


def accept_handoff(
    *,
    bundle_path: Path,
    run_root: Path,
    implementation_run_id: str,
    implementation_engine_version: str,
    allow_repo_override: bool = False,
) -> AcceptedHandoff:
    bundle = PlanningHandoffBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))

    ack_path = run_root / "handoff_ack.json"
    if ack_path.exists():
        ack = HandoffAck.model_validate_json(ack_path.read_text(encoding="utf-8"))
        if ack.accepted_handoff_digest != bundle.handoff_digest:
            raise HandoffRejected("Existing handoff ack digest mismatch", ack_path)
        return AcceptedHandoff(
            bundle_path=bundle_path,
            plan_path=_resolve_path(bundle_path, bundle.final_plan.path),
            repo_context_path=_resolve_path(bundle_path, "repo_context.json"),
            workspace_context_path=_resolve_path(bundle_path, "workspace_context.json"),
            config_snapshot_path=_resolve_path(bundle_path, bundle.config_snapshot.path),
            plan_content_hash=ack.accepted_plan_content_hash,
            handoff_digest=ack.accepted_handoff_digest,
        )

    computed_digest = _compute_handoff_digest(bundle)
    if computed_digest != bundle.handoff_digest:
        rejection_path = _write_rejection(
            run_root,
            "handoff_digest_mismatch",
            {"expected": bundle.handoff_digest},
            {"actual": computed_digest},
            reason_code="HASH_MISMATCH",
            run_id=implementation_run_id,
        )
        raise HandoffRejected("Handoff digest mismatch", rejection_path)

    base_root = _bundle_base_root(bundle_path)
    hash_spec_path = None
    for candidate in (
        base_root / "planning" / "hash_spec.json",
        base_root / "planning" / "artifacts" / "hash_spec.json",
        base_root / "hash_spec.json",
    ):
        if candidate.exists():
            hash_spec_path = candidate
            break
    hash_spec = _load_hash_spec(hash_spec_path)

    for item in bundle.required_artifacts:
        artifact_path = _resolve_path(bundle_path, item.path)
        if not artifact_path.exists():
            rejection_path = _write_rejection(
                run_root,
                "required_artifact_missing",
                {"path": item.path},
                {"actual": "missing"},
                reason_code="MISSING_ARTIFACT",
                run_id=implementation_run_id,
            )
            raise HandoffRejected(f"Missing required artifact: {item.path}", rejection_path)
        actual_hash = artifact_hash(_load_json(artifact_path))
        if actual_hash != item.sha256:
            rejection_path = _write_rejection(
                run_root,
                "required_artifact_hash_mismatch",
                {"path": item.path, "expected": item.sha256},
                {"actual": actual_hash},
                reason_code="HASH_MISMATCH",
                run_id=implementation_run_id,
            )
            raise HandoffRejected(f"Hash mismatch for required artifact: {item.path}", rejection_path)

    plan_path = _resolve_path(bundle_path, bundle.final_plan.path)
    plan_payload = _load_json(plan_path)
    plan_meta = plan_payload.get("meta", {}) if isinstance(plan_payload, dict) else {}
    if plan_meta.get("plan_status") != "FROZEN":
        rejection_path = _write_rejection(
            run_root,
            "plan_not_frozen",
            {"expected": "FROZEN"},
            {"actual": plan_meta.get("plan_status")},
            reason_code="SCHEMA_UNSUPPORTED",
            run_id=implementation_run_id,
        )
        raise HandoffRejected("plan_package_final must be frozen", rejection_path)

    plan_hash = plan_content_hash(plan_payload, hash_spec=hash_spec)
    if plan_hash != bundle.final_plan.plan_content_hash:
        rejection_path = _write_rejection(
            run_root,
            "plan_hash_mismatch",
            {"expected": bundle.final_plan.plan_content_hash},
            {"actual": plan_hash},
            reason_code="HASH_MISMATCH",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Plan content hash mismatch", rejection_path)

    freeze_path = _resolve_path(bundle_path, bundle.freeze_record.path)
    freeze_record = PlanFreezeRecord.model_validate_json(freeze_path.read_text(encoding="utf-8"))
    if freeze_record.plan_content_hash != plan_hash:
        rejection_path = _write_rejection(
            run_root,
            "freeze_record_hash_mismatch",
            {"expected": freeze_record.plan_content_hash},
            {"actual": plan_hash},
            reason_code="HASH_MISMATCH",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Plan freeze record hash mismatch", rejection_path)

    if bundle.approval.required:
        if not bundle.approval.path:
            rejection_path = _write_rejection(
                run_root,
                "approval_missing",
                {"expected": "plan_approval.json"},
                {"actual": "missing"},
                reason_code="MISSING_ARTIFACT",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Plan approval required but missing", rejection_path)
        approval_path = _resolve_path(bundle_path, bundle.approval.path)
        approval = PlanApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
        if not approval.approved:
            rejection_path = _write_rejection(
                run_root,
                "approval_not_granted",
                {"expected": True},
                {"actual": approval.approved},
                reason_code="HASH_MISMATCH",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Plan approval not granted", rejection_path)
        if approval.approved_plan_hash != plan_hash:
            rejection_path = _write_rejection(
                run_root,
                "approval_hash_mismatch",
                {"expected": plan_hash},
                {"actual": approval.approved_plan_hash},
                reason_code="HASH_MISMATCH",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Plan approval hash mismatch", rejection_path)

    if bundle.clarifications.required:
        if not bundle.clarifications.path:
            rejection_path = _write_rejection(
                run_root,
                "clarifications_missing",
                {"expected": "clarification_resolutions.json"},
                {"actual": "missing"},
                reason_code="MISSING_ARTIFACT",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Clarification resolutions required but missing", rejection_path)
        clar_path = _resolve_path(bundle_path, bundle.clarifications.path)
        ClarificationResolutions.model_validate_json(clar_path.read_text(encoding="utf-8"))

    repo_context_path = _resolve_path(bundle_path, "repo_context.json")
    workspace_context_path = _resolve_path(bundle_path, "workspace_context.json")
    repo_context = _load_json(repo_context_path)
    repo_root = Path(str(repo_context.get("repo_root", ""))).expanduser()
    if not repo_root.exists():
        rejection_path = _write_rejection(
            run_root,
            "repo_root_missing",
            {"expected": str(repo_root)},
            {"actual": "missing"},
            reason_code="REPO_ACQUIRE_FAILED",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Repo root missing", rejection_path)

    override, override_path = _load_override(run_root)
    allow_override = allow_repo_override or override is not None
    try:
        actual_snapshot = capture_repo_snapshot(repo_root)
    except Exception as exc:
        if not allow_override:
            rejection_path = _write_rejection(
                run_root,
                "repo_snapshot_unavailable",
                {"expected": bundle.repo_snapshot.model_dump()},
                {"actual": str(exc)},
                reason_code="REPO_ACQUIRE_FAILED",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Repo snapshot unavailable", rejection_path)
        if override is None:
            override_path = _write_override(
                run_root,
                "REPO_SNAPSHOT_OVERRIDE",
                {"expected": bundle.repo_snapshot.model_dump()},
                {"actual": str(exc)},
            )
        actual_snapshot = bundle.repo_snapshot

    if (
        actual_snapshot.commit_sha != bundle.repo_snapshot.commit_sha
        or actual_snapshot.dirty != bundle.repo_snapshot.dirty
    ):
        if not allow_override:
            rejection_path = _write_rejection(
                run_root,
                "repo_snapshot_mismatch",
                {"expected": bundle.repo_snapshot.model_dump()},
                {"actual": actual_snapshot.model_dump()},
                reason_code="TREE_HASH_MISMATCH",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Repo snapshot mismatch", rejection_path)
        if override is None:
            override_path = _write_override(
                run_root,
                "REPO_SNAPSHOT_OVERRIDE",
                {"expected": bundle.repo_snapshot.model_dump()},
                {"actual": actual_snapshot.model_dump()},
            )

    if bundle.repo_snapshot.tree_hash and actual_snapshot.tree_hash != bundle.repo_snapshot.tree_hash:
        if not allow_override:
            rejection_path = _write_rejection(
                run_root,
                "repo_tree_hash_mismatch",
                {"expected": bundle.repo_snapshot.tree_hash},
                {"actual": actual_snapshot.tree_hash},
                reason_code="TREE_HASH_MISMATCH",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Repo tree hash mismatch", rejection_path)
        if override is None:
            override_path = _write_override(
                run_root,
                "TREE_HASH_MATCHES_AFTER_PATCH",
                {"expected": bundle.repo_snapshot.tree_hash},
                {"actual": actual_snapshot.tree_hash},
            )

    inputs = {
        "plan_package_final": plan_path,
        "plan_freeze_record": freeze_path,
        "config_snapshot": _resolve_path(bundle_path, bundle.config_snapshot.path),
        "human_feedback_bundle": _resolve_path(bundle_path, bundle.human_feedback_bundle.path),
    }
    if bundle.clarifications.required and bundle.clarifications.path:
        inputs["clarification_resolutions"] = _resolve_path(bundle_path, bundle.clarifications.path)
    if bundle.approval.required and bundle.approval.path:
        inputs["plan_approval"] = _resolve_path(bundle_path, bundle.approval.path)
    if freeze_record.repo_snapshot_ref is not None:
        inputs["repo_snapshot"] = _resolve_path(bundle_path, freeze_record.repo_snapshot_ref.path)
    if getattr(freeze_record, "env_snapshot_ref", None) is not None:
        inputs["env_snapshot"] = _resolve_path(bundle_path, freeze_record.env_snapshot_ref.path)
    if freeze_record.hash_spec_ref is not None:
        inputs["hash_spec"] = _resolve_path(bundle_path, freeze_record.hash_spec_ref.path)
    if "hash_spec" not in inputs and hash_spec_path is not None and hash_spec_path.exists():
        inputs["hash_spec"] = hash_spec_path
    if "repo_snapshot" not in inputs:
        for candidate in (
            base_root / "planning" / "artifacts" / "repo_snapshot.json",
            base_root / "planning" / "repo_snapshot.json",
            base_root / "repo_snapshot.json",
        ):
            if candidate.exists():
                inputs["repo_snapshot"] = candidate
                break
    inputs["planning_handoff_bundle"] = bundle_path
    inputs["repo_context"] = repo_context_path
    inputs["workspace_context"] = workspace_context_path
    if override_path:
        inputs["handoff_override"] = override_path

    copied = copy_with_provenance(inputs, run_root / "implementation" / "inputs" / "planning")

    imported = [
        ImportedArtifactRef(ref=item.path, digest=item.sha256) for item in bundle.required_artifacts
    ]
    env_fingerprint = None
    env_path = copied.get("env_snapshot")
    if env_path is not None and Path(env_path).exists():
        env_fingerprint = artifact_hash(_load_json(Path(env_path)))
    ack = HandoffAck(
        implementation_run_id=implementation_run_id,
        accepted_at=datetime.now(UTC).isoformat(),
        accepted_handoff_digest=bundle.handoff_digest,
        accepted_plan_content_hash=plan_hash,
        accepted_repo_commit=actual_snapshot.commit_sha,
        accepted_repo_tree_hash=actual_snapshot.tree_hash,
        accepted_config_sha256=bundle.config_snapshot.sha256,
        implementation_engine_version=implementation_engine_version,
        env_fingerprint=env_fingerprint,
        imported_artifacts=imported,
        acquired_repo={
            "git_commit": actual_snapshot.commit_sha,
            "git_tree_hash": actual_snapshot.tree_hash,
        },
    )
    _write_json(ack_path, ack.model_dump())
    append_vnext_event(
        run_root,
        "implementation",
        "HANDOFF_ACCEPTED",
        run_id=implementation_run_id,
        plan_hash=plan_hash,
        payload={"handoff_digest": bundle.handoff_digest},
    )

    append_event(
        run_root,
        new_event(
            UUID(implementation_run_id),
            stage="intake",
            actor={"kind": "orchestrator", "role": "implementation_engine"},
            event_type="handoff_accept",
            payload={
                "handoff_digest": bundle.handoff_digest,
                "plan_content_hash": plan_hash,
                "repo_commit": bundle.repo_snapshot.commit_sha,
                "repo_tree_hash": bundle.repo_snapshot.tree_hash,
                "override_path": str(override_path) if override_path else None,
            },
        ),
    )

    return AcceptedHandoff(
        bundle_path=copied.get("planning_handoff_bundle", bundle_path),
        plan_path=copied["plan_package_final"],
        repo_context_path=copied["repo_context"],
        workspace_context_path=copied["workspace_context"],
        config_snapshot_path=copied["config_snapshot"],
        plan_content_hash=plan_hash,
        handoff_digest=bundle.handoff_digest,
    )


def accept_handoff_manifest(
    *,
    manifest_path: Path,
    run_root: Path,
    implementation_run_id: str,
    implementation_engine_version: str,
    allow_repo_override: bool = False,
) -> AcceptedHandoff:
    manifest = HandoffManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))

    ack_path = run_root / "handoff_ack.json"
    if ack_path.exists():
        ack = HandoffAck.model_validate_json(ack_path.read_text(encoding="utf-8"))
        if ack.accepted_handoff_digest != manifest.handoff_digest:
            raise HandoffRejected("Existing handoff ack digest mismatch", ack_path)
        plan_ref_path = _resolve_manifest_ref(manifest_path, manifest.pointers.plan_ref)
        repo_context_artifact = _find_manifest_artifact(manifest, schema_version="repo_context.v1")
        workspace_context_artifact = _find_manifest_artifact(manifest, schema_version="workspace_context.v1")
        config_artifact = _find_manifest_artifact(manifest, schema_version="config_snapshot.v1")
        return AcceptedHandoff(
            bundle_path=manifest_path,
            plan_path=plan_ref_path,
            repo_context_path=_resolve_manifest_ref(manifest_path, repo_context_artifact.ref)
            if repo_context_artifact
            else _resolve_manifest_ref(manifest_path, _artifact_ref("repo_context.json")),
            workspace_context_path=_resolve_manifest_ref(manifest_path, workspace_context_artifact.ref)
            if workspace_context_artifact
            else _resolve_manifest_ref(manifest_path, _artifact_ref("workspace_context.json")),
            config_snapshot_path=_resolve_manifest_ref(manifest_path, config_artifact.ref)
            if config_artifact
            else _resolve_manifest_ref(manifest_path, _artifact_ref("config_snapshot.json")),
            plan_content_hash=ack.accepted_plan_content_hash,
            handoff_digest=ack.accepted_handoff_digest,
        )

    computed_digest = compute_manifest_digest(manifest)
    if computed_digest != manifest.handoff_digest:
        rejection_path = _write_rejection(
            run_root,
            "handoff_digest_mismatch",
            {"expected": manifest.handoff_digest},
            {"actual": computed_digest},
            reason_code="HASH_MISMATCH",
            run_id=implementation_run_id,
        )
        raise HandoffRejected("Handoff manifest digest mismatch", rejection_path)

    for artifact in manifest.artifacts:
        if artifact.role.upper() != "REQUIRED":
            continue
        artifact_path = _resolve_manifest_ref(manifest_path, artifact.ref)
        if not artifact_path.exists():
            rejection_path = _write_rejection(
                run_root,
                "required_artifact_missing",
                {"path": artifact.ref},
                {"actual": "missing"},
                reason_code="MISSING_ARTIFACT",
                run_id=implementation_run_id,
            )
            raise HandoffRejected(f"Missing required artifact: {artifact.ref}", rejection_path)
        actual_hash = artifact_hash(_load_json(artifact_path))
        if actual_hash != artifact.digest:
            rejection_path = _write_rejection(
                run_root,
                "required_artifact_hash_mismatch",
                {"path": artifact.ref, "expected": artifact.digest},
                {"actual": actual_hash},
                reason_code="HASH_MISMATCH",
                run_id=implementation_run_id,
            )
            raise HandoffRejected(f"Hash mismatch for required artifact: {artifact.ref}", rejection_path)

    hash_spec_path = _resolve_manifest_ref(manifest_path, manifest.pointers.hash_spec_ref)
    hash_spec = _load_hash_spec(hash_spec_path if hash_spec_path.exists() else None)
    if hash_spec.hash_spec_version != manifest.hash_spec_version:
        rejection_path = _write_rejection(
            run_root,
            "hash_spec_version_mismatch",
            {"expected": manifest.hash_spec_version},
            {"actual": hash_spec.hash_spec_version},
            reason_code="SCHEMA_UNSUPPORTED",
            run_id=implementation_run_id,
        )
        raise HandoffRejected("Hash spec version mismatch", rejection_path)

    plan_path = _resolve_manifest_ref(manifest_path, manifest.pointers.plan_ref)
    plan_payload = _load_json(plan_path)
    plan_meta = plan_payload.get("meta", {}) if isinstance(plan_payload, dict) else {}
    if plan_meta.get("plan_status") != "FROZEN":
        rejection_path = _write_rejection(
            run_root,
            "plan_not_frozen",
            {"expected": "FROZEN"},
            {"actual": plan_meta.get("plan_status")},
            reason_code="SCHEMA_UNSUPPORTED",
            run_id=implementation_run_id,
        )
        raise HandoffRejected("plan_package_final must be frozen", rejection_path)

    plan_hash = plan_content_hash(plan_payload, hash_spec=hash_spec)

    freeze_path = _resolve_manifest_ref(manifest_path, manifest.pointers.freeze_record_ref)
    freeze_record = PlanFreezeRecord.model_validate_json(freeze_path.read_text(encoding="utf-8"))
    if freeze_record.plan_content_hash != plan_hash:
        rejection_path = _write_rejection(
            run_root,
            "freeze_record_hash_mismatch",
            {"expected": freeze_record.plan_content_hash},
            {"actual": plan_hash},
            reason_code="HASH_MISMATCH",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Plan freeze record hash mismatch", rejection_path)

    if freeze_record.plan_approval_ref is not None:
        approval_path = _resolve_manifest_ref(manifest_path, freeze_record.plan_approval_ref.path)
        approval = PlanApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
        if not approval.approved:
            rejection_path = _write_rejection(
                run_root,
                "approval_not_granted",
                {"expected": True},
                {"actual": approval.approved},
                reason_code="HASH_MISMATCH",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Plan approval not granted", rejection_path)
        if approval.approved_plan_hash != plan_hash:
            rejection_path = _write_rejection(
                run_root,
                "approval_hash_mismatch",
                {"expected": plan_hash},
                {"actual": approval.approved_plan_hash},
                reason_code="HASH_MISMATCH",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Plan approval hash mismatch", rejection_path)

    if freeze_record.clarification_resolutions_ref is not None:
        clar_path = _resolve_manifest_ref(manifest_path, freeze_record.clarification_resolutions_ref.path)
        ClarificationResolutions.model_validate_json(clar_path.read_text(encoding="utf-8"))

    repo_context_artifact = _find_manifest_artifact(manifest, schema_version="repo_context.v1")
    repo_context_path = (
        _resolve_manifest_ref(manifest_path, repo_context_artifact.ref)
        if repo_context_artifact
        else _resolve_manifest_ref(manifest_path, _artifact_ref("repo_context.json"))
    )
    repo_context_payload = _load_json(repo_context_path)
    primary_root = Path(str(repo_context_payload.get("repo_root", ""))).expanduser()
    repo_specs_raw = manifest.repo_acquisition_spec
    if isinstance(repo_specs_raw, list):
        repo_specs = repo_specs_raw
    else:
        repo_specs = [repo_specs_raw]
    if not repo_specs:
        rejection_path = _write_rejection(
            run_root,
            "repo_spec_missing",
            {"expected": "repo_acquisition_spec"},
            {"actual": "empty"},
            reason_code="SCHEMA_UNSUPPORTED",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Repo acquisition spec missing", rejection_path)

    override, override_path = _load_override(run_root)
    allow_override = allow_repo_override or override is not None
    acquired_repos: list[dict[str, Any]] = []
    actual_snapshot: RepoSnapshot | None = None

    for idx, repo_spec in enumerate(repo_specs):
        repo_root = _resolve_repo_checkout(primary_root, repo_spec, idx)
        try:
            _ensure_repo_acquired(repo_root, repo_spec.repo_url)
        except Exception as exc:
            rejection_path = _write_rejection(
                run_root,
                "repo_acquire_failed",
                {"expected": repo_spec.model_dump()},
                {"actual": str(exc)},
                reason_code="REPO_ACQUIRE_FAILED",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Repo acquisition failed", rejection_path)
        if not repo_root.exists():
            rejection_path = _write_rejection(
                run_root,
                "repo_root_missing",
                {"expected": str(repo_root)},
                {"actual": "missing"},
                reason_code="REPO_ACQUIRE_FAILED",
                run_id=implementation_run_id,
                plan_hash=plan_hash,
            )
            raise HandoffRejected("Repo root missing", rejection_path)

        try:
            snapshot = capture_repo_snapshot(repo_root)
        except Exception as exc:
            if not allow_override:
                rejection_path = _write_rejection(
                    run_root,
                    "repo_snapshot_unavailable",
                    {"expected": repo_spec.model_dump()},
                    {"actual": str(exc)},
                    reason_code="REPO_ACQUIRE_FAILED",
                    run_id=implementation_run_id,
                    plan_hash=plan_hash,
                )
                raise HandoffRejected("Repo snapshot unavailable", rejection_path)
            if override is None:
                override_path = _write_override(
                    run_root,
                    "REPO_ACQUIRE_FAILED",
                    {"expected": repo_spec.model_dump()},
                    {"actual": str(exc)},
                    rationale="Repo snapshot unavailable; override allowed.",
                )
            snapshot = capture_repo_snapshot(repo_root)

        if snapshot.commit_sha != repo_spec.git_commit and _git_has_commit(repo_root, repo_spec.git_commit):
            if _checkout_commit(repo_root, repo_spec.git_commit):
                snapshot = capture_repo_snapshot(repo_root)
        if snapshot.commit_sha != repo_spec.git_commit:
            if not allow_override:
                rejection_path = _write_rejection(
                    run_root,
                    "repo_commit_mismatch",
                    {"expected": repo_spec.git_commit, "repo_id": repo_spec.repo_id},
                    {"actual": snapshot.commit_sha},
                    reason_code="TREE_HASH_MISMATCH",
                    run_id=implementation_run_id,
                    plan_hash=plan_hash,
                )
                raise HandoffRejected("Repo commit mismatch", rejection_path)
            if override is None:
                override_path = _write_override(
                    run_root,
                    "FAST_FORWARD_ALLOWED",
                    {"expected": repo_spec.git_commit, "repo_id": repo_spec.repo_id},
                    {"actual": snapshot.commit_sha},
                    rationale="Repo commit mismatch override allowed.",
                )

        if repo_spec.git_tree_hash and snapshot.tree_hash != repo_spec.git_tree_hash:
            if not allow_override:
                rejection_path = _write_rejection(
                    run_root,
                    "tree_hash_mismatch",
                    {"expected": repo_spec.git_tree_hash, "repo_id": repo_spec.repo_id},
                    {"actual": snapshot.tree_hash},
                    reason_code="TREE_HASH_MISMATCH",
                    run_id=implementation_run_id,
                    plan_hash=plan_hash,
                )
                raise HandoffRejected("Repo tree hash mismatch", rejection_path)
            if override is None:
                override_path = _write_override(
                    run_root,
                    "TREE_HASH_MATCHES_AFTER_PATCH",
                    {"expected": repo_spec.git_tree_hash, "repo_id": repo_spec.repo_id},
                    {"actual": snapshot.tree_hash},
                    rationale="Repo tree hash mismatch override allowed.",
                )

        acquired_repos.append(
            {
                "repo_id": repo_spec.repo_id,
                "repo_url": repo_spec.repo_url,
                "checkout_path": repo_spec.checkout_path,
                "repo_root": str(repo_root),
                "git_commit": snapshot.commit_sha,
                "git_tree_hash": snapshot.tree_hash,
            }
        )
        if idx == 0:
            actual_snapshot = snapshot

    if actual_snapshot is None:
        raise RuntimeError("Primary repo snapshot missing after acquisition")

    workspace_context_artifact = _find_manifest_artifact(manifest, schema_version="workspace_context.v1")
    workspace_context_path = (
        _resolve_manifest_ref(manifest_path, workspace_context_artifact.ref)
        if workspace_context_artifact
        else _resolve_manifest_ref(manifest_path, _artifact_ref("workspace_context.json"))
    )
    config_artifact = _find_manifest_artifact(manifest, schema_version="config_snapshot.v1")
    config_snapshot_path = (
        _resolve_manifest_ref(manifest_path, config_artifact.ref)
        if config_artifact
        else _resolve_manifest_ref(manifest_path, _artifact_ref("config_snapshot.json"))
    )
    human_feedback_artifact = _find_manifest_artifact(manifest, schema_version="human_feedback_bundle.v1")
    repo_snapshot_artifact = _find_manifest_artifact(manifest, schema_version="repo_snapshot.v1")

    inputs = {
        "plan_package_final": plan_path,
        "plan_freeze_record": freeze_path,
        "config_snapshot": config_snapshot_path,
        "repo_context": repo_context_path,
        "workspace_context": workspace_context_path,
        "handoff_manifest": manifest_path,
    }
    if repo_snapshot_artifact is not None:
        inputs["repo_snapshot"] = _resolve_manifest_ref(manifest_path, repo_snapshot_artifact.ref)
    if hash_spec_path.exists():
        inputs["hash_spec"] = hash_spec_path
    if manifest.env_requirements_ref is None:
        rejection_path = _write_rejection(
            run_root,
            "env_requirements_missing",
            {"expected": "env_snapshot"},
            {"actual": "missing"},
            reason_code="ENV_MISMATCH",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Env requirements missing", rejection_path)
    env_snapshot_path = _resolve_manifest_ref(manifest_path, manifest.env_requirements_ref)
    if not env_snapshot_path.exists():
        rejection_path = _write_rejection(
            run_root,
            "env_requirements_missing",
            {"expected": manifest.env_requirements_ref},
            {"actual": "missing"},
            reason_code="ENV_MISMATCH",
            run_id=implementation_run_id,
            plan_hash=plan_hash,
        )
        raise HandoffRejected("Env requirements missing", rejection_path)
    inputs["env_snapshot"] = env_snapshot_path
    if human_feedback_artifact is not None:
        inputs["human_feedback_bundle"] = _resolve_manifest_ref(manifest_path, human_feedback_artifact.ref)
    if freeze_record.clarification_resolutions_ref is not None:
        inputs["clarification_resolutions"] = _resolve_manifest_ref(
            manifest_path, freeze_record.clarification_resolutions_ref.path
        )
    if freeze_record.plan_approval_ref is not None:
        inputs["plan_approval"] = _resolve_manifest_ref(manifest_path, freeze_record.plan_approval_ref.path)
    if override_path:
        inputs["handoff_override"] = override_path

    copied = copy_with_provenance(inputs, run_root / "implementation" / "inputs" / "planning")

    imported = [
        ImportedArtifactRef(ref=item.ref, digest=item.digest)
        for item in manifest.artifacts
        if item.role.upper() == "REQUIRED"
    ]
    accepted_config_sha = (
        config_artifact.digest
        if config_artifact is not None
        else artifact_hash(_load_json(config_snapshot_path))
    )
    env_fingerprint = None
    env_path = copied.get("env_snapshot")
    if env_path is not None and Path(env_path).exists():
        env_fingerprint = artifact_hash(_load_json(Path(env_path)))
    ack = HandoffAck(
        implementation_run_id=implementation_run_id,
        accepted_at=datetime.now(UTC).isoformat(),
        accepted_handoff_digest=manifest.handoff_digest,
        accepted_plan_content_hash=plan_hash,
        accepted_repo_commit=actual_snapshot.commit_sha,
        accepted_repo_tree_hash=actual_snapshot.tree_hash,
        accepted_config_sha256=accepted_config_sha,
        implementation_engine_version=implementation_engine_version,
        env_fingerprint=env_fingerprint,
        imported_artifacts=imported,
        acquired_repo={
            "git_commit": actual_snapshot.commit_sha,
            "git_tree_hash": actual_snapshot.tree_hash,
        },
        acquired_repos=acquired_repos or None,
    )
    _write_json(ack_path, ack.model_dump())
    append_vnext_event(
        run_root,
        "implementation",
        "HANDOFF_ACCEPTED",
        run_id=implementation_run_id,
        plan_hash=plan_hash,
        payload={"handoff_digest": manifest.handoff_digest},
    )

    append_event(
        run_root,
        new_event(
            UUID(implementation_run_id),
            stage="intake",
            actor={"kind": "orchestrator", "role": "implementation_engine"},
            event_type="handoff_accept",
            payload={
                "handoff_digest": manifest.handoff_digest,
                "plan_content_hash": plan_hash,
                "repo_commit": actual_snapshot.commit_sha,
                "repo_tree_hash": actual_snapshot.tree_hash,
                "override_path": str(override_path) if override_path else None,
            },
        ),
    )

    return AcceptedHandoff(
        bundle_path=copied.get("handoff_manifest", manifest_path),
        plan_path=copied["plan_package_final"],
        repo_context_path=copied["repo_context"],
        workspace_context_path=copied["workspace_context"],
        config_snapshot_path=copied["config_snapshot"],
        plan_content_hash=plan_hash,
        handoff_digest=manifest.handoff_digest,
    )

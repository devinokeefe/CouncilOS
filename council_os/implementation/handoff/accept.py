from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from council_os.handoff.hashing import artifact_hash, plan_content_hash
from council_os.handoff.schemas import (
    HandoffAck,
    HandoffRejection,
    PlanFreezeRecord,
    PlanningHandoffBundle,
)
from council_os.implementation.event_log import append_event, new_event
from council_os.implementation.handoff.errors import HandoffRejected
from council_os.implementation.handoff.importer import copy_with_provenance
from council_os.orchestrator.feedback.schemas import ClarificationResolutions, PlanApproval
from council_os.orchestrator.feedback.utils import stable_json_dumps
from council_os.orchestrator.handoff.repo_snapshot import capture_repo_snapshot


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(stable_json_dumps(payload), encoding="utf-8")


def _write_rejection(run_root: Path, reason: str, expected: dict[str, Any], actual: dict[str, Any]) -> Path:
    rejection = HandoffRejection(
        rejected_at=datetime.now(UTC).isoformat(),
        reason=reason,
        expected=expected,
        actual=actual,
    )
    path = run_root / "handoff_rejection.json"
    _write_json(path, rejection.model_dump())
    return path


def _write_override(run_root: Path, reason: str, expected: dict[str, Any], actual: dict[str, Any]) -> Path:
    override = HandoffRejection(
        rejected_at=datetime.now(UTC).isoformat(),
        reason=reason,
        expected=expected,
        actual=actual,
    )
    path = run_root / "handoff_override.json"
    _write_json(path, override.model_dump())
    return path


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
        )
        raise HandoffRejected("Handoff digest mismatch", rejection_path)

    for item in bundle.required_artifacts:
        artifact_path = _resolve_path(bundle_path, item.path)
        if not artifact_path.exists():
            rejection_path = _write_rejection(
                run_root,
                "required_artifact_missing",
                {"path": item.path},
                {"actual": "missing"},
            )
            raise HandoffRejected(f"Missing required artifact: {item.path}", rejection_path)
        actual_hash = artifact_hash(_load_json(artifact_path))
        if actual_hash != item.sha256:
            rejection_path = _write_rejection(
                run_root,
                "required_artifact_hash_mismatch",
                {"path": item.path, "expected": item.sha256},
                {"actual": actual_hash},
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
        )
        raise HandoffRejected("plan_package_final must be frozen", rejection_path)

    plan_hash = plan_content_hash(plan_payload)
    if plan_hash != bundle.final_plan.plan_content_hash:
        rejection_path = _write_rejection(
            run_root,
            "plan_hash_mismatch",
            {"expected": bundle.final_plan.plan_content_hash},
            {"actual": plan_hash},
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
        )
        raise HandoffRejected("Plan freeze record hash mismatch", rejection_path)

    if bundle.approval.required:
        if not bundle.approval.path:
            rejection_path = _write_rejection(
                run_root,
                "approval_missing",
                {"expected": "plan_approval.json"},
                {"actual": "missing"},
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
            )
            raise HandoffRejected("Plan approval not granted", rejection_path)
        if approval.approved_plan_hash != plan_hash:
            rejection_path = _write_rejection(
                run_root,
                "approval_hash_mismatch",
                {"expected": plan_hash},
                {"actual": approval.approved_plan_hash},
            )
            raise HandoffRejected("Plan approval hash mismatch", rejection_path)

    if bundle.clarifications.required:
        if not bundle.clarifications.path:
            rejection_path = _write_rejection(
                run_root,
                "clarifications_missing",
                {"expected": "clarification_resolutions.json"},
                {"actual": "missing"},
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
        )
        raise HandoffRejected("Repo root missing", rejection_path)

    override_path = None
    try:
        actual_snapshot = capture_repo_snapshot(repo_root)
    except Exception as exc:
        if not allow_repo_override:
            rejection_path = _write_rejection(
                run_root,
                "repo_snapshot_unavailable",
                {"expected": bundle.repo_snapshot.model_dump()},
                {"actual": str(exc)},
            )
            raise HandoffRejected("Repo snapshot unavailable", rejection_path)
        override_path = _write_override(
            run_root,
            "repo_snapshot_override",
            {"expected": bundle.repo_snapshot.model_dump()},
            {"actual": str(exc)},
        )
        actual_snapshot = bundle.repo_snapshot

    if (
        actual_snapshot.commit_sha != bundle.repo_snapshot.commit_sha
        or actual_snapshot.dirty != bundle.repo_snapshot.dirty
    ):
        if not allow_repo_override:
            rejection_path = _write_rejection(
                run_root,
                "repo_snapshot_mismatch",
                {"expected": bundle.repo_snapshot.model_dump()},
                {"actual": actual_snapshot.model_dump()},
            )
            raise HandoffRejected("Repo snapshot mismatch", rejection_path)
        override_path = _write_override(
            run_root,
            "repo_snapshot_override",
            {"expected": bundle.repo_snapshot.model_dump()},
            {"actual": actual_snapshot.model_dump()},
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
    inputs["planning_handoff_bundle"] = bundle_path
    inputs["repo_context"] = repo_context_path
    inputs["workspace_context"] = workspace_context_path

    copied = copy_with_provenance(inputs, run_root / "inputs" / "planning")

    ack = HandoffAck(
        implementation_run_id=implementation_run_id,
        accepted_at=datetime.now(UTC).isoformat(),
        accepted_handoff_digest=bundle.handoff_digest,
        accepted_plan_content_hash=plan_hash,
        accepted_repo_commit=actual_snapshot.commit_sha,
        accepted_config_sha256=bundle.config_snapshot.sha256,
        implementation_engine_version=implementation_engine_version,
    )
    _write_json(ack_path, ack.model_dump())

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

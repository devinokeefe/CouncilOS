from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from council_os.handoff.hashing import artifact_hash, default_hash_spec, plan_content_hash
from council_os.handoff.schemas import (
    ApprovalRef,
    ClarificationsRef,
    HandoffManifest,
    HandoffArtifactRef,
    HumanFeedbackBundle,
    ManifestArtifactRef,
    ManifestPointers,
    PlanFreezeRecord,
    PlanningHandoffBundle,
    RepoAcquisitionSpec,
)
from council_os.handoff.manifest import with_manifest_digest
from council_os.orchestrator.feedback.config import FeedbackConfig
from council_os.orchestrator.feedback.event_log import append_event, new_event
from council_os.orchestrator.feedback.schemas import (
    ClarificationResolutions,
    PlanApproval,
)
from council_os.orchestrator.feedback.store import PlanningArtifactStore
from council_os.orchestrator.feedback.utils import stable_json_dumps
from council_os.orchestrator.handoff.bundle import compute_handoff_digest
from council_os.orchestrator.handoff.repo_snapshot import capture_repo_snapshot, resolve_repo_url


def _planning_ref(name: str) -> str:
    return f"planning/{name}"


def _artifact_ref(name: str) -> str:
    return f"artifacts/{name}"


def _artifacts_root(run_root: Path) -> Path:
    path = run_root / "planning" / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_config_snapshot(config: dict[str, Any], feedback_cfg: FeedbackConfig) -> dict[str, Any]:
    snapshot = deepcopy(config)
    snapshot["feedback"] = {
        "clarify": feedback_cfg.clarify,
        "plan_review": feedback_cfg.plan_review,
        "provider": feedback_cfg.provider,
        "max_plan_review_rounds": feedback_cfg.max_plan_review_rounds,
        "require_explicit_approval": feedback_cfg.require_explicit_approval,
    }
    return snapshot


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any], *, force: bool) -> None:
    encoded = stable_json_dumps(payload)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == payload:
            return
        if not force:
            raise RuntimeError(f"Handoff artifact already exists with different content: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")


def _write_hash_spec(*, run_root: Path, force: bool) -> tuple[HashSpec, HandoffArtifactRef]:
    spec = default_hash_spec()
    artifacts_root = _artifacts_root(run_root)
    artifact_path = artifacts_root / "hash_spec.json"
    _write_json(artifact_path, spec.model_dump(), force=force)
    planning_path = run_root / "planning" / "hash_spec.json"
    _write_json(planning_path, spec.model_dump(), force=force)
    return spec, HandoffArtifactRef(path=_artifact_ref("hash_spec.json"), sha256=artifact_hash(spec.model_dump()))


def _find_latest_draft(store: PlanningArtifactStore) -> tuple[dict[str, Any], int]:
    versions: list[int] = []
    for path in store.root.glob("plan_package_draft_v*.json"):
        name = path.name
        if name.startswith("plan_package_draft_v") and name.endswith(".json"):
            try:
                versions.append(int(name[len("plan_package_draft_v") : -len(".json")]))
            except ValueError:
                continue
    if not versions:
        raise FileNotFoundError("No plan_package_draft_v*.json found")
    versions.sort()
    latest = versions[-1]
    return store.read_json(f"plan_package_draft_v{latest}.json"), latest


def _load_selected_draft(
    store: PlanningArtifactStore,
    selected_draft_name: str | None,
) -> tuple[dict[str, Any], int, str]:
    if selected_draft_name:
        name = Path(selected_draft_name).name
        if not (name.startswith("plan_package_draft_v") and name.endswith(".json")):
            raise ValueError(f"Invalid selected_draft_name: {selected_draft_name}")
        suffix = name[len("plan_package_draft_v") : -len(".json")]
        try:
            version = int(suffix)
        except ValueError as exc:
            raise ValueError(f"Invalid draft version in {selected_draft_name}") from exc
        return store.read_json(name), version, name
    draft_plan, draft_version = _find_latest_draft(store)
    return draft_plan, draft_version, f"plan_package_draft_v{draft_version}.json"


def _ensure_context_snapshot(
    *,
    run_root: Path,
    label: str,
    source_path: Path | None,
    force: bool,
) -> Path:
    dest = run_root / f"{label}.json"
    if source_path is None:
        if dest.exists():
            return dest
        raise FileNotFoundError(f"Missing required {label}.json")
    source_path = source_path.resolve()
    if dest.exists():
        if dest.resolve() == source_path:
            return dest
        if not force:
            raise RuntimeError(f"{label}.json already exists at {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, dest)
    return dest


def _build_human_feedback_bundle(
    *,
    store: PlanningArtifactStore,
    clarification_ref: HandoffArtifactRef | None,
    approval_ref: HandoffArtifactRef | None,
    clarification_resolutions: ClarificationResolutions | None,
) -> HumanFeedbackBundle:
    clarifications = []
    if clarification_resolutions is not None:
        for idx, resolution in enumerate(clarification_resolutions.resolutions):
            clarifications.append(
                {
                    "question_id": resolution.question_id,
                    "resolved_value": resolution.resolved_value,
                    "source": resolution.source,
                    "evidence": [
                        {
                            "artifact_ref": _planning_ref("clarification_resolutions.json"),
                            "entity_id": resolution.question_id,
                            "json_pointer": f"/resolutions/{idx}",
                            "note": None,
                            "quote": None,
                        }
                    ],
                }
            )

    rounds = []
    for path in sorted(store.root.glob("plan_review_feedback_v*.json")):
        name = path.name
        suffix = name[len("plan_review_feedback_v") : -len(".json")]
        try:
            round_idx = int(suffix)
        except ValueError:
            continue
        feedback_ref = HandoffArtifactRef(path=_planning_ref(name), sha256=artifact_hash(_load_json(path)))
        diff_path = store.path(f"plan_diff_summary_v{round_idx}.json")
        diff_ref = None
        if diff_path.exists():
            diff_ref = HandoffArtifactRef(
                path=_planning_ref(diff_path.name), sha256=artifact_hash(_load_json(diff_path))
            )
        rounds.append(
            {
                "round": round_idx,
                "feedback_ref": feedback_ref.model_dump(),
                "diff_ref": diff_ref.model_dump() if diff_ref else None,
            }
        )

    bundle = HumanFeedbackBundle(
        clarifications=clarifications,
        clarification_resolutions_ref=clarification_ref,
        plan_review_rounds=[
            {
                "round": item["round"],
                "feedback_ref": item["feedback_ref"],
                "diff_ref": item["diff_ref"],
            }
            for item in rounds
        ],
        approval_ref=approval_ref,
        notes=[],
    )
    return bundle


def finalize_and_write_handoff(
    *,
    run_id: str,
    run_root: Path,
    plan: dict[str, Any],
    feedback_cfg: FeedbackConfig,
    config_snapshot: dict[str, Any],
    repo_context_path: Path | None = None,
    workspace_context_path: Path | None = None,
    selected_draft_name: str | None = None,
    force: bool = False,
) -> PlanningHandoffBundle:
    store = PlanningArtifactStore(run_root)
    artifacts_root = _artifacts_root(run_root)
    append_event(
        run_root,
        new_event(
            run_id=run_id,
            stage="planning",
            event_type="PLAN_FINALIZATION_STARTED",
        ),
    )

    try:
        draft_plan, draft_version, draft_name = _load_selected_draft(store, selected_draft_name)
    except FileNotFoundError:
        draft_version = 1
        draft_plan = plan
        draft_name = f"plan_package_draft_v{draft_version}.json"
        _write_json(store.path(draft_name), draft_plan, force=force)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    _write_json(artifacts_root / draft_name, draft_plan, force=force)

    hash_spec, hash_spec_ref = _write_hash_spec(run_root=run_root, force=force)
    selected_draft_ref = _artifact_ref(draft_name)
    selected_draft_hash = plan_content_hash(draft_plan, hash_spec=hash_spec)

    approval: PlanApproval | None = None
    if feedback_cfg.plan_review:
        if not store.exists("plan_approval.json"):
            raise RuntimeError("Plan review approval required before handoff")
        approval = PlanApproval.model_validate_json(
            store.path("plan_approval.json").read_text(encoding="utf-8")
        )
        if not approval.approved:
            raise RuntimeError("Plan review approval required before handoff")
        if approval.approved_plan_hash != selected_draft_hash:
            raise RuntimeError("Plan approval hash does not match latest draft plan")

    clarification_resolutions: ClarificationResolutions | None = None
    if feedback_cfg.clarify:
        if not store.exists("clarification_resolutions.json"):
            raise RuntimeError("Clarification resolutions required before handoff")
        clarification_resolutions = ClarificationResolutions.model_validate_json(
            store.path("clarification_resolutions.json").read_text(encoding="utf-8")
        )

    plan_final = deepcopy(draft_plan)
    meta = plan_final.get("meta", {})
    if isinstance(meta, dict):
        meta["plan_status"] = "FROZEN"
        plan_final["meta"] = meta
    plan_hash = plan_content_hash(plan_final, hash_spec=hash_spec)

    _write_json(store.path("config_snapshot.json"), config_snapshot, force=force)
    _write_json(artifacts_root / "config_snapshot.json", config_snapshot, force=force)
    config_ref = HandoffArtifactRef(
        path=_planning_ref("config_snapshot.json"),
        sha256=artifact_hash(config_snapshot),
    )
    config_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref("config_snapshot.json"),
        sha256=artifact_hash(config_snapshot),
    )

    plan_final_path = store.path("plan_package_final.json")
    _write_json(plan_final_path, plan_final, force=force)
    plan_final_artifact_name = f"plan_package_final_v{draft_version}.json"
    _write_json(artifacts_root / plan_final_artifact_name, plan_final, force=force)
    plan_ref = HandoffArtifactRef(
        path=_planning_ref("plan_package_final.json"),
        sha256=artifact_hash(plan_final),
    )
    plan_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref(plan_final_artifact_name),
        sha256=artifact_hash(plan_final),
    )

    append_event(
        run_root,
        new_event(
            run_id=run_id,
            stage="planning",
            event_type="PLAN_FROZEN",
            artifact_refs=[plan_ref.path],
            message=plan_hash,
        ),
    )

    clarification_ref = None
    if clarification_resolutions is not None:
        clarification_ref = HandoffArtifactRef(
            path=_planning_ref("clarification_resolutions.json"),
            sha256=artifact_hash(clarification_resolutions.model_dump()),
        )
        _write_json(
            artifacts_root / "clarification_resolutions.json",
            clarification_resolutions.model_dump(),
            force=force,
        )

    approval_ref = None
    if approval is not None:
        approval_ref = HandoffArtifactRef(
            path=_planning_ref("plan_approval.json"),
            sha256=artifact_hash(approval.model_dump()),
        )
        _write_json(artifacts_root / "plan_approval.json", approval.model_dump(), force=force)

    human_feedback_bundle = _build_human_feedback_bundle(
        store=store,
        clarification_ref=clarification_ref,
        approval_ref=approval_ref,
        clarification_resolutions=clarification_resolutions,
    )
    _write_json(store.path("human_feedback_bundle.json"), human_feedback_bundle.model_dump(), force=force)
    _write_json(artifacts_root / "human_feedback_bundle.json", human_feedback_bundle.model_dump(), force=force)
    human_feedback_ref = HandoffArtifactRef(
        path=_planning_ref("human_feedback_bundle.json"),
        sha256=artifact_hash(human_feedback_bundle.model_dump()),
    )
    human_feedback_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref("human_feedback_bundle.json"),
        sha256=artifact_hash(human_feedback_bundle.model_dump()),
    )

    repo_context = _ensure_context_snapshot(
        run_root=run_root,
        label="repo_context",
        source_path=repo_context_path,
        force=force,
    )
    workspace_context = _ensure_context_snapshot(
        run_root=run_root,
        label="workspace_context",
        source_path=workspace_context_path,
        force=force,
    )
    shutil.copyfile(repo_context, artifacts_root / "repo_context.json")
    shutil.copyfile(workspace_context, artifacts_root / "workspace_context.json")

    repo_context_payload = _load_json(repo_context)
    repo_context_ref = HandoffArtifactRef(
        path="repo_context.json",
        sha256=artifact_hash(repo_context_payload),
    )
    workspace_context_ref = HandoffArtifactRef(
        path="workspace_context.json",
        sha256=artifact_hash(_load_json(workspace_context)),
    )
    repo_context_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref("repo_context.json"),
        sha256=artifact_hash(repo_context_payload),
    )
    workspace_context_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref("workspace_context.json"),
        sha256=artifact_hash(_load_json(workspace_context)),
    )

    repo_root_value = repo_context_payload.get("repo_root") if isinstance(repo_context_payload, dict) else None
    repo_root = Path(str(repo_root_value)).expanduser() if repo_root_value else run_root
    repo_snapshot = capture_repo_snapshot(repo_root)
    repo_snapshot_path = artifacts_root / "repo_snapshot.json"
    _write_json(repo_snapshot_path, repo_snapshot.model_dump(), force=force)
    repo_snapshot_ref = HandoffArtifactRef(
        path=_artifact_ref("repo_snapshot.json"),
        sha256=artifact_hash(repo_snapshot.model_dump()),
    )

    plan_freeze = PlanFreezeRecord(
        plan_id=str(plan_final.get("meta", {}).get("plan_id", "")),
        planning_run_id=run_id,
        frozen_at=datetime.now(UTC).isoformat(),
        plan_package_final_ref=plan_ref,
        plan_content_hash=plan_hash,
        hash_spec_version=hash_spec.hash_spec_version,
        selected_draft_ref=selected_draft_ref,
        selected_draft_hash=selected_draft_hash,
        interactive_flags={
            "clarify_intent_enabled": feedback_cfg.clarify,
            "plan_review_enabled": feedback_cfg.plan_review,
        },
        clarification_resolutions_ref=clarification_ref,
        plan_approval_ref=approval_ref,
        approved_plan_hash=approval.approved_plan_hash if approval is not None else None,
        repo_snapshot_ref=repo_snapshot_ref,
        hash_spec_ref=hash_spec_ref,
        config_snapshot_ref=config_ref,
        human_feedback_bundle_ref=human_feedback_ref,
        notes=f"Frozen after review round {draft_version}" if feedback_cfg.plan_review else "Frozen without review",
    )
    _write_json(store.path("plan_freeze_record.json"), plan_freeze.model_dump(), force=force)
    _write_json(artifacts_root / "plan_freeze_record.json", plan_freeze.model_dump(), force=force)
    freeze_ref = HandoffArtifactRef(
        path=_planning_ref("plan_freeze_record.json"),
        sha256=artifact_hash(plan_freeze.model_dump()),
    )
    freeze_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref("plan_freeze_record.json"),
        sha256=artifact_hash(plan_freeze.model_dump()),
    )

    approval_payload = ApprovalRef(required=feedback_cfg.plan_review)
    if feedback_cfg.plan_review and approval_ref is not None:
        approval_payload = ApprovalRef(
            required=True,
            path=approval_ref.path,
            sha256=approval_ref.sha256,
            approved_plan_hash=approval.approved_plan_hash if approval is not None else None,
        )

    clarifications_payload = ClarificationsRef(required=feedback_cfg.clarify)
    if feedback_cfg.clarify and clarification_ref is not None:
        clarifications_payload = ClarificationsRef(
            required=True,
            path=clarification_ref.path,
            sha256=clarification_ref.sha256,
        )

    manifest_artifacts: list[ManifestArtifactRef] = [
        ManifestArtifactRef(
            ref=plan_ref_artifact.path,
            digest=plan_ref_artifact.sha256,
            schema_version="plan_package.v1",
            role="REQUIRED",
        ),
        ManifestArtifactRef(
            ref=freeze_ref_artifact.path,
            digest=freeze_ref_artifact.sha256,
            schema_version="plan_freeze_record.v1",
            role="REQUIRED",
        ),
        ManifestArtifactRef(
            ref=hash_spec_ref.path,
            digest=hash_spec_ref.sha256,
            schema_version="hash_spec.v1",
            role="REQUIRED",
        ),
        ManifestArtifactRef(
            ref=config_ref_artifact.path,
            digest=config_ref_artifact.sha256,
            schema_version="config_snapshot.v1",
            role="REQUIRED",
        ),
        ManifestArtifactRef(
            ref=repo_snapshot_ref.path,
            digest=repo_snapshot_ref.sha256,
            schema_version="repo_snapshot.v1",
            role="REQUIRED",
        ),
        ManifestArtifactRef(
            ref=repo_context_ref_artifact.path,
            digest=repo_context_ref_artifact.sha256,
            schema_version="repo_context.v1",
            role="REQUIRED",
        ),
        ManifestArtifactRef(
            ref=workspace_context_ref_artifact.path,
            digest=workspace_context_ref_artifact.sha256,
            schema_version="workspace_context.v1",
            role="REQUIRED",
        ),
    ]
    if human_feedback_ref_artifact:
        manifest_artifacts.append(
            ManifestArtifactRef(
                ref=human_feedback_ref_artifact.path,
                digest=human_feedback_ref_artifact.sha256,
                schema_version="human_feedback_bundle.v1",
                role="OPTIONAL",
            )
        )
    if clarification_ref is not None:
        manifest_artifacts.append(
            ManifestArtifactRef(
                ref=_artifact_ref("clarification_resolutions.json"),
                digest=clarification_ref.sha256,
                schema_version="clarification_resolutions.v1",
                role="OPTIONAL",
            )
        )
    if approval_ref is not None:
        manifest_artifacts.append(
            ManifestArtifactRef(
                ref=_artifact_ref("plan_approval.json"),
                digest=approval_ref.sha256,
                schema_version="plan_approval.v1",
                role="OPTIONAL",
            )
        )
    manifest_artifacts.append(
        ManifestArtifactRef(
            ref=selected_draft_ref,
            digest=artifact_hash(draft_plan),
            schema_version="plan_package.v1",
            role="OPTIONAL",
        )
    )

    repo_url = resolve_repo_url(repo_root) or str(repo_root)
    manifest = HandoffManifest(
        hash_spec_version=hash_spec.hash_spec_version,
        pointers=ManifestPointers(
            plan_ref=plan_ref_artifact.path,
            freeze_record_ref=freeze_ref_artifact.path,
            hash_spec_ref=hash_spec_ref.path,
        ),
        repo_acquisition_spec=RepoAcquisitionSpec(
            repo_url=repo_url,
            git_commit=repo_snapshot.commit_sha,
            git_tree_hash=repo_snapshot.tree_hash,
            submodules="NONE",
        ),
        artifacts=manifest_artifacts,
        env_requirements_ref=None,
        handoff_digest="",
    )
    manifest = with_manifest_digest(manifest)
    _write_json(artifacts_root / "handoff_manifest.json", manifest.model_dump(), force=force)
    _write_json(store.path("handoff_manifest.json"), manifest.model_dump(), force=force)

    required_artifacts = [
        plan_ref,
        freeze_ref,
        config_ref,
        repo_context_ref,
        workspace_context_ref,
    ]
    optional_artifacts = [human_feedback_ref]

    bundle = PlanningHandoffBundle(
        handoff_bundle_id=f"HB-{run_id}",
        planning_run_id=run_id,
        implementation_run_id=None,
        final_plan={
            "path": plan_ref.path,
            "sha256": plan_ref.sha256,
            "plan_content_hash": plan_hash,
        },
        freeze_record=freeze_ref,
        approval=approval_payload,
        clarifications=clarifications_payload,
        human_feedback_bundle=human_feedback_ref,
        config_snapshot=config_ref,
        repo_snapshot=repo_snapshot,
        required_artifacts=required_artifacts,
        optional_artifacts=optional_artifacts,
        handoff_digest="",
    )
    bundle = bundle.model_copy(update={"handoff_digest": compute_handoff_digest(bundle)})

    _write_json(store.path("planning_handoff_bundle.json"), bundle.model_dump(), force=force)

    append_event(
        run_root,
        new_event(
            run_id=run_id,
            stage="planning",
            event_type="HANDOFF_BUNDLE_WRITTEN",
            artifact_refs=[
                _planning_ref("planning_handoff_bundle.json"),
                _planning_ref("handoff_manifest.json"),
            ],
            message=bundle.handoff_digest,
        ),
    )
    append_event(
        run_root,
        new_event(
            run_id=run_id,
            stage="planning",
            event_type="HANDOFF_READY",
            artifact_refs=[
                _planning_ref("planning_handoff_bundle.json"),
                _planning_ref("handoff_manifest.json"),
            ],
        ),
    )

    return bundle

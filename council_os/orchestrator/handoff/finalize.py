from __future__ import annotations

import json
import hashlib
import shutil
import re
import platform
import sys
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
    RepoSnapshot,
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
from council_os.orchestrator.handoff.repo_snapshot import (
    capture_repo_snapshot,
    resolve_repo_root,
    resolve_repo_url,
)
from council_os.vnext.events import append_vnext_event


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
        if label == "repo_context":
            repo_payload = _default_repo_context(run_root)
            _write_json(dest, repo_payload, force=True)
            return dest
        if label == "workspace_context":
            workspace_payload = _default_workspace_context()
            _write_json(dest, workspace_payload, force=True)
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


def _default_repo_context(run_root: Path) -> dict[str, Any]:
    repo_root = run_root
    commit = "unknown"
    branch = "unknown"
    try:
        repo_root = resolve_repo_root(Path.cwd())
    except Exception:
        repo_root = run_root
    try:
        snapshot = capture_repo_snapshot(repo_root)
        commit = snapshot.commit_sha
        branch = snapshot.branch
    except Exception:
        pass
    return {
        "repo_root": str(repo_root),
        "base_commit": commit,
        "head_commit": commit,
        "branch_name": branch,
        "build_entrypoints": [],
        "validator_entrypoints": [],
        "protected_paths": [".git/*"],
        "forbidden_paths": [],
        "metadata": {},
        "execution_profiles": None,
        "validator_runners": [],
        "dependency_policy": None,
        "surfaces": [],
        "default_expectations": [],
    }


def _default_workspace_context() -> dict[str, Any]:
    return {
        "workspace_root": "workspace",
        "namespace_rules": [],
        "partition_rules": {
            "canonical_prefixes": ["canonical/"],
            "non_canonical_prefixes": ["non_canonical/"],
        },
        "retention_policy": {"max_age_days": 7, "max_size_mb": 10, "compaction_strategy": "none"},
        "access_control": [],
        "context_pack_limits": {"max_items": 20, "max_bytes": 4000},
    }


def _build_env_snapshot() -> dict[str, Any]:
    return {
        "schema_version": "env_snapshot.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "runtime": {"implementation": sys.implementation.name},
        "notes": [],
    }


def _compute_tree_hash(repo_root: Path) -> str | None:
    if not repo_root.exists():
        return None
    entries: list[str] = []
    for path in sorted(repo_root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(repo_root).as_posix()
        if rel.startswith(".git/"):
            continue
        try:
            content = path.read_bytes()
        except Exception:
            continue
        digest = hashlib.sha256(content).hexdigest()
        entries.append(f"{rel}:{digest}")
    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _safe_repo_snapshot(repo_root: Path) -> RepoSnapshot:
    try:
        return capture_repo_snapshot(repo_root)
    except Exception:
        return RepoSnapshot(
            commit_sha="unknown",
            branch="unknown",
            dirty=True,
            tree_hash=_compute_tree_hash(repo_root),
        )


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


def _build_traceability_matrix(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {"schema_version": "traceability_matrix.v1", "requirements": []}
    requirements = plan.get("requirements", []) if isinstance(plan, dict) else []
    acceptance_tests = plan.get("acceptance_tests", []) if isinstance(plan, dict) else []
    work_items = plan.get("work_items", []) if isinstance(plan, dict) else []
    checks = plan.get("checks") or plan.get("expectations") or []

    use_vnext = bool(work_items or checks or plan.get("scope") or plan.get("constraints"))
    if use_vnext:
        req_ids = {req.get("id") for req in requirements if isinstance(req, dict) and req.get("id")}
        work_item_map: dict[str, set[str]] = {}
        for item in work_items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not item_id:
                continue
            mapped = item.get("maps_to_requirements", []) or []
            work_item_map[item_id] = {req_id for req_id in mapped if req_id}

        def _check_requirements(check: dict[str, Any]) -> set[str]:
            mapped: set[str] = set()
            direct = check.get("maps_to_requirements", []) or []
            for req_id in direct:
                if req_id:
                    mapped.add(req_id)
            via_work = (
                check.get("maps_to_work_items")
                or check.get("maps_to_work_item_ids")
                or check.get("work_item_ids")
                or []
            )
            for item_id in via_work:
                mapped.update(work_item_map.get(item_id, set()))
            return mapped

        matrix: list[dict[str, Any]] = []
        for req_id in sorted(req_ids):
            work_ids = sorted([item_id for item_id, mapped in work_item_map.items() if req_id in mapped])
            check_ids: set[str] = set()
            for check in checks:
                if not isinstance(check, dict):
                    continue
                check_id = check.get("id")
                if not check_id:
                    continue
                if req_id in _check_requirements(check):
                    check_ids.add(check_id)
            matrix.append(
                {
                    "requirement_id": req_id,
                    "work_item_ids": work_ids,
                    "check_ids": sorted(check_ids),
                }
            )
        return {"schema_version": "traceability_matrix.v1", "requirements": matrix}

    matrix: list[dict[str, Any]] = []
    for req in requirements:
        if not isinstance(req, dict):
            continue
        req_id = req.get("id", "")
        mapped = []
        for test in acceptance_tests:
            if not isinstance(test, dict):
                continue
            mapped_to = test.get("maps_to_requirements", []) or []
            if req_id and req_id in mapped_to:
                mapped.append(test.get("id", ""))
        matrix.append({"requirement_id": req_id, "acceptance_test_ids": [m for m in mapped if m]})
    return {"schema_version": "traceability_matrix.v1", "requirements": matrix}


def _build_plan_lint_report(plan: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not isinstance(plan, dict):
        return {"schema_version": "plan_lint_report.v1", "passed": False, "issues": issues}

    requirements = plan.get("requirements", []) if isinstance(plan, dict) else []
    acceptance_tests = plan.get("acceptance_tests", []) if isinstance(plan, dict) else []
    work_items = plan.get("work_items", []) if isinstance(plan, dict) else []
    checks = plan.get("checks") or plan.get("expectations") or []
    milestones = plan.get("milestones", []) if isinstance(plan, dict) else []

    is_vnext = bool(work_items or checks or plan.get("scope") or plan.get("constraints"))

    requirement_ids = {req.get("id") for req in requirements if isinstance(req, dict) and req.get("id")}
    covered: dict[str, int] = {req_id: 0 for req_id in requirement_ids if req_id}

    if work_items:
        for idx, item in enumerate(work_items):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id and not re.match(r"^WI-", str(item_id)):
                issues.append(
                    {
                        "severity": "warning",
                        "message": f"Work item id {item_id} does not match WI-### convention",
                        "json_pointer": f"/work_items/{idx}/id",
                    }
                )
            mapped = item.get("maps_to_requirements", []) or []
            if not mapped:
                issues.append(
                    {
                        "severity": "error",
                        "message": "Work item has no maps_to_requirements entries",
                        "json_pointer": f"/work_items/{idx}/maps_to_requirements",
                    }
                )
            for req_id in mapped:
                if req_id not in requirement_ids:
                    issues.append(
                        {
                            "severity": "error",
                            "message": f"Work item maps to unknown requirement {req_id}",
                            "json_pointer": f"/work_items/{idx}/maps_to_requirements",
                        }
                    )
                else:
                    covered[req_id] = covered.get(req_id, 0) + 1

    if acceptance_tests:
        for idx, test in enumerate(acceptance_tests):
            if not isinstance(test, dict):
                continue
            mapped = test.get("maps_to_requirements", []) or []
            for req_id in mapped:
                if req_id not in requirement_ids:
                    issues.append(
                        {
                            "severity": "error",
                            "message": f"Acceptance test maps to unknown requirement {req_id}",
                            "json_pointer": f"/acceptance_tests/{idx}/maps_to_requirements",
                        }
                    )
                else:
                    covered[req_id] = covered.get(req_id, 0) + 1

    for req_idx, req in enumerate(requirements):
        if not isinstance(req, dict):
            continue
        req_id = req.get("id")
        if req_id and not re.match(r"^REQ-", str(req_id)):
            issues.append(
                {
                    "severity": "warning",
                    "message": f"Requirement id {req_id} does not match REQ-### convention",
                    "json_pointer": f"/requirements/{req_idx}/id",
                }
            )
        if req_id and covered.get(req_id, 0) == 0:
            issues.append(
                {
                    "severity": "error",
                    "message": f"Requirement {req_id} has no mapped work items or acceptance tests",
                    "json_pointer": f"/requirements/{req_idx}",
                }
            )

    for idx, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        check_id = check.get("id")
        if check_id and not re.match(r"^(CHK|EXP)-", str(check_id)):
            issues.append(
                {
                    "severity": "warning",
                    "message": f"Check id {check_id} does not match CHK/EXP convention",
                    "json_pointer": f"/checks/{idx}/id",
                }
            )
        oracle = (
            check.get("oracle")
            or check.get("oracle_ref")
            or check.get("oracle_entrypoint")
            or check.get("entrypoint")
        )
        if not oracle:
            issues.append(
                {
                    "severity": "error",
                    "message": f"Check {check_id or idx} missing oracle mapping",
                    "json_pointer": f"/checks/{idx}",
                }
            )

    for idx, test in enumerate(acceptance_tests):
        if not isinstance(test, dict):
            continue
        test_id = test.get("id")
        if test_id and not re.match(r"^(CHK|EXP|AT)-", str(test_id)):
            issues.append(
                {
                    "severity": "warning",
                    "message": f"Acceptance test id {test_id} does not match CHK/EXP convention",
                    "json_pointer": f"/acceptance_tests/{idx}/id",
                }
            )

    for idx, milestone in enumerate(milestones):
        if not isinstance(milestone, dict):
            continue
        milestone_id = milestone.get("id")
        if milestone_id and not re.match(r"^MS-", str(milestone_id)):
            issues.append(
                {
                    "severity": "warning",
                    "message": f"Milestone id {milestone_id} does not match MS-### convention",
                    "json_pointer": f"/milestones/{idx}/id",
                }
            )
        required_checks = (
            milestone.get("required_check_ids")
            or milestone.get("required_checks")
            or milestone.get("exit_criteria")
        )
        if not required_checks:
            issues.append(
                {
                    "severity": "error" if is_vnext else "warning",
                    "message": "Milestone has no required_check_ids/exit_criteria checks",
                    "json_pointer": f"/milestones/{idx}/required_check_ids",
                }
            )

    scope = plan.get("scope")
    constraints = plan.get("constraints")
    if is_vnext:
        if not scope:
            issues.append(
                {
                    "severity": "error",
                    "message": "Scope section missing; expected scope.in_scope_paths/out_of_scope_paths",
                    "json_pointer": "/scope",
                }
            )
        if not constraints:
            issues.append(
                {
                    "severity": "error",
                    "message": "Constraints section missing; expected constraints.caps",
                    "json_pointer": "/constraints",
                }
            )
    else:
        if "scope" not in plan:
            issues.append(
                {
                    "severity": "warning",
                    "message": "Scope section missing; expected scope.in_scope_paths/out_of_scope_paths",
                    "json_pointer": "/scope",
                }
            )
    capsule = plan.get("project_capsule", {}) if isinstance(plan, dict) else {}
    capsule_constraints = capsule.get("constraints") if isinstance(capsule, dict) else None
    if not capsule_constraints and not constraints:
        issues.append(
            {
                "severity": "warning",
                "message": "No constraints defined in project_capsule.constraints or constraints.caps",
                "json_pointer": "/project_capsule/constraints",
            }
        )

    passed = not any(issue["severity"] == "error" for issue in issues)
    return {"schema_version": "plan_lint_report.v1", "passed": passed, "issues": issues}


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
    approval_ref: HandoffArtifactRef | None = None
    if approval is not None:
        approval_ref = HandoffArtifactRef(
            path=_planning_ref("plan_approval.json"),
            sha256=artifact_hash(approval.model_dump()),
        )

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
        if "hash_spec_version" not in meta:
            meta["hash_spec_version"] = hash_spec.hash_spec_version
        lineage_cfg = config_snapshot.get("plan_lineage") if isinstance(config_snapshot, dict) else None
        if isinstance(lineage_cfg, dict) and not feedback_cfg.plan_review:
            parent_hash = lineage_cfg.get("parent_plan_hash")
            amendment_id = lineage_cfg.get("amendment_id")
            if parent_hash and "parent_plan_hash" not in meta:
                meta["parent_plan_hash"] = parent_hash
            if amendment_id and "amendment_id" not in meta:
                meta["amendment_id"] = amendment_id
        plan_final["meta"] = meta
    plan_hash = plan_content_hash(plan_final, hash_spec=hash_spec)
    append_vnext_event(
        run_root,
        "planning",
        "PLAN_DRAFTED",
        run_id=run_id,
        plan_hash=selected_draft_hash,
        payload={"draft_ref": selected_draft_ref},
    )
    if feedback_cfg.plan_review and approval is not None:
        append_vnext_event(
            run_root,
            "planning",
            "PLAN_REVIEWED",
            run_id=run_id,
            plan_hash=selected_draft_hash,
            payload={"approval_ref": approval_ref.path if approval_ref else None},
        )

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

    env_snapshot_path = store.path("env_snapshot.json")
    if env_snapshot_path.exists():
        env_snapshot_payload = _load_json(env_snapshot_path)
    else:
        env_snapshot_payload = _build_env_snapshot()
        _write_json(env_snapshot_path, env_snapshot_payload, force=force)
    _write_json(artifacts_root / "env_snapshot.json", env_snapshot_payload, force=force)
    env_snapshot_ref = HandoffArtifactRef(
        path=_planning_ref("env_snapshot.json"),
        sha256=artifact_hash(env_snapshot_payload),
    )
    env_snapshot_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref("env_snapshot.json"),
        sha256=artifact_hash(env_snapshot_payload),
    )

    plan_final_path = store.path("plan_package_final.json")
    _write_json(plan_final_path, plan_final, force=force)
    plan_final_artifact_name = f"plan_package_final_v{draft_version}.json"
    _write_json(artifacts_root / plan_final_artifact_name, plan_final, force=force)
    append_vnext_event(
        run_root,
        "planning",
        "PLAN_FROZEN",
        run_id=run_id,
        plan_hash=plan_hash,
        payload={"final_ref": _artifact_ref(plan_final_artifact_name)},
    )
    plan_ref = HandoffArtifactRef(
        path=_planning_ref("plan_package_final.json"),
        sha256=artifact_hash(plan_final),
    )
    plan_ref_artifact = HandoffArtifactRef(
        path=_artifact_ref(plan_final_artifact_name),
        sha256=artifact_hash(plan_final),
    )

    lint_report = _build_plan_lint_report(plan_final)
    _write_json(store.path("plan_lint_report.json"), lint_report, force=force)
    _write_json(artifacts_root / "plan_lint_report.json", lint_report, force=force)
    traceability = _build_traceability_matrix(plan_final)
    _write_json(store.path("traceability_matrix.json"), traceability, force=force)
    _write_json(artifacts_root / "traceability_matrix.json", traceability, force=force)

    plan_migration_record: dict[str, Any] | None = None
    plan_migration_ref: HandoffArtifactRef | None = None
    parent_plan_hash = meta.get("parent_plan_hash") if isinstance(meta, dict) else None
    if parent_plan_hash:
        plan_migration_record = {
            "schema_version": "plan_migration_record.v1",
            "parent_plan_hash": parent_plan_hash,
            "child_plan_hash": plan_hash,
            "amendment_id": meta.get("amendment_id") if isinstance(meta, dict) else None,
            "notes": [],
        }
        _write_json(store.path("plan_migration_record.json"), plan_migration_record, force=force)
        _write_json(artifacts_root / "plan_migration_record.json", plan_migration_record, force=force)
        plan_migration_ref = HandoffArtifactRef(
            path=_planning_ref("plan_migration_record.json"),
            sha256=artifact_hash(plan_migration_record),
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

    if approval is not None:
        if approval_ref is None:
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
    repo_snapshot = _safe_repo_snapshot(repo_root)
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
        env_snapshot_ref=env_snapshot_ref,
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
            ref=env_snapshot_ref_artifact.path,
            digest=env_snapshot_ref_artifact.sha256,
            schema_version="env_snapshot.v1",
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
    manifest_artifacts.append(
        ManifestArtifactRef(
            ref=_artifact_ref("plan_lint_report.json"),
            digest=artifact_hash(lint_report),
            schema_version="plan_lint_report.v1",
            role="OPTIONAL",
        )
    )
    manifest_artifacts.append(
        ManifestArtifactRef(
            ref=_artifact_ref("traceability_matrix.json"),
            digest=artifact_hash(traceability),
            schema_version="traceability_matrix.v1",
            role="OPTIONAL",
        )
    )
    if plan_migration_record is not None:
        manifest_artifacts.append(
            ManifestArtifactRef(
                ref=_artifact_ref("plan_migration_record.json"),
                digest=artifact_hash(plan_migration_record),
                schema_version="plan_migration_record.v1",
                role="OPTIONAL",
            )
        )
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

    try:
        repo_url = resolve_repo_url(repo_root) or str(repo_root)
    except Exception:
        repo_url = str(repo_root)
    primary_repo_id = None
    if isinstance(config_snapshot, dict):
        primary_repo_id = config_snapshot.get("repo_id")
    repo_specs = [
        RepoAcquisitionSpec(
            repo_id=str(primary_repo_id) if primary_repo_id else None,
            repo_url=repo_url,
            git_commit=repo_snapshot.commit_sha,
            git_tree_hash=repo_snapshot.tree_hash,
            submodules="NONE",
        )
    ]
    extra_specs_raw = []
    if isinstance(config_snapshot, dict):
        extra_specs_raw = config_snapshot.get("repo_acquisition_specs") or config_snapshot.get("additional_repos") or []
    if isinstance(extra_specs_raw, list):
        for item in extra_specs_raw:
            if not isinstance(item, dict):
                continue
            try:
                repo_specs.append(RepoAcquisitionSpec.model_validate(item))
            except Exception:
                continue
    manifest = HandoffManifest(
        hash_spec_version=hash_spec.hash_spec_version,
        pointers=ManifestPointers(
            plan_ref=plan_ref_artifact.path,
            freeze_record_ref=freeze_ref_artifact.path,
            hash_spec_ref=hash_spec_ref.path,
        ),
        repo_acquisition_spec=repo_specs,
        artifacts=manifest_artifacts,
        env_requirements_ref=env_snapshot_ref_artifact.path,
        handoff_digest="",
    )
    manifest = with_manifest_digest(manifest)
    _write_json(artifacts_root / "handoff_manifest.json", manifest.model_dump(), force=force)
    _write_json(store.path("handoff_manifest.json"), manifest.model_dump(), force=force)
    append_vnext_event(
        run_root,
        "planning",
        "HANDOFF_READY",
        run_id=run_id,
        plan_hash=plan_hash,
        payload={"handoff_manifest_ref": _artifact_ref("handoff_manifest.json")},
    )

    required_artifacts = [
        plan_ref,
        freeze_ref,
        config_ref,
        env_snapshot_ref,
        repo_context_ref,
        workspace_context_ref,
    ]
    optional_artifacts = [human_feedback_ref]
    if plan_migration_ref is not None:
        optional_artifacts.append(plan_migration_ref)

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

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from council_os.agents.roles import RoleConfig, load_role_configs
from council_os.agents.schemas import PlanPackage
from council_os.implementation.event_log import EventType, append_event, new_event
from council_os.implementation.handoff.accept import accept_handoff, accept_handoff_manifest
from council_os.implementation.manifest import ManifestInput, create_manifest
from council_os.implementation.patches import (
    PatchApplyError,
    PatchConflictError,
    PatchLimitError,
    apply_patchset,
    patchset_bytes_changed,
    patchset_operation_count,
    select_smallest_patchset,
)
from council_os.implementation.schemas import (
    IMPL_SCHEMA_VERSION_V1,
    IMPL_SCHEMA_VERSION_V2,
    IMPL_SCHEMA_VERSION_V21,
    ApplyRecordPayload,
    AssumptionRegistryPayload,
    AttestationBundlePayload,
    ChangeIntentPayload,
    ChangeRequestPayload,
    ConflictItem,
    ConflictReportPayload,
    ContextPackPayload,
    DecisionRecordPayload,
    DiffReportPayload,
    EvidenceIndexEntry,
    EvidenceIndexPayload,
    EvidencePointer,
    ExecutionProfileCatalogPayload,
    Expectation,
    ExpectationRegistryPayload,
    ExperimentManifestPayload,
    ExperimentResultsPayload,
    ImplementationArtifactEnvelope,
    IntegrationReportPayload,
    InterfaceContractsPayload,
    JobEvent,
    JobEventsPayload,
    JobResultPayload,
    JobSpecPayload,
    LeaseHolder,
    PatchChange,
    PatchsetLimits,
    PatchsetPayload,
    PlanningHandoffBundlePayload,
    PortfolioDecisionsPayload,
    ProgramGraphPayload,
    QualityCheck,
    QualityReportsPayload,
    ReleaseArtifact,
    ReleaseBundlePayload,
    RemoteOpAttempt,
    RemoteOpEvent,
    RemoteOpEventsPayload,
    RemoteOpResult,
    RemoteOpsManifestPayload,
    RemoteOpsResultsPayload,
    RemoteRunCacheEntry,
    RemoteRunCacheIndexPayload,
    ReplicationReportPayload,
    RepoContextPayload,
    RepoContextPayloadV2,
    RepoContextPayloadV21,
    RepoSnapshotPayload,
    ResearchContractsPayload,
    ReviewFinding,
    ReviewFindingsPayload,
    StageHandoffPayload,
    SurfaceLeaseEvent,
    SurfaceLeasesPayload,
    SurfaceRef,
    TestOutcome,
    TestResultsPayload,
    ToolProbeResultsPayload,
    ToolRegistryPayload,
    TraceReportPayload,
    VerificationPlanPayload,
    WorkGraphEventsPayload,
    WorkGraphBudget,
    WorkGraphTask,
    WorkGraphPayload,
    WorkLane,
    WorkPlanPayload,
    WorkPlanPayloadV2,
    WorkspaceContextPayload,
    WorkTask,
    WorkTaskV2,
    deterministic_json_dumps,
)
from council_os.handoff.hashing import artifact_hash, default_hash_spec, plan_content_hash
from council_os.handoff.schemas import HashSpec, RepoSnapshot
from council_os.implementation.secrets import scan_for_secrets
from council_os.implementation.store import ArtifactNotFoundError, ImplementationArtifactStore
from council_os.implementation.v2_1.execution.cache import IdempotencyCache
from council_os.implementation.v2_1.execution.job_runner import JobRunner
from council_os.implementation.v2_1.governance.assumptions import compile_assumption_registry
from council_os.implementation.v2_1.governance.leases import LeaseManager, SurfaceResolver
from council_os.implementation.v2_1.orchestration.program_graph import ProgramGraphCompiler
from council_os.implementation.v2_1.orchestration.scheduler import WorkScheduler
from council_os.implementation.v2_1.orchestration.verification_plan import VerificationPlanCompiler
from council_os.implementation.v2_1.orchestration.work_graph import WorkGraphBuilder
from council_os.implementation.v2_1.validators.gates import (
    validate_assumption_gate,
    validate_context_pack_adequacy,
    validate_expectations_v21,
    validate_lease_compliance_v21,
    validate_profile_compliance_v21,
    validate_side_effects_v21,
    validate_tool_registry_v21,
    validate_verification_plan_coverage_v21,
    validate_world_state_v21,
)
from council_os.implementation.validators import (
    collect_evidence_pointers,
    evaluate_expectation,
    validate_attestation_bundle,
    validate_evidence_pointers,
    validate_expectation_evidence_coverage,
    validate_expectation_registry,
    validate_forbidden_paths,
    validate_patchset_semantics,
    validate_plan_package_frozen,
    validate_profile_compliance,
    validate_risk_closure,
    validate_tool_registry,
)
from council_os.implementation.workspace import WorkspaceManager
from council_os.orchestrator.checkpoints import CheckpointState, write_checkpoint
from council_os.orchestrator.handoff.repo_snapshot import capture_repo_snapshot
from council_os.vnext.events import append_vnext_event

STAGES = [
    "intake",
    "work_planning",
    "generate",
    "patch_apply",
    "integrate_validate",
    "remote_validate",
    "validate",
    "review",
    "judge",
    "freeze",
]

DEPENDENCY_FILE_PATTERNS = [
    "requirements*.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "setup.cfg",
    "setup.py",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
    "composer.json",
    "composer.lock",
    "mix.exs",
    "mix.lock",
]


@dataclass
class LaneResult:
    lane_id: str
    patchsets: list[PatchsetPayload]
    test_results: TestResultsPayload
    quality_reports: QualityReportsPayload
    review_findings: ReviewFindingsPayload
    stage_handoffs: list[str]


@dataclass(frozen=True)
class ImplementationRunResult:
    run_id: UUID
    run_root: Path


@dataclass
class GuardrailPolicy:
    in_scope_paths: list[str] = field(default_factory=list)
    out_of_scope_paths: list[str] = field(default_factory=list)
    max_files_changed: int | None = None
    max_loc_changed: int | None = None
    max_dep_changes: int | None = None


@dataclass
class GuardrailCounters:
    files_changed: int = 0
    loc_changed: int = 0
    dep_changes: int = 0


class ImplementationEngine:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._tool_permissions: dict[str, set[str]] = {}
        self._tool_calls: list[dict[str, object]] = []
        self._tool_policy: dict[str, Any] = {}
        self._schema_version: str = IMPL_SCHEMA_VERSION_V1
        self._features: dict[str, bool] = {}

    def _git_code_version(self) -> str:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
            dirty = bool(
                subprocess.check_output(["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL).strip()
            )
            return f"{commit}:{'dirty' if dirty else 'clean'}"
        except Exception:
            return "unknown:dirty"

    def _event(
        self,
        run_root: Path,
        run_id: UUID,
        stage: str,
        event_type: EventType,
        refs: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
        actor: dict[str, str] | None = None,
    ) -> str:
        normalized_stage = self._normalize_stage(stage)
        event_payload = payload or {}
        if normalized_stage != stage:
            event_payload = dict(event_payload)
            event_payload.setdefault("stage_alias", stage)
        ev = new_event(
            run_id=run_id,
            stage=normalized_stage,  # type: ignore[arg-type]
            actor=actor or {"kind": "orchestrator", "role": "implementation_engine"},
            event_type=event_type,
            refs=refs,
            payload=event_payload,
        )
        append_event(run_root, ev)
        return str(ev.event_id)

    def _normalize_stage(self, stage: str) -> str:
        lower = stage.lower()
        for name in STAGES:
            if name in lower:
                return name
        if "patch" in lower or "complete" in lower:
            return "review"
        return "generate"

    def _schema_version_from_config(self, config: dict[str, Any]) -> str:
        configured = str(config.get("schemas_version", "")).strip()
        if not configured:
            raise ValueError("schemas_version must be configured")
        if configured not in {IMPL_SCHEMA_VERSION_V1, IMPL_SCHEMA_VERSION_V2, IMPL_SCHEMA_VERSION_V21}:
            raise ValueError(f"Unsupported schemas_version: {configured}")
        return configured

    def _feature_flags(self, config: dict[str, Any], schema_version: str) -> dict[str, bool]:
        raw = config.get("features", {})
        features = raw if isinstance(raw, dict) else {}
        v2_default = schema_version.startswith("2")
        v21_default = schema_version.startswith("2.1")

        def _flag(name: str) -> bool:
            if name in features:
                return bool(features[name])
            return v2_default

        flags = {
            "v2_1": _flag("v2_1") if "v2_1" in features else v21_default,
            "v2_profiles": _flag("v2_profiles"),
            "v2_tool_registry": _flag("v2_tool_registry"),
            "v2_expectations": _flag("v2_expectations"),
            "v2_remote_ops": _flag("v2_remote_ops"),
            "v2_research": _flag("v2_research"),
        }
        if flags.get("v2_1") and not schema_version.startswith("2.1"):
            raise ValueError("v2_1 feature flag requires schemas_version 2.1.0")
        if any(value for key, value in flags.items() if key != "v2_1") and not schema_version.startswith("2"):
            raise ValueError("v2 feature flags require schemas_version 2.x")
        return flags

    def _impl_artifacts_root(self, run_root: Path) -> Path:
        path = run_root / "implementation" / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _impl_jobs_root(self, run_root: Path) -> Path:
        path = run_root / "implementation" / "jobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_impl_json(self, run_root: Path, name: str, payload: dict[str, Any]) -> Path:
        path = self._impl_artifacts_root(run_root) / name
        encoded = deterministic_json_dumps(payload)
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == encoded:
                return path
            raise RuntimeError(f"Implementation artifact already exists with different content: {path}")
        path.write_text(encoded, encoding="utf-8")
        return path

    def _read_impl_json(self, run_root: Path, name: str) -> dict[str, Any] | None:
        path = self._impl_artifacts_root(run_root) / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_discrepancy_report(
        self,
        run_root: Path,
        *,
        expected: dict[str, Any],
        actual: dict[str, Any],
        reason: str,
    ) -> Path:
        payload = {
            "schema_version": "discrepancy_report.v1",
            "reason": reason,
            "expected": expected,
            "actual": actual,
        }
        return self._write_impl_json(run_root, "discrepancy_report.json", payload)

    def _impl_inputs_root(self, run_root: Path) -> Path:
        path = run_root / "implementation" / "inputs" / "planning"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _impl_job_dir(self, run_root: Path, name: str) -> Path:
        path = self._impl_jobs_root(run_root) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_job_json(self, path: Path, payload: dict[str, Any]) -> None:
        encoded = deterministic_json_dumps(payload)
        if path.exists():
            if path.read_text(encoding="utf-8") == encoded:
                return
            raise RuntimeError(f"Job artifact already exists with different content: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")

    def _append_job_run_record(self, run_root: Path, job_id: str, payload: dict[str, Any]) -> None:
        path = self._impl_job_dir(run_root, "run_records") / f"{job_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str))
            handle.write("\n")

    def _compute_tree_hash(self, repo_root: Path) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
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
            digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
            return digest

    def _ensure_input_json(self, root: Path, name: str, payload: dict[str, Any]) -> Path:
        path = root / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(deterministic_json_dumps(payload), encoding="utf-8")
        return path

    def _load_hash_spec(self, root: Path) -> HashSpec:
        path = root / "hash_spec.json"
        if path.exists():
            return HashSpec.model_validate_json(path.read_text(encoding="utf-8"))
        spec = default_hash_spec()
        self._ensure_input_json(root, "hash_spec.json", spec.model_dump())
        return spec

    def _normalize_resume_mode(self, value: object) -> str:
        mode = str(value or "RESUME_EXACT").strip().upper()
        if mode not in {"RESUME_EXACT", "REPAIR_WITHIN_PLAN", "REPLAN_REQUIRED"}:
            return "RESUME_EXACT"
        return mode

    def _normalize_guardrail_patterns(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _safe_id(self, value: str, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
        return cleaned or fallback

    def _extract_guardrails(self, plan: PlanPackage, config: dict[str, Any]) -> GuardrailPolicy | None:
        guard_cfg = config.get("guardrails", {}) if isinstance(config.get("guardrails", {}), dict) else {}
        scope = plan.scope
        constraints = plan.constraints
        caps = constraints.caps if constraints else None

        in_scope = self._normalize_guardrail_patterns(guard_cfg.get("in_scope_paths"))
        if not in_scope and scope is not None:
            in_scope = [p for p in scope.in_scope_paths if str(p).strip()]
        out_scope = self._normalize_guardrail_patterns(guard_cfg.get("out_of_scope_paths"))
        if not out_scope and scope is not None:
            out_scope = [p for p in scope.out_of_scope_paths if str(p).strip()]

        def _coerce_int(value: object | None) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except Exception:
                return None

        max_files = _coerce_int(guard_cfg.get("max_files_changed"))
        if max_files is None and caps is not None:
            max_files = caps.max_files_changed
        max_loc = _coerce_int(guard_cfg.get("max_loc_changed"))
        if max_loc is None and caps is not None:
            max_loc = caps.max_loc_changed
        max_dep = _coerce_int(guard_cfg.get("max_dep_changes"))
        if max_dep is None and caps is not None:
            max_dep = caps.max_dep_changes

        if not any([in_scope, out_scope, max_files, max_loc, max_dep]):
            return None
        return GuardrailPolicy(
            in_scope_paths=in_scope,
            out_of_scope_paths=out_scope,
            max_files_changed=max_files,
            max_loc_changed=max_loc,
            max_dep_changes=max_dep,
        )

    def _patchset_loc_changed(self, patchset: PatchsetPayload) -> int:
        total = 0
        for change in patchset.changes:
            before = change.before or ""
            after = change.after or ""
            before_lines = before.splitlines()
            after_lines = after.splitlines()
            if change.action == "add":
                total += len(after_lines)
            elif change.action == "delete":
                total += len(before_lines)
            else:
                total += max(len(before_lines), len(after_lines))
        return total

    def _patchset_dep_changes(self, patchset: PatchsetPayload) -> int:
        count = 0
        for change in patchset.changes:
            rel_path = change.path.replace("\\", "/").lstrip("/")
            if any(fnmatch(rel_path, pattern) for pattern in DEPENDENCY_FILE_PATTERNS):
                count += 1
        return count

    def _enforce_patchset_guardrails(
        self,
        *,
        guardrails: GuardrailPolicy,
        counters: GuardrailCounters,
        patchset: PatchsetPayload,
        run_root: Path,
        plan_hash: str | None,
        milestone_id: str | None,
    ) -> None:
        changed_paths = sorted(
            {change.path.replace("\\", "/").lstrip("/") for change in patchset.changes if change.path}
        )
        if guardrails.in_scope_paths:
            for path in changed_paths:
                if not any(fnmatch(path, pattern) for pattern in guardrails.in_scope_paths):
                    if plan_hash:
                        self._emit_vnext_change_request(
                            run_root,
                            plan_hash=plan_hash,
                            reason_code="SCOPE_CHANGE",
                            summary=f"Patchset {patchset.patchset_id} touches out-of-scope path {path}.",
                            blocked_task_ids=[],
                            blocked_job_ids=[],
                            blocked_check_ids=[],
                            milestones=[milestone_id or "MS-001"],
                        )
                    raise RuntimeError(f"Scope violation: {path} not in scope")
        if guardrails.out_of_scope_paths:
            for path in changed_paths:
                if any(fnmatch(path, pattern) for pattern in guardrails.out_of_scope_paths):
                    if plan_hash:
                        self._emit_vnext_change_request(
                            run_root,
                            plan_hash=plan_hash,
                            reason_code="SCOPE_CHANGE",
                            summary=f"Patchset {patchset.patchset_id} touches out-of-scope path {path}.",
                            blocked_task_ids=[],
                            blocked_job_ids=[],
                            blocked_check_ids=[],
                            milestones=[milestone_id or "MS-001"],
                        )
                    raise RuntimeError(f"Scope violation: {path} out of scope")

        counters.files_changed += len(changed_paths)
        counters.loc_changed += self._patchset_loc_changed(patchset)
        counters.dep_changes += self._patchset_dep_changes(patchset)

        if guardrails.max_files_changed is not None and counters.files_changed > guardrails.max_files_changed:
            if plan_hash:
                self._emit_vnext_change_request(
                    run_root,
                    plan_hash=plan_hash,
                    reason_code="SCOPE_CHANGE",
                    summary="File change budget exceeded; requires plan update.",
                    blocked_task_ids=[],
                    blocked_job_ids=[],
                    blocked_check_ids=[],
                    milestones=[milestone_id or "MS-001"],
                )
            raise RuntimeError("File change budget exceeded")
        if guardrails.max_loc_changed is not None and counters.loc_changed > guardrails.max_loc_changed:
            if plan_hash:
                self._emit_vnext_change_request(
                    run_root,
                    plan_hash=plan_hash,
                    reason_code="SCOPE_CHANGE",
                    summary="LOC change budget exceeded; requires plan update.",
                    blocked_task_ids=[],
                    blocked_job_ids=[],
                    blocked_check_ids=[],
                    milestones=[milestone_id or "MS-001"],
                )
            raise RuntimeError("LOC change budget exceeded")
        if guardrails.max_dep_changes is not None and counters.dep_changes > guardrails.max_dep_changes:
            if plan_hash:
                self._emit_vnext_change_request(
                    run_root,
                    plan_hash=plan_hash,
                    reason_code="SCOPE_CHANGE",
                    summary="Dependency change budget exceeded; requires plan update.",
                    blocked_task_ids=[],
                    blocked_job_ids=[],
                    blocked_check_ids=[],
                    milestones=[milestone_id or "MS-001"],
                )
            raise RuntimeError("Dependency change budget exceeded")

    def _build_vnext_expectation_registry(
        self,
        expectation_registry: ExpectationRegistryPayload,
        *,
        plan_hash: str,
        hash_spec_version: str,
        required_expectations: set[str],
        extra_expectations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        expectations: list[dict[str, Any]] = []
        for exp in sorted(expectation_registry.expectations, key=lambda item: item.expectation_id):
            severity = "MUST" if exp.expectation_id in required_expectations else "SHOULD"
            if exp.oracle:
                oracle = exp.oracle.model_dump(exclude_none=True)
                oracle.setdefault("type", exp.oracle.type)
            else:
                oracle = {"type": "exists"}
            expectations.append(
                {
                    "expectation_id": exp.expectation_id,
                    "severity": severity,
                    "oracle": oracle,
                    "required_env_class": "LOCAL",
                    "evidence_type": "job_result",
                }
            )
        if extra_expectations:
            for item in extra_expectations:
                if isinstance(item, dict):
                    expectations.append(item)
        expectations.sort(key=lambda item: item.get("expectation_id", ""))
        return {
            "schema_version": "expectation_registry.v1",
            "meta": {"plan_hash": plan_hash, "hash_spec_version": hash_spec_version},
            "expectations": expectations,
        }

    def _build_vnext_verification_plan(
        self,
        job_spec_map: dict[str, JobSpecPayload],
        *,
        milestone_id: str,
        plan_hash: str,
        hash_spec_version: str,
    ) -> dict[str, Any]:
        required_expectations: set[str] = set()
        for spec in job_spec_map.values():
            if spec.job_type == "research":
                continue
            required_expectations.update(spec.expected_expectation_ids)
        return {
            "schema_version": "verification_plan.v1",
            "meta": {
                "plan_hash": plan_hash,
                "compiler_version": "verify_compiler.v1",
                "hash_spec_version": hash_spec_version,
            },
            "milestone_id": milestone_id,
            "required_expectations": sorted(required_expectations),
            "jobs_to_run": sorted(job_spec_map.keys()),
        }

    def _build_vnext_work_graph(
        self,
        work_graph: WorkGraphPayload,
        *,
        plan_hash: str,
        compiler_inputs_hashes: list[str],
    ) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        for task in sorted(work_graph.tasks, key=lambda item: item.task_id):
            source_ids = sorted(set(task.program_check_ids + task.expectation_ids + task.assumption_ids))
            task_payload: dict[str, Any] = {
                "task_id": task.task_id,
                "title": f"Task {task.task_id}",
                "source_plan_ids": source_ids,
                "depends_on": list(task.deps),
                "target_paths": [],
                "acceptance_check_ids": sorted(task.expectation_ids),
                "inputs": [],
                "expected_outputs": [],
            }
            if task.budget is not None:
                task_payload["budgets"] = {
                    "max_runtime_sec": task.budget.max_runtime_sec,
                    "max_cost_usd": task.budget.max_cost_usd,
                    "max_calls": task.budget.max_calls,
                }
            tasks.append(task_payload)
        return {
            "schema_version": "work_graph.v1",
            "meta": {
                "source_plan_hash": plan_hash,
                "compiler_version": "work_compiler.v1",
                "compiler_inputs_hashes": compiler_inputs_hashes,
            },
            "tasks": tasks,
        }

    def _topo_sort_tasks(self, work_graph: dict[str, Any]) -> list[str]:
        tasks = work_graph.get("tasks", []) if isinstance(work_graph, dict) else []
        task_ids = {task.get("task_id") for task in tasks if isinstance(task, dict)}
        deps_map: dict[str, set[str]] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id")
            if not task_id:
                continue
            deps = {dep for dep in task.get("depends_on", []) if dep in task_ids}
            deps_map[str(task_id)] = deps
        ordered: list[str] = []
        ready = sorted([tid for tid, deps in deps_map.items() if not deps])
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for tid, deps in deps_map.items():
                if current in deps:
                    deps.remove(current)
                    if not deps and tid not in ordered and tid not in ready:
                        ready.append(tid)
                        ready.sort()
        if len(ordered) != len(deps_map):
            return sorted(deps_map.keys())
        return ordered

    def _build_vnext_execution_plan(
        self,
        work_graph: dict[str, Any],
        job_map: dict[str, list[str]],
    ) -> dict[str, Any]:
        runlist: list[dict[str, Any]] = []
        for idx, task_id in enumerate(self._topo_sort_tasks(work_graph), start=1):
            runlist.append({"sequence": idx, "task_id": task_id, "job_ids": sorted(job_map.get(task_id, []))})
        return {"schema_version": "execution_plan.v1", "task_runlist": runlist}

    def _build_vnext_job_specs(
        self,
        job_spec_map: dict[str, JobSpecPayload],
        work_graph: WorkGraphPayload,
        *,
        repo_snapshot: RepoSnapshot | None,
        env_snapshot_hash: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        job_to_task: dict[str, str] = {}
        for task in work_graph.tasks:
            for job_id in task.job_specs:
                job_to_task[str(job_id)] = task.task_id
        job_specs: list[dict[str, Any]] = []
        task_job_map: dict[str, list[str]] = {}
        repo_ref = None
        if repo_snapshot and repo_snapshot.tree_hash:
            repo_ref = f"repo@{repo_snapshot.tree_hash}"
        for job_id, spec in sorted(job_spec_map.items(), key=lambda item: item[0]):
            inputs: list[dict[str, Any]] = []
            for inp in spec.inputs:
                if inp.artifact_ref:
                    inputs.append({"type": "artifact", "ref": inp.artifact_ref})
                if inp.snapshot_ref:
                    inputs.append({"type": "snapshot", "ref": inp.snapshot_ref})
            if repo_ref:
                inputs.append({"type": "repo", "ref": repo_ref})
            budgets: dict[str, Any] = {}
            if spec.budget is not None:
                if spec.budget.max_runtime_sec is not None:
                    budgets["max_runtime_sec"] = spec.budget.max_runtime_sec
                if spec.budget.max_calls is not None:
                    budgets["max_retries"] = spec.budget.max_calls
            task_id = job_to_task.get(job_id)
            if task_id:
                task_job_map.setdefault(task_id, []).append(job_id)
            payload = {
                "schema_version": "job_spec.v1",
                "job_id": job_id,
                "job_type": spec.job_type,
                "task_id": task_id,
                "idempotency_key": spec.idempotency_key,
                "inputs": inputs,
                "budgets": budgets or None,
                "expectation_ids": spec.expected_expectation_ids,
                "env_profile_ref": f"profile:{spec.profile_id}",
                "env_snapshot_hash": env_snapshot_hash,
            }
            job_specs.append(payload)
        return job_specs, task_job_map

    def _write_vnext_compiled_artifacts(
        self,
        run_root: Path,
        *,
        compiler_inputs: dict[str, Any],
        expectation_registry: dict[str, Any],
        verification_plan: dict[str, Any],
        work_graph: dict[str, Any],
        execution_plan: dict[str, Any],
        resume_mode: str,
    ) -> dict[str, Any]:
        existing_inputs = self._read_impl_json(run_root, "compiler_inputs.json")
        if existing_inputs is not None and existing_inputs != compiler_inputs:
            self._write_discrepancy_report(
                run_root,
                expected=existing_inputs,
                actual=compiler_inputs,
                reason="compiler_inputs_mismatch",
            )
            if resume_mode == "RESUME_EXACT":
                raise RuntimeError("Compiler inputs mismatch during resume")
            if resume_mode == "REPAIR_WITHIN_PLAN":
                reused = {
                    "expectation_registry": self._read_impl_json(run_root, "expectation_registry.json"),
                    "verification_plan": self._read_impl_json(run_root, "verification_plan.json"),
                    "work_graph": self._read_impl_json(run_root, "work_graph.json"),
                    "execution_plan": self._read_impl_json(run_root, "execution_plan.json"),
                }
                if all(reused.values()):
                    return reused
        if existing_inputs is None:
            self._write_impl_json(run_root, "compiler_inputs.json", compiler_inputs)
        if existing_inputs is not None and existing_inputs == compiler_inputs:
            # Reuse existing compiled artifacts if present.
            reused = {
                "expectation_registry": self._read_impl_json(run_root, "expectation_registry.json"),
                "verification_plan": self._read_impl_json(run_root, "verification_plan.json"),
                "work_graph": self._read_impl_json(run_root, "work_graph.json"),
                "execution_plan": self._read_impl_json(run_root, "execution_plan.json"),
            }
            if all(reused.values()):
                return reused
        self._write_impl_json(run_root, "expectation_registry.json", expectation_registry)
        self._write_impl_json(run_root, "verification_plan.json", verification_plan)
        self._write_impl_json(run_root, "work_graph.json", work_graph)
        self._write_impl_json(run_root, "execution_plan.json", execution_plan)
        return {
            "expectation_registry": expectation_registry,
            "verification_plan": verification_plan,
            "work_graph": work_graph,
            "execution_plan": execution_plan,
        }

    def _write_vnext_job_specs(self, run_root: Path, job_specs: list[dict[str, Any]]) -> None:
        specs_root = self._impl_job_dir(run_root, "specs")
        for spec in job_specs:
            job_id = spec.get("job_id")
            if not job_id:
                continue
            self._write_job_json(specs_root / f"{job_id}.json", spec)

    def _write_vnext_job_results(
        self,
        run_root: Path,
        job_results: list[JobResultPayload],
        job_spec_map: dict[str, JobSpecPayload],
    ) -> list[dict[str, Any]]:
        results_root = self._impl_job_dir(run_root, "results")
        vnext_results: list[dict[str, Any]] = []
        for result in job_results:
            status = "SUCCEEDED" if result.status in {"success", "cache_hit"} else "FAILED"
            outputs = [{"type": "artifact", "ref": output.artifact_ref} for output in result.outputs]
            spec = job_spec_map.get(result.job_id)
            satisfies = spec.expected_expectation_ids if spec and status == "SUCCEEDED" else []
            payload = {
                "schema_version": "job_result.v1",
                "job_id": result.job_id,
                "status": status,
                "outputs": outputs,
                "logs_refs": [],
                "metrics_refs": [],
                "satisfies_expectations": satisfies,
            }
            self._write_job_json(results_root / f"{result.job_id}.json", payload)
            vnext_results.append(payload)
        return vnext_results

    def _build_vnext_evidence_index(
        self,
        expectation_registry: dict[str, Any],
        *,
        job_results: list[dict[str, Any]],
        job_spec_map: dict[str, JobSpecPayload],
        milestone_id: str,
        plan_hash: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        expectation_status: dict[str, str] = {}
        evidence_refs: dict[str, list[str]] = {}
        for exp in expectation_registry.get("expectations", []):
            exp_id = exp.get("expectation_id")
            if exp_id:
                expectation_status[exp_id] = "PENDING"
                evidence_refs[exp_id] = []
        for result in job_results:
            job_id = result.get("job_id")
            status = result.get("status")
            satisfies = result.get("satisfies_expectations", []) or []
            if status == "SUCCEEDED":
                for exp_id in satisfies:
                    if exp_id in expectation_status:
                        expectation_status[exp_id] = "PASS"
                        evidence_refs[exp_id].append(f"jobs/results/{job_id}.json")
            else:
                spec = job_spec_map.get(job_id) if job_id else None
                for exp_id in (spec.expected_expectation_ids if spec else []):
                    if exp_id in expectation_status and expectation_status[exp_id] != "PASS":
                        expectation_status[exp_id] = "FAIL"
        entries: list[dict[str, Any]] = []
        for exp_id in sorted(expectation_status.keys()):
            entries.append(
                {
                    "expectation_id": exp_id,
                    "status": expectation_status[exp_id],
                    "evidence_refs": [
                        {"type": "job_result", "ref": ref} for ref in evidence_refs.get(exp_id, [])
                    ],
                }
            )
        payload = {
            "schema_version": "evidence_index.v1",
            "meta": {"plan_hash": plan_hash, "milestone_id": milestone_id},
            "expectations": entries,
        }
        return payload, expectation_status

    def _build_vnext_decision_record(
        self,
        expectation_registry: dict[str, Any],
        expectation_status: dict[str, str],
        *,
        evidence_index: dict[str, Any],
        milestone_id: str,
        plan_hash: str,
    ) -> tuple[dict[str, Any], list[str]]:
        must_failures: list[str] = []
        per_expectation: list[dict[str, Any]] = []
        evidence_map: dict[str, list[dict[str, Any]]] = {}
        for entry in evidence_index.get("expectations", []):
            if not isinstance(entry, dict):
                continue
            exp_id = entry.get("expectation_id")
            if exp_id:
                evidence_map[exp_id] = entry.get("evidence_refs", []) or []
        for exp in expectation_registry.get("expectations", []):
            exp_id = exp.get("expectation_id")
            if not exp_id:
                continue
            status = expectation_status.get(exp_id, "PENDING")
            per_expectation.append(
                {
                    "expectation_id": exp_id,
                    "status": status,
                    "evidence_refs": [ref.get("ref") for ref in evidence_map.get(exp_id, []) if isinstance(ref, dict)],
                }
            )
            if exp.get("severity") == "MUST" and status != "PASS":
                must_failures.append(exp_id)
        decision = "PASS" if not must_failures else "FAIL"
        payload = {
            "schema_version": "decision_record.v1",
            "meta": {"plan_hash": plan_hash, "milestone_id": milestone_id, "rubric_version": "1"},
            "decision": decision,
            "per_expectation": per_expectation,
        }
        return payload, must_failures

    def _build_baseline_report(
        self,
        *,
        expectation_registry: dict[str, Any],
        evidence_index: dict[str, Any],
        expectation_status: dict[str, str],
        plan_hash: str,
        milestone_id: str,
        run_root: Path,
    ) -> dict[str, Any] | None:
        entries: list[dict[str, Any]] = []
        evidence_map: dict[str, list[dict[str, Any]]] = {}
        for entry in evidence_index.get("expectations", []):
            if not isinstance(entry, dict):
                continue
            exp_id = entry.get("expectation_id")
            if exp_id:
                evidence_map[exp_id] = entry.get("evidence_refs", []) or []
        planning_root = run_root / "implementation" / "inputs" / "planning"
        for exp in expectation_registry.get("expectations", []):
            if not isinstance(exp, dict):
                continue
            oracle = exp.get("oracle") or {}
            if not isinstance(oracle, dict) or oracle.get("type") != "differential_baseline":
                continue
            exp_id = exp.get("expectation_id", "")
            baseline_ref = oracle.get("baseline_ref")
            baseline_present = False
            if isinstance(baseline_ref, str) and baseline_ref:
                candidate = (planning_root / baseline_ref).resolve()
                if candidate.exists():
                    baseline_present = True
                else:
                    alt = (run_root / baseline_ref).resolve()
                    baseline_present = alt.exists()
            entries.append(
                {
                    "expectation_id": exp_id,
                    "status": expectation_status.get(exp_id, "PENDING"),
                    "baseline_ref": baseline_ref,
                    "baseline_present": baseline_present,
                    "evidence_refs": evidence_map.get(exp_id, []),
                }
            )
        if not entries:
            return None
        return {
            "schema_version": "baseline_report.v1",
            "meta": {"plan_hash": plan_hash, "milestone_id": milestone_id},
            "entries": entries,
        }

    def _emit_vnext_change_request(
        self,
        run_root: Path,
        *,
        plan_hash: str,
        reason_code: str,
        summary: str,
        blocked_task_ids: list[str],
        blocked_job_ids: list[str],
        blocked_check_ids: list[str],
        milestones: list[str],
    ) -> None:
        payload = {
            "schema_version": "change_request.v1",
            "meta": {"impl_run_id": run_root.name, "source_plan_hash": plan_hash},
            "reason": {"code": reason_code, "summary": summary},
            "blocked_entities": {
                "task_ids": blocked_task_ids,
                "job_ids": blocked_job_ids,
                "check_ids": blocked_check_ids,
            },
            "evidence_refs": [],
            "proposed_plan_edits_ref": None,
            "impact": {"milestones_affected": milestones, "checks_affected": blocked_check_ids},
        }
        self._write_impl_json(run_root, "change_request_v1.json", payload)
        append_vnext_event(
            run_root,
            "implementation",
            "CHANGE_REQUESTED",
            run_id=run_root.name,
            plan_hash=plan_hash,
            payload={"reason_code": reason_code},
        )

    def _load_config_payload(self, config: dict[str, Any], config_path: Path, key: str) -> dict[str, Any] | None:
        raw = config.get("v2_inputs", {}) if isinstance(config.get("v2_inputs", {}), dict) else {}
        value = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"_list": value}
        if isinstance(value, str) and value:
            path = Path(value)
            if not path.is_absolute():
                path = (config_path.parent / path).resolve()
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _load_change_requests(
        self,
        config: dict[str, Any],
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        stage: str,
        decision_role: str,
    ) -> tuple[set[str], list[str], dict[str, str]]:
        approved: set[str] = set()
        artifact_ids: list[str] = []
        decision_records: dict[str, str] = {}
        raw = config.get("change_requests", [])
        if not isinstance(raw, list):
            return approved, artifact_ids, decision_records
        for item in raw:
            if not isinstance(item, dict):
                continue
            payload = ChangeRequestPayload.model_validate(item)
            artifact_id = f"change_request_{payload.change_id}_v1"
            self._write_artifact(
                store,
                workspace,
                run_id,
                "change_request",
                artifact_id,
                payload.model_dump(),
                parents=[],
                stage=stage,
                role="engine",
            )
            artifact_ids.append(artifact_id)
            if payload.status == "approved":
                approved.add(payload.change_id)
                for step in ["hitl", "change_request"]:
                    self._event(
                        workspace.run_root,
                        run_id,
                        stage=stage,
                        event_type="decision",
                        payload={"change_request_id": payload.change_id, "escalation_step": step},
                    )
                decision = DecisionRecordPayload(
                    decision_id=f"DEC-CR-{payload.change_id}",
                    summary="ChangeRequest approved",
                    reasons=[payload.rationale] + [f"approval:{a}" for a in payload.approvals],
                    what_would_change=["ChangeRequest rejected or not approved"],
                    evidence=[EvidencePointer(artifact_ref=artifact_id, json_pointer="/")],
                )
                decision_id = f"decision_record_{payload.change_id}_v1"
                self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "decision_record",
                    decision_id,
                    decision.model_dump(),
                    parents=[artifact_id],
                    stage=stage,
                    role=decision_role,
                )
                artifact_ids.append(decision_id)
                decision_records[payload.change_id] = decision_id
        return approved, artifact_ids, decision_records

    def _load_tool_permissions(self, config: dict[str, Any]) -> None:
        self._tool_policy = config.get("tool_policy", {}) if isinstance(config.get("tool_policy", {}), dict) else {}
        raw = config.get("tool_permissions", {})
        permissions: dict[str, set[str]] = {}
        if isinstance(raw, dict):
            for stage, caps in raw.items():
                if isinstance(caps, list):
                    permissions[str(stage)] = {str(cap) for cap in caps}
        self._tool_permissions = permissions
        calls = config.get("tool_calls", [])
        if isinstance(calls, list):
            self._tool_calls = [call for call in calls if isinstance(call, dict)]

    def _tool_policy_for_stage(self, stage: str) -> dict[str, Any]:
        if not isinstance(self._tool_policy, dict):
            return {}
        stage_policies = self._tool_policy.get("stages", {})
        if isinstance(stage_policies, dict) and stage in stage_policies:
            entry = stage_policies.get(stage)
            return entry if isinstance(entry, dict) else {}
        return {}

    def _check_tool_scopes(self, stage_policy: dict[str, Any], call: dict[str, object]) -> None:
        def _match_any(path: str, patterns: list[str]) -> bool:
            from fnmatch import fnmatch

            return any(fnmatch(path, pattern) for pattern in patterns)

        fs_read = call.get("fs_read_paths") or call.get("fs_paths") or []
        fs_write = call.get("fs_write_paths") or []
        if isinstance(fs_read, str):
            fs_read = [fs_read]
        if isinstance(fs_write, str):
            fs_write = [fs_write]
        read_scopes = stage_policy.get("fs_read_scopes", [])
        write_scopes = stage_policy.get("fs_write_scopes", [])
        if read_scopes:
            for path in fs_read:
                if not _match_any(str(path), list(read_scopes)):
                    raise RuntimeError("Tool call read path outside allowed scopes")
        if write_scopes:
            for path in fs_write:
                if not _match_any(str(path), list(write_scopes)):
                    raise RuntimeError("Tool call write path outside allowed scopes")

        network_hosts = call.get("network_hosts") or []
        if isinstance(network_hosts, str):
            network_hosts = [network_hosts]
        allowed_hosts = stage_policy.get("network_allowlist", [])
        if allowed_hosts:
            for host in network_hosts:
                if str(host) not in allowed_hosts:
                    raise RuntimeError("Tool call network host outside allowlist")

        pkg = call.get("package_manager")
        allowed_pkgs = stage_policy.get("package_managers", [])
        if allowed_pkgs and pkg and str(pkg) not in allowed_pkgs:
            raise RuntimeError("Tool call package manager not allowed")

    def _execute_tool_calls(
        self,
        run_root: Path,
        run_id: UUID,
        stage: str,
        repo_context: RepoContextPayload | RepoContextPayloadV2 | None = None,
    ) -> None:
        stage_policy = self._tool_policy_for_stage(stage)
        for call in self._tool_calls:
            if str(call.get("stage")) != stage:
                continue
            tool_name = str(call.get("tool_name", ""))
            capabilities = {str(cap) for cap in call.get("capabilities", []) if isinstance(cap, str)}
            approved = bool(call.get("approved", False))
            allowed = set(self._tool_permissions.get(stage, set()))
            if not allowed:
                allow_from_policy = stage_policy.get("allow", [])
                if isinstance(allow_from_policy, list):
                    allowed = {str(cap) for cap in allow_from_policy}
            deny_from_policy = stage_policy.get("deny", [])
            operation = str(call.get("operation") or "")
            if isinstance(deny_from_policy, list) and (
                any(cap in deny_from_policy for cap in capabilities) or operation in deny_from_policy
            ):
                raise RuntimeError("Tool invocation denied by policy")
            self._event(
                run_root,
                run_id,
                stage=stage,
                event_type="tool_call",
                payload={"tool_name": tool_name, "capabilities": sorted(capabilities), "approved": approved},
                actor={"kind": "tool", "role": tool_name or "unknown"},
            )
            if not capabilities.issubset(allowed):
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="tool_result",
                    payload={"tool_name": tool_name, "status": "blocked"},
                    actor={"kind": "tool", "role": tool_name or "unknown"},
                )
                raise RuntimeError("Tool invocation outside permission matrix")
            hitl_triggers = stage_policy.get("hitl_triggers", [])
            high_risk_ops = stage_policy.get("high_risk_ops", [])
            if (any(cap in hitl_triggers for cap in capabilities) or operation in high_risk_ops) and not approved:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="tool_result",
                    payload={"tool_name": tool_name, "status": "blocked_hitl"},
                    actor={"kind": "tool", "role": tool_name or "unknown"},
                )
                raise RuntimeError("Tool invocation requires approval")
            if stage_policy:
                self._check_tool_scopes(stage_policy, call)
            if repo_context:
                from fnmatch import fnmatch

                fs_write = call.get("fs_write_paths") or []
                if isinstance(fs_write, str):
                    fs_write = [fs_write]
                for path in fs_write:
                    rel_path = str(path).replace("\\", "/").lstrip("/")
                    if any(fnmatch(rel_path, pattern) for pattern in repo_context.forbidden_paths):
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="tool_result",
                            payload={"tool_name": tool_name, "status": "blocked_forbidden"},
                            actor={"kind": "tool", "role": tool_name or "unknown"},
                        )
                        raise RuntimeError("Tool invocation writes to forbidden path")
                    if any(fnmatch(rel_path, pattern) for pattern in repo_context.protected_paths) and not approved:
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="tool_result",
                            payload={"tool_name": tool_name, "status": "blocked_hitl"},
                            actor={"kind": "tool", "role": tool_name or "unknown"},
                        )
                        raise RuntimeError("Tool invocation writes to protected path without approval")
            self._event(
                run_root,
                run_id,
                stage=stage,
                event_type="tool_result",
                payload={"tool_name": tool_name, "status": "ok"},
                actor={"kind": "tool", "role": tool_name or "unknown"},
            )

    def _enforce_role_separation(self, roles: dict[str, RoleConfig]) -> None:
        def provider_family(role: RoleConfig) -> str:
            provider = role.model_provider
            model = role.model_name
            if provider == "openrouter" and "/" in model:
                return model.split("/", 1)[0]
            return provider

        writers = [r for name, r in roles.items() if name.startswith("writer")]
        reviewers = [r for name, r in roles.items() if name.startswith("reviewer")]
        judges = [r for name, r in roles.items() if name.startswith("judge")]
        if not writers or not reviewers or not judges:
            raise ValueError("writer/reviewer/judge roles are required for implementation runs")
        writer_families = {provider_family(r) for r in writers}
        reviewer_families = {provider_family(r) for r in reviewers}
        judge_families = {provider_family(r) for r in judges}
        if writer_families & reviewer_families:
            raise ValueError("writer and reviewer providers must be distinct")
        if writer_families & judge_families:
            raise ValueError("writer and judge providers must be distinct")
        if reviewer_families & judge_families:
            raise ValueError("reviewer and judge providers must be distinct")

    def _role_sets(self, roles: dict[str, RoleConfig]) -> dict[str, set[str]]:
        return {
            "writer": {name for name in roles if name.startswith("writer")},
            "reviewer": {name for name in roles if name.startswith("reviewer")},
            "judge": {name for name in roles if name.startswith("judge")},
        }

    def _role_assignments(self, roles: dict[str, RoleConfig]) -> dict[str, str]:
        role_sets = self._role_sets(roles)

        def _pick(kind: str) -> str:
            names = sorted(role_sets[kind])
            if not names:
                raise ValueError(f"{kind} role required for implementation runs")
            return names[0]

        return {
            "writer": _pick("writer"),
            "reviewer": _pick("reviewer"),
            "judge": _pick("judge"),
        }

    def _model_portfolio(self, roles: dict[str, RoleConfig]) -> dict[str, dict[str, object]]:
        portfolio: dict[str, dict[str, object]] = {}
        for name, role in roles.items():
            portfolio[name] = {
                "provider": role.model_provider,
                "model": role.model_name,
                "temperature": role.temperature,
                "max_tokens": role.max_tokens,
                "prompt_version_hash": role.prompt_version_hash,
            }
        return portfolio

    def _load_plan_package(self, plan_path: Path) -> PlanPackage:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "plan_package" in data:
            data = data["plan_package"]
        plan = PlanPackage.model_validate(data)
        validate_plan_package_frozen(plan)
        return plan

    def _build_work_plan(self, plan: PlanPackage, patchset_limits: PatchsetLimits) -> WorkPlanPayload:
        lanes = [
            WorkLane(lane_id="L1", name="Lane 1", description="Primary lane"),
            WorkLane(lane_id="L2", name="Lane 2", description="Secondary lane"),
        ]
        tasks: list[WorkTask] = []
        for idx, req in enumerate(plan.requirements, start=1):
            lane = lanes[(idx - 1) % len(lanes)]
            maps_to_acceptance = [t.id for t in plan.acceptance_tests if req.id in t.maps_to_requirements]
            tasks.append(
                WorkTask(
                    task_id=f"T{idx}",
                    title=f"Implement {req.id}",
                    lane_id=lane.lane_id,
                    depends_on=[f"T{idx-1}"] if idx > 1 else [],
                    maps_to_requirements=[req.id],
                    maps_to_acceptance_tests=maps_to_acceptance,
                )
            )
        return WorkPlanPayload(
            lanes=lanes,
            tasks=tasks,
            patchset_limits=patchset_limits,
            generated_at=datetime.now(UTC),
        )

    def _build_work_plan_v2(
        self,
        plan: PlanPackage,
        patchset_limits: PatchsetLimits,
        repo_context: RepoContextPayloadV2,
        expectation_registry: ExpectationRegistryPayload,
        profile_catalog: ExecutionProfileCatalogPayload,
        remote_ops_manifest: RemoteOpsManifestPayload | None,
    ) -> WorkPlanPayloadV2:
        lanes = [
            WorkLane(lane_id="L1", name="Lane 1", description="Primary lane"),
            WorkLane(lane_id="L2", name="Lane 2", description="Secondary lane"),
        ]
        req_expectations = {
            exp.source.id: exp.expectation_id
            for exp in expectation_registry.expectations
            if exp.expectation_id.startswith("EXP-PLAN-")
        }
        test_expectations = {
            exp.source.id: exp.expectation_id
            for exp in expectation_registry.expectations
            if exp.expectation_id.startswith("EXP-AT-")
        }
        default_profiles = {}
        if repo_context.execution_profiles:
            default_profiles = repo_context.execution_profiles.default_profile_id_by_stage
        default_profile = None
        if default_profiles:
            for key in ("Generate", "generate", "ValidateLocal", "Validate", "validate"):
                if key in default_profiles:
                    default_profile = default_profiles[key]
                    break
        if not default_profile and repo_context.execution_profiles:
            allowed = repo_context.execution_profiles.allowed_profile_ids
            if allowed:
                default_profile = sorted(allowed)[0]
        if not default_profile and profile_catalog.profiles:
            default_profile = sorted([p.profile_id for p in profile_catalog.profiles])[0]
        if not default_profile:
            raise ValueError("No execution profiles available for work plan")

        tasks: list[WorkTaskV2] = []
        for idx, req in enumerate(plan.requirements, start=1):
            lane = lanes[(idx - 1) % len(lanes)]
            maps_to_acceptance = [t.id for t in plan.acceptance_tests if req.id in t.maps_to_requirements]
            satisfies = []
            req_expectation_id = req_expectations.get(req.id)
            if req_expectation_id:
                satisfies.append(req_expectation_id)
            verifies = [test_expectations[test_id] for test_id in maps_to_acceptance if test_id in test_expectations]
            remote_ops: list[str] = []
            if remote_ops_manifest:
                expectation_ids = set(satisfies + verifies)
                for op in remote_ops_manifest.ops:
                    if expectation_ids.intersection(op.expected):
                        remote_ops.append(op.op_id)
            tasks.append(
                WorkTaskV2(
                    task_id=f"T{idx}",
                    title=f"Implement {req.id}",
                    lane_id=lane.lane_id,
                    depends_on=[f"T{idx-1}"] if idx > 1 else [],
                    maps_to_requirements=[req.id],
                    maps_to_acceptance_tests=maps_to_acceptance,
                    profile_id=default_profile,
                    satisfies_expectations=satisfies,
                    verifies_expectations=verifies,
                    remote_ops=remote_ops,
                )
            )
        return WorkPlanPayloadV2(
            lanes=lanes,
            tasks=tasks,
            patchset_limits=patchset_limits,
            generated_at=datetime.now(UTC),
        )

    def _default_profile_catalog(self, schema_version: str | None = None) -> ExecutionProfileCatalogPayload:
        digest = hashlib.sha256(self._git_code_version().encode("utf-8")).hexdigest()
        schema_version = schema_version or IMPL_SCHEMA_VERSION_V2
        payload = {
            "schema_version": schema_version,
            "catalog_id": "legacy_v1",
            "profiles": [
                {
                    "profile_id": "legacy_v1",
                    "profile_version": "1.0.0",
                    "runtime": {"lang": "python", "version": "3.12"},
                    "base_image": {"name": "legacy/runtime", "digest": f"sha256:{digest}"},
                    "deps": {"required": [], "allowed": [], "forbidden": []},
                    "hardware": {"gpu": False},
                    "network": {"default": "deny", "allowlist_domains": [], "allowlist_ports": [443]},
                    "package_manager": {"pip_install": "deny", "conda_install": "deny"},
                    "tool_adapters": [],
                    "env": {"vars": {"PYTHONHASHSEED": "0"}},
                }
            ],
        }
        return ExecutionProfileCatalogPayload.model_validate(payload)

    def _compile_expectation_registry_from_plan(
        self,
        plan: PlanPackage,
        schema_version: str | None = None,
    ) -> ExpectationRegistryPayload:
        expectations: list[dict[str, Any]] = []
        for req in plan.requirements:
            expectations.append(
                {
                    "expectation_id": f"EXP-PLAN-{req.id}",
                    "level": "plan",
                    "source": {"type": "R", "id": req.id},
                    "subject": {"kind": "requirement", "artifact_type": None, "name": req.id},
                    "oracle": {"type": "exists"},
                    "tolerance": None,
                    "inputs": [],
                    "parents": [],
                    "children": [],
                    "must_exist_before_remote_ops": True,
                }
            )
        for test in plan.acceptance_tests:
            parents = [f"EXP-PLAN-{req_id}" for req_id in test.maps_to_requirements]
            expectations.append(
                {
                    "expectation_id": f"EXP-AT-{test.id}",
                    "level": "test",
                    "source": {"type": "AT", "id": test.id},
                    "subject": {"kind": "acceptance_test", "artifact_type": None, "name": test.id},
                    "oracle": {"type": "exists"},
                    "tolerance": None,
                    "inputs": [],
                    "parents": parents,
                    "children": [],
                    "must_exist_before_remote_ops": True,
                }
            )
        for option in plan.architecture.options:
            expectations.append(
                {
                    "expectation_id": f"EXP-ARCH-{option.id}",
                    "level": "plan",
                    "source": {"type": "O", "id": option.id},
                    "subject": {"kind": "architecture_option", "artifact_type": None, "name": option.id},
                    "oracle": {"type": "exists"},
                    "tolerance": None,
                    "inputs": [],
                    "parents": [],
                    "children": [],
                    "must_exist_before_remote_ops": True,
                }
            )
        for risk in plan.risk_register:
            expectations.append(
                {
                    "expectation_id": f"EXP-RISK-{risk.id}",
                    "level": "plan",
                    "source": {"type": "K", "id": risk.id},
                    "subject": {"kind": "risk", "artifact_type": None, "name": risk.id},
                    "oracle": {"type": "exists"},
                    "tolerance": None,
                    "inputs": [],
                    "parents": [],
                    "children": [],
                    "must_exist_before_remote_ops": True,
                }
            )
        children_map: dict[str, list[str]] = {}
        for exp in expectations:
            for parent in exp["parents"]:
                children_map.setdefault(parent, []).append(exp["expectation_id"])
        for exp in expectations:
            exp["children"] = sorted(children_map.get(exp["expectation_id"], []))
        payload = {
            "schema_version": schema_version or IMPL_SCHEMA_VERSION_V2,
            "registry_id": "default",
            "expectations": expectations,
        }
        return ExpectationRegistryPayload.model_validate(payload)

    def _hash_payload(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(deterministic_json_dumps(payload).encode()).hexdigest()

    def _deterministic_metric(self, experiment_id: str, metric: str, seed: int) -> float:
        key = f"{experiment_id}:{metric}:{seed}".encode()
        digest = hashlib.sha256(key).hexdigest()
        return (int(digest[:8], 16) % 10000) / 100.0

    def _simulate_experiment_results(
        self,
        manifest: ExperimentManifestPayload,
        attestation_ref: str,
    ) -> ExperimentResultsPayload:
        results: list[dict[str, Any]] = []
        metrics = list(manifest.metrics)
        for seed in manifest.seeds:
            metric_values = {
                metric: self._deterministic_metric(manifest.experiment_id, metric, seed)
                for metric in metrics
            }
            results.append({"seed": seed, "metrics": metric_values})
        aggregate: dict[str, float] = {}
        for metric in metrics:
            values = [item["metrics"].get(metric, 0.0) for item in results]
            if values:
                mean = sum(values) / len(values)
                variance = sum((value - mean) ** 2 for value in values) / len(values)
                aggregate[f"{metric}_mean"] = mean
                aggregate[f"{metric}_std"] = variance ** 0.5
            else:
                aggregate[f"{metric}_mean"] = 0.0
                aggregate[f"{metric}_std"] = 0.0
        payload = ExperimentResultsPayload(
            schema_version=IMPL_SCHEMA_VERSION_V2,
            experiment_id=manifest.experiment_id,
            results=results,
            aggregate_stats=aggregate,
            artifacts=[],
            attestation_ref=attestation_ref,
        )
        return payload

    def _run_experiments(
        self,
        manifest: ExperimentManifestPayload,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        stage: str,
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        profile_catalog: ExecutionProfileCatalogPayload | None,
    ) -> ExperimentResultsPayload:
        attestation_ref = self._write_attestation(
            store,
            workspace,
            run_id,
            stage=stage,
            profile_id=manifest.profile_id,
            repo_context=repo_context,
            profile_catalog=profile_catalog,
            tool_id=None,
            inputs=manifest.dataset_snapshot_ids,
            spec_versions={"experiment_manifest": manifest.schema_version},
        )
        return self._simulate_experiment_results(manifest, attestation_ref)

    def _run_replication(
        self,
        manifest: ExperimentManifestPayload,
        experiment_results: ExperimentResultsPayload,
    ) -> ReplicationReportPayload:
        comparison: dict[str, Any] = {"metrics": []}
        status = "pass"
        tolerance = 1e-6
        expected = {
            result.seed: result.metrics
            for result in experiment_results.results
        }
        for seed in manifest.seeds:
            metrics = {
                metric: self._deterministic_metric(manifest.experiment_id, metric, seed)
                for metric in manifest.metrics
            }
            original = expected.get(seed, {})
            for metric, value in metrics.items():
                original_value = original.get(metric)
                diff = None if original_value is None else abs(value - float(original_value))
                comparison["metrics"].append(
                    {
                        "seed": seed,
                        "metric": metric,
                        "original": original_value,
                        "replicated": value,
                        "diff": diff,
                    }
                )
                if original_value is None or (diff is not None and diff > tolerance):
                    status = "fail"
        return ReplicationReportPayload(
            schema_version=IMPL_SCHEMA_VERSION_V2,
            experiment_id=manifest.experiment_id,
            comparison=comparison,
            status=status,
        )

    def _write_attestation(
        self,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        stage: str,
        profile_id: str,
        repo_context: RepoContextPayload | RepoContextPayloadV2 | RepoContextPayloadV21,
        profile_catalog: ExecutionProfileCatalogPayload | None,
        tool_id: str | None = None,
        inputs: list[str] | None = None,
        spec_versions: dict[str, str] | None = None,
        runner_version: dict[str, str] | None = None,
    ) -> str:
        profile_map = {p.profile_id: p for p in profile_catalog.profiles} if profile_catalog else {}
        profile = profile_map.get(profile_id)
        base_digest = profile.base_image.digest if profile else "sha256:unknown"
        inputs = inputs or []
        input_refs = []
        parent_refs: list[str] = []
        for ref in inputs:
            try:
                payload = store.read_artifact(ref).payload
                input_refs.append({"artifact_ref": ref, "hash": self._hash_payload(payload)})
                parent_refs.append(ref)
            except ArtifactNotFoundError:
                input_refs.append({"artifact_ref": ref, "hash": None})
            except Exception:
                input_refs.append({"artifact_ref": ref, "hash": None})
        spec_payload = {"implementation": self._schema_version}
        if spec_versions:
            spec_payload.update(spec_versions)
        runner_payload = {"runner": "implementation_engine"}
        if runner_version:
            runner_payload.update(runner_version)
        payload = AttestationBundlePayload(
            run_id=str(run_id),
            stage=stage,
            lane_id=None,
            code_sha={"base_commit": repo_context.base_commit, "head_commit": repo_context.head_commit},
            profile_id=profile_id,
            base_image_digest=base_digest,
            tool_id=tool_id,
            inputs=input_refs,
            spec_versions=spec_payload,
            timestamps={"start": datetime.now(UTC).isoformat(), "end": datetime.now(UTC).isoformat()},
            runner_version=runner_payload,
        )
        artifact_id = f"attestation_{stage}_{tool_id or profile_id}_{uuid4().hex[:8]}"
        self._write_artifact(
            store,
            workspace,
            run_id,
            "attestation_bundle",
            artifact_id,
            payload.model_dump(),
            parents=parent_refs,
            stage=stage,
        )
        return artifact_id

    def _update_evidence_index(
        self,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        current_ref: str,
        updates: list[EvidenceIndexEntry],
        stage: str,
    ) -> str:
        current = store.read_artifact(current_ref)
        payload = EvidenceIndexPayload.model_validate(current.payload)
        index = {entry.expectation_id: entry for entry in payload.evidence}
        for update in updates:
            existing = index.get(update.expectation_id)
            if existing is None:
                index[update.expectation_id] = update
            else:
                if update.status == "fail":
                    existing.status = "fail"
                elif update.status == "waived":
                    if existing.status != "fail":
                        existing.status = "waived"
                        existing.waiver_approvals = update.waiver_approvals
                elif update.status == "pass":
                    if existing.status not in {"fail", "waived"}:
                        existing.status = "pass"
                existing.evidence_pointers.extend(update.evidence_pointers)
        new_payload = EvidenceIndexPayload(schema_version=payload.schema_version, evidence=list(index.values()))
        new_id = f"{current_ref}_{uuid4().hex[:8]}"
        path = store.patch_artifact(
            artifact_id=current_ref,
            new_artifact_id=new_id,
            patch_ops=[{"op": "replace", "path": "/evidence", "value": [e.model_dump() for e in new_payload.evidence]}],
            run_id=run_id,
        )
        workspace.index_artifact(path, "evidence_index", self._schema_version, {"stage": stage, "role": "engine"})
        return new_id

    def _prepare_tool_registry(
        self,
        registry_payload: dict[str, Any] | None,
        probe_payload: dict[str, Any] | None,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        stage: str,
        repo_context: RepoContextPayload | RepoContextPayloadV2 | RepoContextPayloadV21,
        profile_catalog: ExecutionProfileCatalogPayload | None,
        allow_unprobed: bool,
    ) -> tuple[ToolRegistryPayload, ToolProbeResultsPayload | None, list[str]]:
        registry = (
            ToolRegistryPayload.model_validate(registry_payload)
            if registry_payload
            else ToolRegistryPayload(schema_version=IMPL_SCHEMA_VERSION_V2, registry_id="default", tools=[])
        )
        probe_results = ToolProbeResultsPayload.model_validate(probe_payload) if probe_payload else None
        attestation_ids: list[str] = []
        if probe_results:
            default_profile_id = (
                profile_catalog.profiles[0].profile_id
                if profile_catalog and profile_catalog.profiles
                else "legacy_v1"
            )
            for probe in probe_results.probes:
                if not probe.attestation_ref:
                    att_id = self._write_attestation(
                        store,
                        workspace,
                        run_id,
                        stage=stage,
                        profile_id=default_profile_id,
                        repo_context=repo_context,
                        profile_catalog=profile_catalog,
                        tool_id=probe.tool_id,
                        inputs=[],
                    )
                    probe.attestation_ref = att_id
                    attestation_ids.append(att_id)
            probe_map = {(probe.tool_id, probe.tool_version): idx for idx, probe in enumerate(probe_results.probes)}
            for tool in registry.tools:
                key = (tool.tool_id, tool.tool_version)
                if tool.probe_status == "probed" and tool.last_probe is None and key in probe_map:
                    tool.last_probe = EvidencePointer(
                        artifact_ref="tool_probe_results_v2", json_pointer=f"/probes/{probe_map[key]}"
                    )
        if not probe_results and registry.tools:
            if allow_unprobed:
                for tool in registry.tools:
                    tool.probe_status = "unprobed"
                    tool.last_probe = None
            else:
                raise RuntimeError("Tool registry entries require probe results")
        return registry, probe_results, attestation_ids

    def _run_remote_ops(
        self,
        manifest: RemoteOpsManifestPayload,
        expectation_registry: ExpectationRegistryPayload,
        evidence_index_ref: str,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        stage: str,
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        profile_catalog: ExecutionProfileCatalogPayload | None,
        cache: RemoteRunCacheIndexPayload,
        retry_cap: int,
        manifest_ref: str | None = None,
        expectation_registry_ref: str | None = None,
    ) -> tuple[RemoteOpsResultsPayload, RemoteOpEventsPayload, RemoteRunCacheIndexPayload, str, list[str]]:
        expectation_map = {exp.expectation_id: exp for exp in expectation_registry.expectations}
        results: list[RemoteOpResult] = []
        events: list[RemoteOpEvent] = []
        diff_report_ids: list[str] = []
        cache_index = {entry.idempotency_key: entry for entry in cache.entries}
        cached_results: dict[str, RemoteOpResult] = {}
        artifacts_for_eval = {artifact.artifact_id: artifact.payload for artifact in store.list_artifacts()}

        def _expectation_for(exp_id: str) -> Expectation | None:
            base = expectation_map.get(exp_id)
            if op.oracle_override:
                if base:
                    return base.model_copy(update={"oracle": op.oracle_override})
                return Expectation(
                    expectation_id=exp_id,
                    level="plan",
                    source={"type": "override", "id": exp_id},
                    subject={"kind": "remote_op", "artifact_type": None, "name": op.op_id},
                    oracle=op.oracle_override,
                    tolerance=None,
                    inputs=[],
                    parents=[],
                    children=[],
                    must_exist_before_remote_ops=True,
                )
            return base

        def _evaluate_expectations(output: Any) -> tuple[bool, str | None, list[dict[str, Any]]]:
            for exp_id in op.expected:
                exp = _expectation_for(exp_id)
                if exp is None:
                    return False, exp_id, [{"reason": "expectation_missing"}]
                ok, diffs = evaluate_expectation(exp, output, artifacts_for_eval)
                if not ok:
                    return False, exp_id, diffs
            return True, None, []

        for op in manifest.ops:
            if not op.idempotency_key:
                if op.task_id:
                    op.idempotency_key = f"{run_id}/{op.task_id}/{op.op_id}/{repo_context.base_commit}"
                else:
                    op.idempotency_key = f"{run_id}/{op.op_id}/{repo_context.base_commit}"
            cached = cache_index.get(op.idempotency_key)
            if op.idempotency_key in cached_results:
                cached_result = cached_results[op.idempotency_key]
                cloned = RemoteOpResult.model_validate(cached_result.model_dump())
                cloned.op_id = op.op_id
                events.append(
                    RemoteOpEvent(
                        timestamp=datetime.now(UTC),
                        op_id=op.op_id,
                        event="cache_hit",
                        attempt_id=None,
                        status="cached",
                        detail=cached.result_ref if cached else "in_run_cache",
                    )
                )
                results.append(cloned)
                continue
            if cached:
                events.append(
                    RemoteOpEvent(
                        timestamp=datetime.now(UTC),
                        op_id=op.op_id,
                        event="cache_hit",
                        attempt_id=None,
                        status="cached",
                        detail=cached.result_ref,
                    )
                )
                try:
                    cached_payload = store.read_artifact(cached.result_ref).payload
                    cached_results = RemoteOpsResultsPayload.model_validate(cached_payload)
                    if cached_results.results:
                        cached_result = cached_results.results[0]
                        cloned = RemoteOpResult.model_validate(cached_result.model_dump())
                        cloned.op_id = op.op_id
                        results.append(cloned)
                        continue
                except Exception:
                    pass

            attempts: list[RemoteOpAttempt] = []
            status = "fail"
            attempt_statuses = op.mock_attempts or ["success"]
            attempt_id = 0
            for attempt_status in attempt_statuses:
                attempt_id += 1
                if attempt_status == "infra_error":
                    attempts.append(
                        RemoteOpAttempt(
                            attempt_id=f"{op.op_id}:{attempt_id}",
                            status="fail",
                            error_class="infra",
                        )
                    )
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="attempt",
                            attempt_id=f"{op.op_id}:{attempt_id}",
                            status="infra_error",
                        )
                    )
                    if attempt_id >= retry_cap + 1:
                        status = "fail"
                        break
                    continue
                if attempt_status == "semantic_error":
                    attempts.append(
                        RemoteOpAttempt(
                            attempt_id=f"{op.op_id}:{attempt_id}",
                            status="fail",
                            error_class="semantic",
                        )
                    )
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="attempt",
                            attempt_id=f"{op.op_id}:{attempt_id}",
                            status="semantic_error",
                        )
                    )
                    status = "fail"
                    break
                attempts.append(RemoteOpAttempt(attempt_id=f"{op.op_id}:{attempt_id}", status="pass"))
                events.append(
                    RemoteOpEvent(
                        timestamp=datetime.now(UTC),
                        op_id=op.op_id,
                        event="attempt",
                        attempt_id=f"{op.op_id}:{attempt_id}",
                        status="pass",
                    )
                )
                status = "pass"
                break

            output = op.mock_output
            canary_output = op.mock_canary_output if op.mock_canary_output is not None else output
            verdict = None
            diff_report_ref = None
            calls_used = len(attempts)
            time_used = float(calls_used)
            if status == "pass" and op.canary and op.canary.cost_cap is not None and op.mock_cost is not None:
                if op.mock_cost > op.canary.cost_cap:
                    status = "fail"
                    verdict = "fail"
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="canary_budget_exceeded",
                            attempt_id=None,
                            status="fail",
                            detail="cost_cap",
                        )
                    )
            if status == "pass" and op.budget:
                if op.budget.cost_usd is not None and op.mock_cost is not None and op.mock_cost > op.budget.cost_usd:
                    status = "fail"
                    verdict = "fail"
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="budget_exceeded",
                            attempt_id=None,
                            status="fail",
                            detail="cost_usd",
                        )
                    )
                if op.budget.max_calls is not None and calls_used > op.budget.max_calls:
                    status = "fail"
                    verdict = "fail"
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="budget_exceeded",
                            attempt_id=None,
                            status="fail",
                            detail="max_calls",
                        )
                    )
                if op.budget.time_sec is not None and time_used > op.budget.time_sec:
                    status = "fail"
                    verdict = "fail"
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="budget_exceeded",
                            attempt_id=None,
                            status="fail",
                            detail="time_sec",
                        )
                    )
            if status == "pass":
                if op.canary:
                    canary_ok, failed_exp, diffs = _evaluate_expectations(canary_output)
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="canary_result",
                            attempt_id=None,
                            status="pass" if canary_ok else "fail",
                        )
                    )
                    if not canary_ok:
                        diff_payload = DiffReportPayload(
                            expectation_id=failed_exp or "unknown",
                            run_id=str(run_id),
                            summary="remote op canary expectation mismatch",
                            diffs=diffs,
                            evidence=[
                                EvidencePointer(artifact_ref=expectation_registry_ref, json_pointer="/")
                                if expectation_registry_ref
                                else EvidencePointer(artifact_ref=evidence_index_ref, json_pointer="/")
                            ]
                            + (
                                [EvidencePointer(artifact_ref=manifest_ref, json_pointer="/")]
                                if manifest_ref
                                else []
                            ),
                        )
                        diff_id = f"diff_report_{failed_exp or 'unknown'}_{op.op_id}"
                        diff_report_ref = self._write_artifact(
                            store,
                            workspace,
                            run_id,
                            "diff_report",
                            diff_id,
                            diff_payload.model_dump(),
                            parents=[evidence_index_ref],
                            stage=stage,
                        )
                        workspace.write_canonical_note(
                            f"diff_reports/{failed_exp or 'unknown'}/{run_id}.json",
                            diff_payload.model_dump(),
                            {"stage": stage, "role": "engine"},
                        )
                        diff_report_ids.append(diff_report_ref)
                        verdict = "fail"
                        status = "fail"
                    elif op.scale or output is not None:
                        if op.scale:
                            if (
                                op.scale.max_cost is not None
                                and op.mock_cost is not None
                                and op.mock_cost > op.scale.max_cost
                            ):
                                verdict = "fail"
                                status = "fail"
                                events.append(
                                    RemoteOpEvent(
                                        timestamp=datetime.now(UTC),
                                        op_id=op.op_id,
                                        event="scale_budget_exceeded",
                                        attempt_id=None,
                                        status="fail",
                                        detail="max_cost",
                                    )
                                )
                            if (
                                op.scale.max_calls is not None
                                and calls_used > op.scale.max_calls
                                and status == "pass"
                            ):
                                verdict = "fail"
                                status = "fail"
                                events.append(
                                    RemoteOpEvent(
                                        timestamp=datetime.now(UTC),
                                        op_id=op.op_id,
                                        event="scale_budget_exceeded",
                                        attempt_id=None,
                                        status="fail",
                                        detail="max_calls",
                                    )
                                )
                        if status != "pass":
                            pass
                        else:
                            scaled_ok, failed_exp, diffs = _evaluate_expectations(output)
                            events.append(
                                RemoteOpEvent(
                                    timestamp=datetime.now(UTC),
                                    op_id=op.op_id,
                                    event="scaled_result",
                                    attempt_id=None,
                                    status="pass" if scaled_ok else "fail",
                                )
                            )
                            if not scaled_ok:
                                diff_payload = DiffReportPayload(
                                    expectation_id=failed_exp or "unknown",
                                    run_id=str(run_id),
                                    summary="remote op expectation mismatch",
                                    diffs=diffs,
                                    evidence=[
                                        EvidencePointer(artifact_ref=expectation_registry_ref, json_pointer="/")
                                        if expectation_registry_ref
                                        else EvidencePointer(artifact_ref=evidence_index_ref, json_pointer="/")
                                    ]
                                    + (
                                        [EvidencePointer(artifact_ref=manifest_ref, json_pointer="/")]
                                        if manifest_ref
                                        else []
                                    ),
                                )
                                diff_id = f"diff_report_{failed_exp or 'unknown'}_{op.op_id}"
                                diff_report_ref = self._write_artifact(
                                    store,
                                    workspace,
                                    run_id,
                                    "diff_report",
                                    diff_id,
                                    diff_payload.model_dump(),
                                    parents=[evidence_index_ref],
                                    stage=stage,
                                )
                                workspace.write_canonical_note(
                                    f"diff_reports/{failed_exp or 'unknown'}/{run_id}.json",
                                    diff_payload.model_dump(),
                                    {"stage": stage, "role": "engine"},
                                )
                                diff_report_ids.append(diff_report_ref)
                                verdict = "fail"
                                status = "fail"
                            else:
                                verdict = "pass"
                                status = "pass"
                    else:
                        verdict = "pass"
                        status = "pass"
                else:
                    ok, failed_exp, diffs = _evaluate_expectations(output)
                    events.append(
                        RemoteOpEvent(
                            timestamp=datetime.now(UTC),
                            op_id=op.op_id,
                            event="evaluation_result",
                            attempt_id=None,
                            status="pass" if ok else "fail",
                        )
                    )
                    if not ok:
                        diff_payload = DiffReportPayload(
                            expectation_id=failed_exp or "unknown",
                            run_id=str(run_id),
                            summary="remote op expectation mismatch",
                            diffs=diffs,
                            evidence=[
                                EvidencePointer(artifact_ref=expectation_registry_ref, json_pointer="/")
                                if expectation_registry_ref
                                else EvidencePointer(artifact_ref=evidence_index_ref, json_pointer="/")
                            ]
                            + (
                                [EvidencePointer(artifact_ref=manifest_ref, json_pointer="/")]
                                if manifest_ref
                                else []
                            ),
                        )
                        diff_id = f"diff_report_{failed_exp or 'unknown'}_{op.op_id}"
                        diff_report_ref = self._write_artifact(
                            store,
                            workspace,
                            run_id,
                            "diff_report",
                            diff_id,
                            diff_payload.model_dump(),
                            parents=[evidence_index_ref],
                            stage=stage,
                        )
                        workspace.write_canonical_note(
                            f"diff_reports/{failed_exp or 'unknown'}/{run_id}.json",
                            diff_payload.model_dump(),
                            {"stage": stage, "role": "engine"},
                        )
                        diff_report_ids.append(diff_report_ref)
                        verdict = "fail"
                        status = "fail"
                    else:
                        verdict = "pass"
                        status = "pass"
            attestation_ref = self._write_attestation(
                store,
                workspace,
                run_id,
                stage=stage,
                profile_id=op.profile_id,
                repo_context=repo_context,
                profile_catalog=profile_catalog,
                tool_id=op.tool_id,
                inputs=(op.inputs.snapshot_refs if op.inputs else []),
                spec_versions={
                    "expectation_registry": expectation_registry.schema_version,
                    "remote_ops_manifest": manifest.schema_version,
                },
                runner_version={"evaluator": "expectation_evaluator_v2"},
            )
            result = RemoteOpResult(
                op_id=op.op_id,
                status=status,
                attempts=attempts,
                cost_usd=op.mock_cost,
                time_sec=time_used,
                calls_used=calls_used,
                output_ref=None,
                output_hash=self._hash_payload({"output": output}) if output is not None else (
                    self._hash_payload({"output": canary_output}) if canary_output is not None else None
                ),
                evaluator_verdict=verdict,
                diff_report_ref=diff_report_ref,
                attestation_ref=attestation_ref,
            )
            results.append(result)
            cached_results[op.idempotency_key] = result
            cache_entry = RemoteRunCacheEntry(
                idempotency_key=op.idempotency_key,
                result_ref=f"remote_ops_results_{op.op_id}",
                created_at=datetime.now(UTC),
            )
            cache.entries.append(cache_entry)
            cache_index[op.idempotency_key] = cache_entry

        # Write per-op results for cache references
        for result in results:
            per_op_payload = RemoteOpsResultsPayload(schema_version=IMPL_SCHEMA_VERSION_V2, results=[result])
            self._write_artifact(
                store,
                workspace,
                run_id,
                "remote_ops_results",
                f"remote_ops_results_{result.op_id}",
                per_op_payload.model_dump(),
                parents=[evidence_index_ref],
                stage=stage,
            )

        results_payload = RemoteOpsResultsPayload(schema_version=IMPL_SCHEMA_VERSION_V2, results=results)
        events_payload = RemoteOpEventsPayload(schema_version=IMPL_SCHEMA_VERSION_V2, events=events)
        return results_payload, events_payload, cache, evidence_index_ref, diff_report_ids

    def _create_patchset_for_task(self, task: WorkTask | WorkTaskV2, base_commit: str) -> PatchsetPayload:
        content = f"Task {task.task_id}: {task.title}\n"
        change = PatchChange(path=f"generated/{task.task_id}.txt", action="add", after=content)
        operation_count = 1
        bytes_changed = len(content)
        return PatchsetPayload(
            patchset_id=f"patchset_{task.task_id}_v1",
            lane_id=task.lane_id,
            base_commit=base_commit,
            changes=[change],
            apply_order=[0],
            maps_to_requirements=task.maps_to_requirements,
            maps_to_acceptance_tests=task.maps_to_acceptance_tests,
            semantic_change=False,
            change_request_ref=None,
            operation_count=operation_count,
            bytes_changed=bytes_changed,
        )

    def _write_artifact(
        self,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        artifact_type: str,
        artifact_id: str,
        payload: dict[str, Any],
        parents: list[str],
        stage: str,
        role: str = "engine",
    ) -> str:
        envelope = ImplementationArtifactEnvelope(
            artifact_type=artifact_type,  # type: ignore[arg-type]
            artifact_id=artifact_id,
            schema_version=self._schema_version,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=parents,
            payload=payload,
        )
        path = store.write_artifact(envelope)
        workspace.index_artifact(path, artifact_type, envelope.schema_version, {"stage": stage, "role": role})
        payload_hash = self._hash_payload(payload)
        self._event(
            workspace.run_root,
            run_id,
            stage=stage,
            event_type="artifact_write",
            refs={"artifact_id": artifact_id},
            payload={"artifact_type": artifact_type, "role": role, "payload_hash": payload_hash},
        )
        return artifact_id

    def _write_stage_handoff(
        self,
        workspace: WorkspaceManager,
        run_id: UUID,
        from_stage: str,
        to_stage: str,
        objectives: list[str],
        completed_refs: list[str],
        pending_blockers: list[str],
        next_actions: list[str],
        evidence: list[EvidencePointer],
    ) -> str:
        handoff_id = f"handoff_{from_stage}_to_{to_stage}"
        payload = StageHandoffPayload(
            schema_version=self._schema_version,
            handoff_id=handoff_id,
            from_stage=from_stage,
            to_stage=to_stage,
            objectives=objectives,
            completed_refs=completed_refs,
            pending_blockers=pending_blockers,
            next_actions=next_actions,
            evidence=evidence,
        )
        workspace.write_stage_handoff(payload, {"stage": from_stage, "role": "engine"})
        self._event(
            workspace.run_root,
            run_id,
            stage=from_stage,
            event_type="decision",
            refs={},
            payload={"stage_handoff": handoff_id},
        )
        return handoff_id

    def _write_context_pack(
        self,
        workspace: WorkspaceManager,
        run_id: UUID,
        stage: str,
        role: str,
        artifact_refs: list[str],
        evidence: list[EvidencePointer],
        required_refs: list[str] | None = None,
    ) -> str:
        limits = workspace.context.context_pack_limits
        if required_refs:
            missing = [ref for ref in required_refs if ref not in artifact_refs]
            if missing:
                raise RuntimeError(f"Context adequacy failure: missing {', '.join(missing)}")
        pack_id = f"context_{stage}_{role}"
        payload = ContextPackPayload(
            schema_version=self._schema_version,
            pack_id=pack_id,
            stage=stage,
            role=role,
            artifact_refs=artifact_refs,
            evidence=evidence,
            max_items=limits.max_items,
            max_bytes=limits.max_bytes,
            size_bytes=len(json.dumps(artifact_refs).encode("utf-8")),
        )
        workspace.write_context_pack(payload, {"stage": stage, "role": role})
        self._event(
            workspace.run_root,
            run_id,
            stage=stage,
            event_type="decision",
            refs={},
            payload={"context_pack": pack_id},
        )
        return pack_id

    def _validate_role_outputs(
        self,
        workspace: WorkspaceManager,
        role_sets: dict[str, set[str]],
    ) -> None:
        index = workspace.index_payload()
        for item in index.items:
            provenance = item.provenance
            if isinstance(provenance, dict):
                role = provenance.get("role")
            else:
                role = getattr(provenance, "role", None)
            if item.item_type == "patchset" and role not in role_sets["writer"]:
                raise RuntimeError("Role separation violation: patchset not produced by writer role")
            if item.item_type == "review_findings" and role not in role_sets["reviewer"]:
                raise RuntimeError("Role separation violation: review findings not produced by reviewer role")
            if item.item_type == "decision_record" and role not in role_sets["judge"]:
                raise RuntimeError("Role separation violation: decision record not produced by judge role")

    def _run_validators(
        self,
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        run_root: Path,
        run_id: UUID,
        stage: str,
        retry_cap: int,
    ) -> tuple[TestResultsPayload, QualityReportsPayload, list[str]]:
        results: list[TestOutcome] = []
        quality_checks: list[QualityCheck] = []
        quarantined: list[str] = []
        for validator in repo_context.validator_entrypoints:
            attempts = 0
            passed = False
            while attempts <= retry_cap:
                attempts += 1
                if validator.mode == "mock":
                    if validator.outcomes:
                        outcome = validator.outcomes[
                            min(attempts - 1, len(validator.outcomes) - 1)
                        ]
                    else:
                        outcome = "pass"
                    passed = outcome == "pass"
                else:
                    if not validator.command:
                        passed = True
                    else:
                        proc = subprocess.run(
                            validator.command,
                            cwd=repo_context.repo_root,
                            capture_output=True,
                            text=True,
                        )
                        passed = proc.returncode == 0
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="validator_result",
                    refs={},
                    payload={"validator_id": validator.id, "attempt": attempts, "passed": passed},
                    actor={"kind": "validator", "role": validator.name},
                )
                if passed or not validator.retryable or attempts >= retry_cap + 1:
                    break
            quarantined_flag = (not passed) and attempts > retry_cap
            if quarantined_flag:
                quarantined.append(validator.id)
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "validator_id": validator.id,
                        "status": "quarantined",
                        "escalation_path": ["patch", "reroute", "hitl", "change_request"],
                    },
                )
            results.append(
                TestOutcome(
                    name=validator.name,
                    status="pass" if passed else "fail",
                    duration_sec=None,
                    maps_to_acceptance_tests=validator.maps_to_acceptance_tests,
                    attempts=attempts,
                    quarantined=quarantined_flag,
                )
            )
            quality_checks.append(
                QualityCheck(
                    name=validator.name,
                    status="pass" if passed else "fail",
                    details=None,
                )
            )
            if not passed and attempts > retry_cap:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "escalation": "retry_budget_exceeded",
                        "validator_id": validator.id,
                        "path": "patch->reroute->hitl->change_request",
                    },
                )
        overall_status = "PASS" if all(r.status == "pass" for r in results) else "FAIL"
        return (
            TestResultsPayload(
                results=results,
                overall_status=overall_status,
                quarantined_validators=quarantined,
            ),
            QualityReportsPayload(
                checks=quality_checks,
                overall_status=overall_status,
            ),
            quarantined,
        )

    def _lane_tasks(self, work_plan: WorkPlanPayload | WorkPlanPayloadV2, lane_id: str) -> list[WorkTask | WorkTaskV2]:
        return [task for task in work_plan.tasks if task.lane_id == lane_id]

    def _apply_patchset_transactional(
        self,
        repo_root: Path,
        patchset: PatchsetPayload,
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        patchset_limits: PatchsetLimits,
        *,
        allow_protected: bool,
    ) -> dict[str, Any]:
        temp_root = repo_root.parent / f".worktree_{patchset.patchset_id}"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        if not repo_root.exists():
            raise RuntimeError(f"Repo root missing: {repo_root}")
        shutil.copytree(repo_root, temp_root)
        try:
            result = apply_patchset(
                temp_root,
                patchset,
                repo_context.forbidden_paths,
                repo_context.protected_paths,
                patchset_limits.max_operations,
                allow_protected=allow_protected,
            )
            changed_files = result.get("changed_files", [])
            backups: dict[str, bytes] = {}
            added: set[str] = set()
            for rel_path in changed_files:
                dest = repo_root / rel_path
                if dest.exists():
                    backups[rel_path] = dest.read_bytes()
                else:
                    added.add(rel_path)
            try:
                for rel_path in changed_files:
                    src = temp_root / rel_path
                    dest = repo_root / rel_path
                    if src.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dest)
                    else:
                        if dest.exists():
                            dest.unlink()
            except Exception:
                for rel_path, content in backups.items():
                    dest = repo_root / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content)
                for rel_path in added:
                    dest = repo_root / rel_path
                    if dest.exists():
                        dest.unlink()
                raise
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)
        return result

    def _apply_patchsets_to_checkout(
        self,
        repo_root: Path,
        patchsets: list[PatchsetPayload],
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        patchset_limits: PatchsetLimits,
        run_root: Path,
        run_id: UUID,
        stage: str,
        lane_id: str | None = None,
        approved_patchsets: set[str] | None = None,
        record_patchsets: bool = False,
        guardrails: GuardrailPolicy | None = None,
        plan_hash: str | None = None,
        milestone_id: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        changed_files: list[str] = []
        file_hashes: dict[str, str] = {}
        approved_patchsets = approved_patchsets or set()
        cycle_cap = patchset_limits.max_operations_total or patchset_limits.max_operations
        total_ops = 0
        patchsets_root: Path | None = None
        guardrail_counters = GuardrailCounters()
        if record_patchsets:
            patchsets_root = run_root / "implementation" / "patchsets"
            patchsets_root.mkdir(parents=True, exist_ok=True)
        for patchset in patchsets:
            if patchset.base_commit != repo_context.base_commit:
                raise RuntimeError("Patchset base_commit mismatch")
            total_ops += len(patchset.changes)
            if total_ops > cycle_cap:
                raise PatchLimitError("Patchset operation cycle cap exceeded")
            approved = patchset.patchset_id in approved_patchsets
            if guardrails is not None:
                self._enforce_patchset_guardrails(
                    guardrails=guardrails,
                    counters=guardrail_counters,
                    patchset=patchset,
                    run_root=run_root,
                    plan_hash=plan_hash,
                    milestone_id=milestone_id,
                )
            if self._patchset_touches_paths(patchset, repo_context.protected_paths):
                if not approved:
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="decision",
                        payload={
                            "patchset_id": patchset.patchset_id,
                            "status": "blocked_protected_path",
                            "hitl_required": True,
                            "approved": approved,
                        },
                    )
                    raise RuntimeError("Patchset touches protected paths")
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "patchset_id": patchset.patchset_id,
                        "status": "approved_protected_path",
                        "hitl_required": True,
                        "approved": approved,
                    },
                )
            pre_tree_hash = self._compute_tree_hash(repo_root) if record_patchsets else None
            try:
                result = self._apply_patchset_transactional(
                    repo_root,
                    patchset,
                    repo_context,
                    patchset_limits,
                    allow_protected=approved,
                )
            except Exception as exc:
                payload: dict[str, object] = {"error": str(exc), "patchset": patchset.patchset_id}
                if lane_id:
                    payload["lane_id"] = lane_id
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="error",
                    payload=payload,
                )
                if record_patchsets and patchsets_root is not None:
                    apply_result = {
                        "schema_version": "apply_result.v1",
                        "patchset_id": patchset.patchset_id,
                        "status": "failed",
                        "pre_apply_tree_hash": pre_tree_hash,
                        "post_apply_tree_hash": None,
                        "error": str(exc),
                        "per_file": [],
                        "integration_status": "failed",
                    }
                    (patchsets_root / f"apply_result_{patchset.patchset_id}_v1.json").write_text(
                        deterministic_json_dumps(apply_result),
                        encoding="utf-8",
                    )
                raise
            if record_patchsets and patchsets_root is not None:
                post_tree_hash = self._compute_tree_hash(repo_root)
                files_touched = sorted({change.path for change in patchset.changes})
                diff_path = patchsets_root / f"{patchset.patchset_id}_v1.diff"
                if not diff_path.exists():
                    diff_path.write_text(deterministic_json_dumps(patchset.model_dump()), encoding="utf-8")
                patch_manifest = {
                    "schema_version": "patch_manifest.v1",
                    "patchset_id": patchset.patchset_id,
                    "files_touched": files_touched,
                    "stats": {
                        "files": len(files_touched),
                        "operations": len(patchset.changes),
                        "bytes_changed": patchset.bytes_changed or patchset_bytes_changed(patchset),
                    },
                    "linked_plan_ids": sorted(
                        set(patchset.maps_to_requirements + patchset.maps_to_acceptance_tests)
                    ),
                }
                (patchsets_root / f"patch_manifest_{patchset.patchset_id}_v1.json").write_text(
                    deterministic_json_dumps(patch_manifest),
                    encoding="utf-8",
                )
                apply_result = {
                    "schema_version": "apply_result.v1",
                    "patchset_id": patchset.patchset_id,
                    "status": "success",
                    "pre_apply_tree_hash": pre_tree_hash,
                    "post_apply_tree_hash": post_tree_hash,
                    "per_file": [{"path": path, "status": "applied"} for path in files_touched],
                    "integration_status": "applied",
                }
                (patchsets_root / f"apply_result_{patchset.patchset_id}_v1.json").write_text(
                    deterministic_json_dumps(apply_result),
                    encoding="utf-8",
                )
                append_vnext_event(
                    run_root,
                    "implementation",
                    "PATCH_APPLIED",
                    run_id=str(run_id),
                    payload={"patchset_id": patchset.patchset_id, "post_tree_hash": post_tree_hash},
                )
            changed_files.extend(result["changed_files"])
            file_hashes.update(result["file_hashes"])
        return sorted(set(changed_files)), file_hashes

    def _patchset_touches_paths(self, patchset: PatchsetPayload, patterns: list[str]) -> bool:
        from fnmatch import fnmatch

        for change in patchset.changes:
            path = change.path.replace("\\", "/").lstrip("/")
            if any(fnmatch(path, pattern) for pattern in patterns):
                return True
        return False

    def _classify_conflict(self, exc: Exception) -> ConflictItem:
        message = str(exc)
        path = "unknown"
        reason = message
        if ":" in message:
            parts = message.split(":", 1)
            reason = parts[0].strip()
            candidate = parts[1].strip()
            if candidate:
                path = candidate
        return ConflictItem(path=path, reason=reason)

    def _apply_patchsets_with_conflict_policy(
        self,
        repo_root: Path,
        patchsets: list[PatchsetPayload],
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        patchset_limits: PatchsetLimits,
        run_root: Path,
        run_id: UUID,
        stage: str,
        approved_patchsets: set[str],
        conflict_policy: dict[str, object],
    ) -> tuple[list[str], dict[str, str], list[ConflictItem], list[str], list[PatchsetPayload], bool]:
        max_conflicts = int(conflict_policy.get("max_conflicts", 0) or 0)
        auto_resolution = str(conflict_policy.get("auto_resolution", "none"))
        conflicts: list[ConflictItem] = []
        skipped: list[str] = []
        applied: list[PatchsetPayload] = []
        blocked = False
        changed_files: list[str] = []
        file_hashes: dict[str, str] = {}
        cycle_cap = patchset_limits.max_operations_total or patchset_limits.max_operations
        total_ops = 0
        for patchset in patchsets:
            if patchset.base_commit != repo_context.base_commit:
                raise RuntimeError("Patchset base_commit mismatch")
            total_ops += len(patchset.changes)
            if total_ops > cycle_cap:
                raise PatchLimitError("Patchset operation cycle cap exceeded")
            approved = patchset.patchset_id in approved_patchsets
            if self._patchset_touches_paths(patchset, repo_context.protected_paths):
                if not approved:
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="decision",
                        payload={
                            "patchset_id": patchset.patchset_id,
                            "status": "blocked_protected_path",
                            "hitl_required": True,
                            "approved": approved,
                        },
                    )
                    raise RuntimeError("Patchset touches protected paths")
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "patchset_id": patchset.patchset_id,
                        "status": "approved_protected_path",
                        "hitl_required": True,
                        "approved": approved,
                    },
                )
            try:
                result = self._apply_patchset_transactional(
                    repo_root,
                    patchset,
                    repo_context,
                    patchset_limits,
                    allow_protected=approved,
                )
            except PatchConflictError as exc:
                conflict_item = self._classify_conflict(exc)
                conflicts.append(conflict_item)
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "patchset_id": patchset.patchset_id,
                        "status": "conflict_detected",
                        "reason": conflict_item.reason,
                        "path": conflict_item.path,
                    },
                )
                if len(conflicts) > max_conflicts or auto_resolution != "skip":
                    blocked = True
                    break
                skipped.append(patchset.patchset_id)
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "patchset_id": patchset.patchset_id,
                        "status": "conflict_auto_skip",
                        "reason": conflict_item.reason,
                        "path": conflict_item.path,
                    },
                )
                continue
            except (PatchApplyError, PatchLimitError) as exc:
                payload: dict[str, object] = {"error": str(exc), "patchset": patchset.patchset_id}
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="error",
                    payload=payload,
                )
                raise
            applied.append(patchset)
            changed_files.extend(result["changed_files"])
            file_hashes.update(result["file_hashes"])
        return sorted(set(changed_files)), file_hashes, conflicts, skipped, applied, blocked

    def _run_builds(
        self,
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        repo_root: Path,
        run_root: Path,
        run_id: UUID,
        stage: str,
        retry_cap: int,
    ) -> tuple[list[QualityCheck], list[dict[str, object]]]:
        checks: list[QualityCheck] = []
        provenance: list[dict[str, object]] = []
        if not repo_context.build_entrypoints:
            checks.append(QualityCheck(name="build", status="pass", details="no build entrypoints configured"))
            return checks, provenance
        for entry in repo_context.build_entrypoints:
            attempts = 0
            passed = False
            while attempts <= retry_cap:
                attempts += 1
                workdir = entry.working_dir or str(repo_root)
                if not Path(workdir).is_absolute():
                    workdir = str((repo_root / workdir).resolve())
                proc = subprocess.run(
                    entry.command,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                )
                passed = proc.returncode == 0
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="validator_result",
                    payload={"validator_id": f"build:{entry.name}", "attempt": attempts, "passed": passed},
                    actor={"kind": "validator", "role": entry.name},
                )
                if passed or attempts >= retry_cap + 1:
                    break
            checks.append(
                QualityCheck(
                    name=f"build:{entry.name}",
                    status="pass" if passed else "fail",
                    details=f"attempts={attempts}",
                )
            )
            provenance.append(
                {
                    "name": entry.name,
                    "command": entry.command,
                    "working_dir": entry.working_dir or str(repo_root),
                    "attempts": attempts,
                    "status": "pass" if passed else "fail",
                }
            )
        return checks, provenance

    def _run_security_checks(
        self,
        config: dict[str, Any],
        repo_root: Path,
        run_root: Path,
        run_id: UUID,
        stage: str,
        retry_cap: int,
    ) -> list[QualityCheck]:
        checks: list[QualityCheck] = []
        raw = config.get("security_checks", [])
        if not isinstance(raw, list) or not raw:
            checks.append(QualityCheck(name="security_scan", status="pass", details="no checks configured"))
            return checks
        for idx, item in enumerate(raw, start=1):
            name = f"security_check_{idx}"
            command: list[str] | None = None
            mode = "command"
            outcomes: list[str] = []
            if isinstance(item, dict):
                name = str(item.get("name") or name)
                command = item.get("command")
                if isinstance(command, str):
                    command = [command]
                mode = str(item.get("mode", "command"))
                outcomes = [str(o) for o in item.get("outcomes", []) if isinstance(o, str)]
            elif isinstance(item, list):
                command = [str(c) for c in item]
            if not command:
                continue
            attempts = 0
            passed = False
            while attempts <= retry_cap:
                attempts += 1
                if mode == "mock":
                    outcome = outcomes[min(attempts - 1, len(outcomes) - 1)] if outcomes else "pass"
                    passed = outcome == "pass"
                else:
                    proc = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
                    passed = proc.returncode == 0
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="validator_result",
                    payload={"validator_id": name, "attempt": attempts, "passed": passed},
                    actor={"kind": "validator", "role": name},
                )
                if passed or attempts >= retry_cap + 1:
                    break
            checks.append(
                QualityCheck(
                    name=name,
                    status="pass" if passed else "fail",
                    details=f"attempts={attempts}",
                )
            )
        return checks

    def _handle_quarantine(
        self,
        run_root: Path,
        run_id: UUID,
        stage: str,
        validator_ids: list[str],
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        config: dict[str, Any],
    ) -> list[str]:
        if not validator_ids:
            return []
        created: list[str] = []
        existing = {artifact.artifact_id for artifact in store.list_artifacts()}
        for validator_id in validator_ids:
            for step in ["patch", "reroute", "hitl", "change_request"]:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={"validator_id": validator_id, "escalation_step": step},
                )
            if config.get("auto_change_request", True):
                payload = ChangeRequestPayload(
                    change_id=f"CR-QUARANTINE-{validator_id}",
                    description=f"Validator {validator_id} exceeded retry budget",
                    affected_entities=[validator_id],
                    rationale="Deterministic retry budget exceeded; escalation required",
                    approvals=[],
                    status="proposed",
                    evidence=[],
                )
                artifact_id = f"change_request_{payload.change_id}_v1"
                if artifact_id in existing:
                    continue
                self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "change_request",
                    artifact_id,
                    payload.model_dump(),
                    parents=[],
                    stage=stage,
                )
                created.append(artifact_id)
                existing.add(artifact_id)
        return created

    def _build_release_bundle(
        self,
        repo_root: Path,
        repo_snapshot: RepoSnapshotPayload,
        build_provenance: list[dict[str, object]],
        config: dict[str, Any],
    ) -> ReleaseBundlePayload:
        def _hash_file(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        artifacts: list[ReleaseArtifact] = []
        bundle_cfg = config.get("release_bundle", {})
        include_paths: list[str] = []
        if isinstance(bundle_cfg, dict):
            include_paths = [str(p) for p in bundle_cfg.get("include_paths", []) if isinstance(p, str)]
        if include_paths:
            for pattern in include_paths:
                for path in repo_root.glob(pattern):
                    if path.is_file():
                        rel = str(path.relative_to(repo_root)).replace("\\", "/")
                        artifacts.append(
                            ReleaseArtifact(
                                name=path.name,
                                path=rel,
                                checksum=_hash_file(path),
                            )
                        )
        else:
            for rel_path in repo_snapshot.changed_files:
                full_path = repo_root / rel_path
                checksum = repo_snapshot.file_hashes.get(rel_path)
                if checksum is None and full_path.exists():
                    checksum = _hash_file(full_path)
                if checksum is None:
                    continue
                artifacts.append(
                    ReleaseArtifact(
                        name=Path(rel_path).name,
                        path=rel_path,
                        checksum=checksum,
                    )
                )
        notes = ["Release bundle assembled from integration workspace"]
        provenance = {
            "build_entrypoints": build_provenance,
            "source_head_commit": repo_snapshot.head_commit,
        }
        return ReleaseBundlePayload(version="v1", artifacts=artifacts, notes=notes, provenance=provenance)

    def _integration_order(
        self, work_plan: WorkPlanPayload | WorkPlanPayloadV2, patchsets: list[PatchsetPayload]
    ) -> list[PatchsetPayload]:
        task_index = {task.task_id: idx for idx, task in enumerate(work_plan.tasks)}
        lane_index = {lane.lane_id: idx for idx, lane in enumerate(work_plan.lanes)}

        def _task_key(patchset: PatchsetPayload) -> tuple[int, int, int, str]:
            task_id = None
            if patchset.patchset_id.startswith("patchset_"):
                parts = patchset.patchset_id.split("_")
                if len(parts) > 1:
                    task_id = parts[1]
            if task_id and task_id in task_index:
                lane_order = lane_index.get(patchset.lane_id or "", 10_000)
                return (0, task_index[task_id], lane_order, patchset.patchset_id)
            lane_order = lane_index.get(patchset.lane_id or "", 10_000)
            return (1, lane_order, 10_000, patchset.patchset_id)

        return sorted(patchsets, key=_task_key)

    def _repair_patchsets(
        self,
        patchsets: list[PatchsetPayload],
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        patchset_limits: PatchsetLimits,
        run_root: Path,
        run_id: UUID,
        stage: str,
        lane_id: str | None,
        retry_cap: int,
        repair_candidates: list[PatchsetPayload] | None = None,
        approved_patchsets: set[str] | None = None,
    ) -> tuple[list[PatchsetPayload], TestResultsPayload, QualityReportsPayload]:
        candidates: list[PatchsetPayload] = []
        if repair_candidates:
            candidates.extend(repair_candidates)
        if not candidates:
            noop = PatchsetPayload(
                patchset_id=f"repair_{lane_id or 'integration'}_noop_v1",
                lane_id=lane_id,
                base_commit=repo_context.base_commit,
                changes=[],
                apply_order=[],
                maps_to_requirements=[],
                maps_to_acceptance_tests=[],
                semantic_change=False,
                change_request_ref=None,
                operation_count=0,
                bytes_changed=0,
            )
            larger = PatchsetPayload(
                patchset_id=f"repair_{lane_id or 'integration'}_large_v1",
                lane_id=lane_id,
                base_commit=repo_context.base_commit,
                changes=[
                    PatchChange(
                        path=f"generated/repair_{lane_id or 'integration'}.txt",
                        action="add",
                        after="repair\n" * 3,
                    )
                ],
                apply_order=[0],
                maps_to_requirements=[],
                maps_to_acceptance_tests=[],
                semantic_change=False,
                change_request_ref=None,
                operation_count=1,
                bytes_changed=len("repair\n" * 3),
            )
            candidates.extend([noop, larger])
        passing: list[tuple[PatchsetPayload, TestResultsPayload, QualityReportsPayload]] = []
        for candidate in candidates:
            candidate.operation_count = patchset_operation_count(candidate)
            candidate.bytes_changed = patchset_bytes_changed(candidate)
            if (
                candidate.operation_count > patchset_limits.max_operations
                or candidate.bytes_changed > patchset_limits.max_bytes
            ):
                continue
            candidate_checkout = run_root / f"repair_{candidate.patchset_id}"
            if candidate_checkout.exists():
                shutil.rmtree(candidate_checkout)
            shutil.copytree(repo_context.repo_root, candidate_checkout)
            context = repo_context.model_copy(update={"repo_root": str(candidate_checkout)})
            self._apply_patchsets_to_checkout(
                candidate_checkout,
                patchsets + [candidate],
                context,
                patchset_limits,
                run_root,
                run_id,
                stage,
                lane_id=lane_id,
                approved_patchsets=approved_patchsets,
            )
            test_results, quality_reports, _ = self._run_validators(context, run_root, run_id, stage, retry_cap)
            if test_results.overall_status == "PASS" and quality_reports.overall_status == "PASS":
                passing.append((candidate, test_results, quality_reports))
        if not passing:
            raise RuntimeError("No passing repair candidate")
        chosen = select_smallest_patchset([item[0] for item in passing])
        for candidate, test_results, quality_reports in passing:
            if candidate.patchset_id == chosen.patchset_id:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={
                        "repair_selection": chosen.patchset_id,
                        "operation_count": chosen.operation_count,
                        "bytes_changed": chosen.bytes_changed,
                        "lane_id": lane_id,
                    },
                )
                return patchsets + [candidate], test_results, quality_reports
        raise RuntimeError("Repair selection failed")

    def _apply_json_repairs(
        self,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        run_id: UUID,
        artifact_id: str,
        candidates: list[dict[str, Any]],
        stage: str,
    ) -> str:
        applied: list[tuple[str, list[dict[str, Any]], int]] = []
        for idx, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                continue
            ops = candidate.get("ops") or candidate.get("patch")
            if not isinstance(ops, list):
                continue
            new_id = str(
                candidate.get("artifact_id")
                or candidate.get("new_artifact_id")
                or f"{artifact_id}_repair_{idx}"
            )
            try:
                path = store.patch_artifact(
                    artifact_id=artifact_id,
                    new_artifact_id=new_id,
                    patch_ops=ops,
                    run_id=run_id,
                )
            except Exception as exc:
                self._event(
                    workspace.run_root,
                    run_id,
                    stage=stage,
                    event_type="error",
                    payload={"json_repair_failed": new_id, "error": str(exc), "artifact_id": artifact_id},
                )
                continue
            envelope = store.read_artifact(new_id)
            workspace.index_artifact(
                path,
                envelope.artifact_type,
                envelope.schema_version,
                {"stage": stage, "role": "patcher"},
            )
            applied.append((new_id, ops, idx))
        if not applied:
            return artifact_id
        scored = []
        for new_id, ops, idx in applied:
            payload = json.dumps(ops, sort_keys=True, separators=(",", ":"))
            scored.append((len(ops), len(payload), idx, new_id))
        scored.sort()
        chosen_id = scored[0][3]
        self._event(
            workspace.run_root,
            run_id,
            stage=stage,
            event_type="decision",
            payload={"json_repair_selection": chosen_id, "artifact_id": artifact_id},
        )
        return chosen_id

    def _run_lane_pipeline(
        self,
        lane: WorkLane,
        tasks: list[WorkTask | WorkTaskV2],
        plan: PlanPackage,
        repo_context: RepoContextPayload | RepoContextPayloadV2,
        work_plan_ref: str | None,
        patchset_limits: PatchsetLimits,
        run_root: Path,
        run_id: UUID,
        store: ImplementationArtifactStore,
        workspace: WorkspaceManager,
        change_request_refs: set[str],
        change_request_decisions: set[str],
        writer_role: str,
        reviewer_role: str,
        approved_patchsets: set[str],
        config: dict[str, Any],
        retry_cap: int,
    ) -> LaneResult:
        lane_handoffs: list[str] = []
        patchsets: list[PatchsetPayload] = []
        for task in tasks:
            patchset = self._create_patchset_for_task(task, repo_context.base_commit)
            patchset.operation_count = patchset_operation_count(patchset)
            patchset.bytes_changed = patchset_bytes_changed(patchset)
            if (
                patchset.operation_count > patchset_limits.max_operations
                or patchset.bytes_changed > patchset_limits.max_bytes
            ):
                raise RuntimeError("Patchset exceeds configured size limits")
            validate_patchset_semantics(patchset, plan, change_request_refs, change_request_decisions)
            validate_forbidden_paths(patchset, repo_context, allow_protected=patchset.patchset_id in approved_patchsets)
            patchsets.append(patchset)
            self._write_artifact(
                store,
                workspace,
                run_id,
                "patchset",
                patchset.patchset_id,
                patchset.model_dump(),
                parents=[work_plan_ref] if work_plan_ref else [],
                stage=f"lane_{lane.lane_id}_generate",
                role=writer_role,
            )
        lane_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage=f"lane_{lane.lane_id}_generate",
                to_stage=f"lane_{lane.lane_id}_validate",
                objectives=[f"Generate patchsets for {lane.lane_id}"],
                completed_refs=[p.patchset_id for p in patchsets],
                pending_blockers=[],
                next_actions=["Validate lane patchsets"],
                evidence=[EvidencePointer(artifact_ref=p.patchset_id, json_pointer="/") for p in patchsets],
            )
        )
        lane_checkout = run_root / f"lane_{lane.lane_id}_checkout"
        if lane_checkout.exists():
            shutil.rmtree(lane_checkout)
        shutil.copytree(repo_context.repo_root, lane_checkout)
        lane_context = repo_context.model_copy(update={"repo_root": str(lane_checkout)})
        self._apply_patchsets_to_checkout(
            lane_checkout,
            patchsets,
            lane_context,
            patchset_limits,
            run_root,
            run_id,
            stage=f"lane_{lane.lane_id}_validate",
            lane_id=lane.lane_id,
            approved_patchsets=approved_patchsets,
        )
        test_results, quality_reports, quarantined = self._run_validators(
            lane_context, run_root, run_id, f"lane_{lane.lane_id}_validate", retry_cap
        )
        if quarantined:
            self._handle_quarantine(
                run_root,
                run_id,
                stage=f"lane_{lane.lane_id}_validate",
                validator_ids=quarantined,
                store=store,
                workspace=workspace,
                config=config,
            )
        test_artifact_id = f"test_results_{lane.lane_id}_v1"
        quality_artifact_id = f"quality_reports_{lane.lane_id}_v1"
        self._write_artifact(
            store,
            workspace,
            run_id,
            "test_results",
            test_artifact_id,
            test_results.model_dump(),
            parents=[],
            stage=f"lane_{lane.lane_id}_validate",
        )
        self._write_artifact(
            store,
            workspace,
            run_id,
            "quality_reports",
            quality_artifact_id,
            quality_reports.model_dump(),
            parents=[],
            stage=f"lane_{lane.lane_id}_validate",
        )
        lane_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage=f"lane_{lane.lane_id}_validate",
                to_stage=f"lane_{lane.lane_id}_review",
                objectives=[f"Validate patchsets for {lane.lane_id}"],
                completed_refs=[test_artifact_id, quality_artifact_id],
                pending_blockers=[],
                next_actions=["Review lane outputs"],
                evidence=[
                    EvidencePointer(artifact_ref=test_artifact_id, json_pointer="/"),
                    EvidencePointer(artifact_ref=quality_artifact_id, json_pointer="/"),
                ],
            )
        )
        findings: list[ReviewFinding] = []
        if test_results.overall_status == "FAIL":
            findings.append(
                ReviewFinding(
                    id=f"RF_{lane.lane_id}_1",
                    severity="high",
                    summary="Lane validation failed",
                    evidence=[EvidencePointer(artifact_ref=test_artifact_id, json_pointer="/")],
                    violated_gate="validation",
                )
            )
        review_findings = ReviewFindingsPayload(findings=findings)
        review_artifact_id = f"review_findings_{lane.lane_id}_v1"
        self._write_artifact(
            store,
            workspace,
            run_id,
            "review_findings",
            review_artifact_id,
            review_findings.model_dump(),
            parents=[],
            stage=f"lane_{lane.lane_id}_review",
            role=reviewer_role,
        )
        lane_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage=f"lane_{lane.lane_id}_review",
                to_stage=f"lane_{lane.lane_id}_patch",
                objectives=[f"Review patchsets for {lane.lane_id}"],
                completed_refs=[review_artifact_id],
                pending_blockers=[],
                next_actions=["Apply repairs if needed"],
                evidence=[EvidencePointer(artifact_ref=review_artifact_id, json_pointer="/")],
            )
        )
        if test_results.overall_status == "FAIL":
            existing_ids = {p.patchset_id for p in patchsets}
            try:
                patchsets, test_results, quality_reports = self._repair_patchsets(
                    patchsets,
                    repo_context,
                    patchset_limits,
                    run_root,
                    run_id,
                    stage=f"lane_{lane.lane_id}_patch",
                    lane_id=lane.lane_id,
                    retry_cap=retry_cap,
                    repair_candidates=None,
                    approved_patchsets=approved_patchsets,
                )
            except RuntimeError:
                if quarantined:
                    self._handle_quarantine(
                        run_root,
                        run_id,
                        stage=f"lane_{lane.lane_id}_patch",
                        validator_ids=quarantined,
                        store=store,
                        workspace=workspace,
                        config=config,
                    )
                raise
            for patchset in patchsets:
                if patchset.patchset_id in existing_ids:
                    continue
                self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "patchset",
                    patchset.patchset_id,
                    patchset.model_dump(),
                    parents=[work_plan_ref] if work_plan_ref else [],
                    stage=f"lane_{lane.lane_id}_patch",
                    role=writer_role,
                )
            test_artifact_id = f"test_results_{lane.lane_id}_v2"
            quality_artifact_id = f"quality_reports_{lane.lane_id}_v2"
            self._write_artifact(
                store,
                workspace,
                run_id,
                "test_results",
                test_artifact_id,
                test_results.model_dump(),
                parents=[],
                stage=f"lane_{lane.lane_id}_patch",
            )
            self._write_artifact(
                store,
                workspace,
                run_id,
                "quality_reports",
                quality_artifact_id,
                quality_reports.model_dump(),
                parents=[],
                stage=f"lane_{lane.lane_id}_patch",
            )
        lane_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage=f"lane_{lane.lane_id}_patch",
                to_stage=f"lane_{lane.lane_id}_complete",
                objectives=[f"Finalize lane {lane.lane_id}"],
                completed_refs=[p.patchset_id for p in patchsets],
                pending_blockers=[],
                next_actions=["Integrate lane outputs"],
                evidence=[EvidencePointer(artifact_ref=p.patchset_id, json_pointer="/") for p in patchsets],
            )
        )
        return LaneResult(
            lane_id=lane.lane_id,
            patchsets=patchsets,
            test_results=test_results,
            quality_reports=quality_reports,
            review_findings=review_findings,
            stage_handoffs=lane_handoffs,
        )

    def _reproducibility_check(
        self,
        run_root: Path,
        repo_context: RepoContextPayload | RepoContextPayloadV2 | RepoContextPayloadV21,
        patchsets: list[PatchsetPayload],
        patchset_limits: PatchsetLimits,
        expected_snapshot: RepoSnapshotPayload,
        run_id: UUID,
        approved_patchsets: set[str] | None = None,
    ) -> None:
        repro_checkout = run_root / "repro_checkout"
        if repro_checkout.exists():
            shutil.rmtree(repro_checkout)
        shutil.copytree(repo_context.repo_root, repro_checkout)
        changed_files, file_hashes = self._apply_patchsets_to_checkout(
            repro_checkout,
            patchsets,
            repo_context.model_copy(update={"repo_root": str(repro_checkout)}),
            patchset_limits,
            run_root,
            run_id,
            stage="freeze",
            approved_patchsets=approved_patchsets,
        )
        if set(changed_files) != set(expected_snapshot.changed_files):
            raise RuntimeError("Reproducibility check failed: changed file set mismatch")
        for path, expected_hash in expected_snapshot.file_hashes.items():
            if file_hashes.get(path) != expected_hash:
                raise RuntimeError("Reproducibility check failed: hash mismatch")

    def _run_v21(
        self,
        plan_path: Path,
        repo_context_path: Path,
        workspace_context_path: Path,
        config_path: Path,
        handoff_bundle_path: Path,
        config_snapshot_path: Path,
        *,
        run_id: UUID,
        run_root: Path,
        config: dict[str, Any],
        config_raw: str,
        roles: dict[str, RoleConfig],
        role_sets: dict[str, set[str]],
        role_assignments: dict[str, str],
        plan_content_hash: str,
    ) -> ImplementationRunResult:
        writer_role = role_assignments["writer"]
        reviewer_role = role_assignments["reviewer"]
        judge_role = role_assignments["judge"]

        plan = self._load_plan_package(plan_path)
        handoff_bundle: PlanningHandoffBundlePayload | None = None
        try:
            handoff_bundle = PlanningHandoffBundlePayload.model_validate_json(
                handoff_bundle_path.read_text(encoding="utf-8")
            )
        except Exception:
            handoff_bundle = None
        repo_context = RepoContextPayloadV21.model_validate_json(repo_context_path.read_text(encoding="utf-8"))
        workspace_context = WorkspaceContextPayload.model_validate_json(
            workspace_context_path.read_text(encoding="utf-8")
        )
        planning_inputs_root = plan_path.parent
        hash_spec = self._load_hash_spec(planning_inputs_root)
        hash_spec_version = hash_spec.hash_spec_version
        config_snapshot_payload = json.loads(config_snapshot_path.read_text(encoding="utf-8"))
        repo_snapshot_path = planning_inputs_root / "repo_snapshot.json"
        if repo_snapshot_path.exists():
            repo_snapshot_payload = json.loads(repo_snapshot_path.read_text(encoding="utf-8"))
        else:
            try:
                snapshot = capture_repo_snapshot(Path(repo_context.repo_root))
            except Exception:
                snapshot = RepoSnapshot(
                    commit_sha="unknown",
                    branch="unknown",
                    dirty=True,
                    tree_hash=self._compute_tree_hash(Path(repo_context.repo_root)),
                )
            repo_snapshot_payload = snapshot.model_dump()
            repo_snapshot_path = self._ensure_input_json(
                planning_inputs_root, "repo_snapshot.json", repo_snapshot_payload
            )
        env_snapshot_payload: dict[str, Any] | None = None
        env_snapshot_path = planning_inputs_root / "env_snapshot.json"
        if env_snapshot_path.exists():
            env_snapshot_payload = json.loads(env_snapshot_path.read_text(encoding="utf-8"))

        compiler_inputs = {
            "schema_version": "compiler_inputs.v1",
            "plan_content_hash": plan_content_hash,
            "repo_snapshot_hash": artifact_hash(repo_snapshot_payload),
            "config_snapshot_hash": artifact_hash(config_snapshot_payload),
            "env_snapshot_hash": artifact_hash(env_snapshot_payload) if env_snapshot_payload else None,
            "hash_spec_version": hash_spec_version,
        }
        compiler_inputs_hashes = [
            plan_content_hash,
            compiler_inputs["repo_snapshot_hash"],
            compiler_inputs["config_snapshot_hash"],
        ]
        if compiler_inputs["env_snapshot_hash"]:
            compiler_inputs_hashes.append(compiler_inputs["env_snapshot_hash"])
        existing_compiler_inputs = self._read_impl_json(run_root, "compiler_inputs.json")
        compiler_inputs_mismatch = (
            existing_compiler_inputs is not None and existing_compiler_inputs != compiler_inputs
        )
        resume_mode = self._normalize_resume_mode(config.get("resume_mode"))
        vnext_enforce_gates = bool(config.get("vnext_enforce_gates", True))
        vnext_milestone_id = plan.milestones[0].id if plan.milestones else "MS-001"

        workspace = WorkspaceManager(
            run_root,
            workspace_context,
            schema_version=self._schema_version,
            run_id=str(run_id),
        )
        store = ImplementationArtifactStore(self.storage_root, run_id)

        policy_knobs = {
            "patchset_limits": config.get("patchset_limits", {}),
            "retry_policy": config.get("retry_policy", {}),
            "tool_policy": config.get("tool_policy", {}),
        }
        patchset_limits = PatchsetLimits(
            max_operations=int(config.get("patchset_limits", {}).get("max_operations", 25)),
            max_bytes=int(config.get("patchset_limits", {}).get("max_bytes", 2000)),
            max_operations_total=(
                int(config.get("patchset_limits", {}).get("max_operations_total"))
                if config.get("patchset_limits", {}).get("max_operations_total") is not None
                else None
            ),
        )
        guardrails = self._extract_guardrails(plan, config)

        v2_inputs = {
            "execution_profile_catalog": self._load_config_payload(config, config_path, "execution_profile_catalog"),
            "tool_registry": self._load_config_payload(config, config_path, "tool_registry"),
            "tool_probe_results": self._load_config_payload(config, config_path, "tool_probe_results"),
            "expectation_registry": self._load_config_payload(config, config_path, "expectation_registry"),
            "assumption_registry": self._load_config_payload(config, config_path, "assumption_registry"),
            "program_graph": self._load_config_payload(config, config_path, "program_graph"),
            "verification_plan": self._load_config_payload(config, config_path, "verification_plan"),
            "work_graph": self._load_config_payload(config, config_path, "work_graph"),
            "job_specs": self._load_config_payload(config, config_path, "job_specs"),
            "change_intents": self._load_config_payload(config, config_path, "change_intents"),
            "interface_contracts": self._load_config_payload(config, config_path, "interface_contracts"),
            "patchsets": self._load_config_payload(config, config_path, "patchsets"),
        }

        artifact_refs: dict[str, str] = {}
        stage_handoffs: list[str] = []

        # Intake stage
        self._event(run_root, run_id, stage="intake", event_type="stage_transition", payload={})
        self._execute_tool_calls(run_root, run_id, "intake", repo_context=repo_context)
        artifact_refs["plan_package_final"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "plan_package_final",
            "plan_package_final_v21",
            plan.model_dump(by_alias=True),
            parents=[],
            stage="intake",
        )
        if handoff_bundle is not None:
            artifact_refs["planning_handoff_bundle"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "planning_handoff_bundle",
                "planning_handoff_bundle_v21",
                handoff_bundle.model_dump(),
                parents=[artifact_refs["plan_package_final"]],
                stage="intake",
            )
        artifact_refs["repo_context"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "repo_context",
            "repo_context_v21",
            repo_context.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )
        artifact_refs["workspace_context"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "workspace_context",
            "workspace_context_v21",
            workspace_context.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )

        change_request_refs, change_request_artifacts, change_request_decisions = self._load_change_requests(
            config, store, workspace, run_id, stage="intake", decision_role=judge_role
        )
        for artifact_id in change_request_artifacts:
            artifact_refs[artifact_id] = artifact_id

        profile_payload = v2_inputs.get("execution_profile_catalog")
        execution_profile_catalog = (
            ExecutionProfileCatalogPayload.model_validate(profile_payload)
            if profile_payload
            else self._default_profile_catalog(schema_version=IMPL_SCHEMA_VERSION_V21)
        )
        execution_profile_catalog.schema_version = self._schema_version
        artifact_refs["execution_profile_catalog"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "execution_profile_catalog",
            "execution_profile_catalog_v21",
            execution_profile_catalog.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )

        tool_registry_payload = v2_inputs.get("tool_registry")
        tool_registry = (
            ToolRegistryPayload.model_validate(tool_registry_payload)
            if tool_registry_payload
            else ToolRegistryPayload(schema_version=self._schema_version, registry_id="default", tools=[])
        )
        artifact_refs["tool_registry"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "tool_registry",
            "tool_registry_v21",
            tool_registry.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )

        tool_probe_payload = v2_inputs.get("tool_probe_results")
        if tool_probe_payload:
            tool_probe_results = ToolProbeResultsPayload.model_validate(tool_probe_payload)
            artifact_refs["tool_probe_results"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "tool_probe_results",
                "tool_probe_results_v21",
                tool_probe_results.model_dump(),
                parents=[artifact_refs["tool_registry"]],
                stage="intake",
            )

        expectation_payload = v2_inputs.get("expectation_registry")
        expectation_registry = (
            ExpectationRegistryPayload.model_validate(expectation_payload)
            if expectation_payload
            else self._compile_expectation_registry_from_plan(plan, schema_version=self._schema_version)
        )
        research_contracts: ResearchContractsPayload | None = None
        research_contract_ids: list[tuple[str, str]] = []
        research_payload = v2_inputs.get("research_contracts")
        if research_payload:
            research_contracts = ResearchContractsPayload.model_validate(research_payload)
            existing_ids = {exp.expectation_id for exp in expectation_registry.expectations}
            for idx, contract in enumerate(research_contracts.contracts, start=1):
                safe_id = self._safe_id(contract.contract_id, f"contract_{idx}")
                exp_id = f"EXP-RESEARCH-{safe_id}"
                research_contract_ids.append((contract.contract_id, safe_id))
                if exp_id in existing_ids:
                    continue
                expectation_registry.expectations.append(
                    Expectation(
                        expectation_id=exp_id,
                        level="plan",
                        source={"type": "research_contract", "id": contract.contract_id},
                        subject={"kind": "research", "artifact_type": "experiment_results", "name": contract.contract_id},
                        oracle={"type": "exists"},
                        tolerance=None,
                        inputs=[],
                        parents=[],
                        children=[],
                        must_exist_before_remote_ops=False,
                    )
                )
                existing_ids.add(exp_id)
        if not expectation_payload and repo_context.default_expectations:
            existing_ids = {exp.expectation_id for exp in expectation_registry.expectations}
            extras = [exp for exp in repo_context.default_expectations if exp.expectation_id not in existing_ids]
            extras.sort(key=lambda exp: exp.expectation_id)
            expectation_registry.expectations.extend(extras)
        artifact_refs["expectation_registry"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "expectation_registry",
            "expectation_registry_v21",
            expectation_registry.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )

        assumption_payload = v2_inputs.get("assumption_registry")
        assumption_registry = (
            AssumptionRegistryPayload.model_validate(assumption_payload)
            if assumption_payload
            else compile_assumption_registry(plan)
        )
        artifact_refs["assumption_registry"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "assumption_registry",
            "assumption_registry_v21",
            assumption_registry.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )

        static_sources = {"O", "K"}
        evidence_entries: list[EvidenceIndexEntry] = []
        for exp in expectation_registry.expectations:
            status = "unknown"
            evidence = []
            if exp.source.type in static_sources:
                status = "pass"
                evidence = [EvidencePointer(artifact_ref=artifact_refs["plan_package_final"], json_pointer="/")]
            evidence_entries.append(
                EvidenceIndexEntry(
                    expectation_id=exp.expectation_id,
                    status=status,
                    evidence_pointers=evidence,
                    waiver_approvals=[],
                )
            )
        evidence_index = EvidenceIndexPayload(schema_version=self._schema_version, evidence=evidence_entries)
        evidence_index_ref = self._write_artifact(
            store,
            workspace,
            run_id,
            "evidence_index",
            "evidence_index_v21",
            evidence_index.model_dump(),
            parents=[artifact_refs["expectation_registry"]],
            stage="intake",
        )
        artifact_refs["evidence_index"] = evidence_index_ref

        program_graph_payload = v2_inputs.get("program_graph")
        program_graph = (
            ProgramGraphPayload.model_validate(program_graph_payload)
            if program_graph_payload
            else ProgramGraphCompiler().compile(plan, expectation_registry)
        )
        artifact_refs["program_graph"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "program_graph",
            "program_graph_v21",
            program_graph.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )

        interface_contracts_payload = v2_inputs.get("interface_contracts")
        if interface_contracts_payload:
            interface_contracts = InterfaceContractsPayload.model_validate(interface_contracts_payload)
            artifact_refs["interface_contracts"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "interface_contracts",
                "interface_contracts_v21",
                interface_contracts.model_dump(),
                parents=[artifact_refs["plan_package_final"]],
                stage="intake",
            )

        create_manifest(
            run_root,
            ManifestInput(
                run_id=run_id,
                code_version=self._git_code_version(),
                schema_versions={"implementation": self._schema_version},
                run_config_version=str(config.get("version", "")),
                stage_machine_version=str(config.get("stage_machine_version", "2.1.0")),
                validator_suite_version=str(config.get("validator_suite_version", "2.1.0")),
                model_portfolio=self._model_portfolio(roles),
                toolchain_versions={"python": "3.12"},
                policy_knobs=policy_knobs,
                base_commit=repo_context.base_commit,
                head_commit=repo_context.head_commit,
                prompt_pack_hash=str(config.get("prompt_pack_hash", "")),
                config_raw=config_raw,
                capability_level="v2_1",
                artifact_refs={"execution_profile_catalog": artifact_refs["execution_profile_catalog"]},
            ),
        )

        context_refs = [
            artifact_refs["plan_package_final"],
            artifact_refs["execution_profile_catalog"],
            artifact_refs["tool_registry"],
            artifact_refs["expectation_registry"],
            artifact_refs["program_graph"],
            artifact_refs["evidence_index"],
            artifact_refs["assumption_registry"],
        ]
        intake_pack_id = self._write_context_pack(
            workspace,
            run_id,
            stage="intake",
            role="engine",
            artifact_refs=context_refs,
            evidence=[EvidencePointer(artifact_ref=artifact_refs["plan_package_final"], json_pointer="/")],
            required_refs=context_refs,
        )
        if self._schema_version.startswith("2.1"):
            intake_pack_path = workspace.context_packs_root / f"{intake_pack_id}.json"
            intake_pack = ContextPackPayload.model_validate_json(intake_pack_path.read_text(encoding="utf-8"))
            validate_context_pack_adequacy(intake_pack, context_refs)

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "intake",
                "work_planning",
                objectives=["Prepare program/work graphs"],
                completed_refs=list(artifact_refs.values()),
                pending_blockers=[],
                next_actions=["Compile verification plan"],
                evidence=[EvidencePointer(artifact_ref=artifact_refs["program_graph"], json_pointer="/")],
            )
        )

        # Work planning stage
        self._event(run_root, run_id, stage="work_planning", event_type="stage_transition", payload={})
        verification_payload = v2_inputs.get("verification_plan")
        verification_plan = (
            VerificationPlanPayload.model_validate(verification_payload)
            if verification_payload
            else VerificationPlanCompiler().compile(program_graph, repo_context)
        )
        artifact_refs["verification_plan"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "verification_plan",
            "verification_plan_v21",
            verification_plan.model_dump(),
            parents=[artifact_refs["program_graph"]],
            stage="work_planning",
        )

        work_graph_payload = v2_inputs.get("work_graph")
        job_spec_payloads = v2_inputs.get("job_specs")
        job_specs: list[JobSpecPayload] = []
        if isinstance(job_spec_payloads, dict):
            if "_list" in job_spec_payloads:
                job_specs = [JobSpecPayload.model_validate(item) for item in job_spec_payloads.get("_list", [])]
            else:
                job_specs = [JobSpecPayload.model_validate(item) for item in job_spec_payloads.get("job_specs", [])]
        elif isinstance(job_spec_payloads, list):
            job_specs = [JobSpecPayload.model_validate(item) for item in job_spec_payloads]
        if work_graph_payload:
            work_graph = WorkGraphPayload.model_validate(work_graph_payload)
        else:
            profile_id = (
                execution_profile_catalog.profiles[0].profile_id
                if execution_profile_catalog.profiles
                else "legacy_v1"
            )
            build_result = WorkGraphBuilder().build(
                program_graph=program_graph,
                verification_plan=verification_plan,
                tool_registry=tool_registry,
                assumption_registry=assumption_registry,
                repo_context=repo_context,
                profile_id=profile_id,
            )
            work_graph = build_result.work_graph
            if not job_specs:
                job_specs = build_result.job_specs

        if job_specs:
            job_ids_in_graph = {job_id for task in work_graph.tasks for job_id in task.job_specs}
            if job_ids_in_graph != {spec.job_id for spec in job_specs}:
                tool_probe_task_ids = [task.task_id for task in work_graph.tasks if task.kind == "tool_probe"]
                existing_task_ids = {task.task_id for task in work_graph.tasks}
                for spec in sorted(job_specs, key=lambda item: item.job_id):
                    if spec.job_id in job_ids_in_graph:
                        continue
                    base_task_id = f"task_{spec.job_id}"
                    task_id = base_task_id
                    suffix = 1
                    while task_id in existing_task_ids:
                        suffix += 1
                        task_id = f"{base_task_id}_{suffix}"
                    existing_task_ids.add(task_id)
                    budget = None
                    if spec.budget is not None:
                        budget = WorkGraphBudget(
                            max_runtime_sec=spec.budget.max_runtime_sec,
                            max_cost_usd=spec.budget.max_cost_usd,
                            max_calls=spec.budget.max_calls,
                        )
                    work_graph.tasks.append(
                        WorkGraphTask(
                            task_id=task_id,
                            kind=spec.job_type,
                            deps=list(tool_probe_task_ids),
                            profile_id=spec.profile_id,
                            program_check_ids=[],
                            expectation_ids=list(spec.expected_expectation_ids),
                            assumption_ids=list(spec.assumption_ids),
                            surfaces=list(spec.surfaces),
                            job_specs=[spec.job_id],
                            risk_class="low",
                            budget=budget,
                            rollback_or_comp_plan_ref=None,
                            priority=spec.priority,
                        )
                    )

        artifact_refs["work_graph"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "work_graph",
            "work_graph_v21",
            work_graph.model_dump(),
            parents=[artifact_refs["verification_plan"]],
            stage="work_planning",
        )

        job_spec_map: dict[str, JobSpecPayload] = {}
        written_job_specs: set[str] = set()
        for spec in job_specs:
            artifact_id = f"job_spec_{spec.job_id}"
            job_spec_map[spec.job_id] = spec
            written_job_specs.add(spec.job_id)
            self._write_artifact(
                store,
                workspace,
                run_id,
                "job_spec",
                artifact_id,
                spec.model_dump(),
                parents=[artifact_refs["work_graph"]],
                stage="work_planning",
                role=writer_role,
            )

        work_graph_events: list[dict[str, Any]] = []
        for task in work_graph.tasks:
            work_graph_events.append(
                {
                    "timestamp": datetime.now(UTC),
                    "task_id": task.task_id,
                    "event": "created",
                    "status": "pending",
                }
            )
        work_graph_events_payload = WorkGraphEventsPayload(
            schema_version=self._schema_version,
            events=work_graph_events,
        )
        artifact_refs["work_graph_events"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "work_graph_events",
            "work_graph_events_v21",
            work_graph_events_payload.model_dump(),
            parents=[artifact_refs["work_graph"]],
            stage="work_planning",
        )

        if research_contracts and research_contract_ids:
            profile_id = (
                execution_profile_catalog.profiles[0].profile_id
                if execution_profile_catalog.profiles
                else "legacy_v1"
            )
            for idx, (_contract_id, safe_id) in enumerate(research_contract_ids, start=1):
                exp_id = f"EXP-RESEARCH-{safe_id}"
                job_id = f"JOB-RESEARCH-{safe_id}"
                if job_id not in job_spec_map:
                    job_spec_map[job_id] = JobSpecPayload(
                        job_id=job_id,
                        job_type="research",
                        profile_id=profile_id,
                        inputs=[],
                        idempotency_key=f"plan:{plan_content_hash}/research:{safe_id}",
                        expected_expectation_ids=[exp_id],
                        priority=0,
                    )

        for job_id, spec in job_spec_map.items():
            if job_id in written_job_specs:
                continue
            artifact_id = f"job_spec_{spec.job_id}"
            self._write_artifact(
                store,
                workspace,
                run_id,
                "job_spec",
                artifact_id,
                spec.model_dump(),
                parents=[artifact_refs["work_graph"]],
                stage="work_planning",
                role=writer_role,
            )
            written_job_specs.add(job_id)

        if job_spec_map:
            job_ids_in_graph = {job_id for task in work_graph.tasks for job_id in task.job_specs}
            existing_task_ids = {task.task_id for task in work_graph.tasks}
            tool_probe_task_ids = [task.task_id for task in work_graph.tasks if task.kind == "tool_probe"]
            for spec in sorted(job_spec_map.values(), key=lambda item: item.job_id):
                if spec.job_id in job_ids_in_graph:
                    continue
                base_task_id = f"task_{spec.job_id}"
                task_id = base_task_id
                suffix = 1
                while task_id in existing_task_ids:
                    suffix += 1
                    task_id = f"{base_task_id}_{suffix}"
                existing_task_ids.add(task_id)
                budget = None
                if spec.budget is not None:
                    budget = WorkGraphBudget(
                        max_runtime_sec=spec.budget.max_runtime_sec,
                        max_cost_usd=spec.budget.max_cost_usd,
                        max_calls=spec.budget.max_calls,
                    )
                work_graph.tasks.append(
                    WorkGraphTask(
                        task_id=task_id,
                        kind=spec.job_type,
                        deps=list(tool_probe_task_ids),
                        profile_id=spec.profile_id,
                        program_check_ids=[],
                        expectation_ids=list(spec.expected_expectation_ids),
                        assumption_ids=list(spec.assumption_ids),
                        surfaces=list(spec.surfaces),
                        job_specs=[spec.job_id],
                        risk_class="low",
                        budget=budget,
                        rollback_or_comp_plan_ref=None,
                        priority=spec.priority,
                    )
                )

        repo_snapshot = RepoSnapshot.model_validate(repo_snapshot_payload)
        required_expectations: set[str] = set()
        for spec in job_spec_map.values():
            if spec.job_type == "research":
                continue
            required_expectations.update(spec.expected_expectation_ids)
        vnext_expectation_registry = self._build_vnext_expectation_registry(
            expectation_registry,
            plan_hash=plan_content_hash,
            hash_spec_version=hash_spec_version,
            required_expectations=required_expectations,
        )
        vnext_job_specs, vnext_task_job_map = self._build_vnext_job_specs(
            job_spec_map,
            work_graph,
            repo_snapshot=repo_snapshot,
            env_snapshot_hash=compiler_inputs.get("env_snapshot_hash"),
        )
        vnext_work_graph = self._build_vnext_work_graph(
            work_graph,
            plan_hash=plan_content_hash,
            compiler_inputs_hashes=compiler_inputs_hashes,
        )
        vnext_verification_plan = self._build_vnext_verification_plan(
            job_spec_map,
            milestone_id=vnext_milestone_id,
            plan_hash=plan_content_hash,
            hash_spec_version=hash_spec_version,
        )
        vnext_execution_plan = self._build_vnext_execution_plan(vnext_work_graph, vnext_task_job_map)
        if compiler_inputs_mismatch and resume_mode == "REPLAN_REQUIRED":
            self._write_discrepancy_report(
                run_root,
                expected=existing_compiler_inputs or {},
                actual=compiler_inputs,
                reason="compiler_inputs_mismatch",
            )
            self._emit_vnext_change_request(
                run_root,
                plan_hash=plan_content_hash,
                reason_code="DEP_CONFLICT",
                summary="Compiler inputs changed; replan required.",
                blocked_task_ids=[],
                blocked_job_ids=[],
                blocked_check_ids=[],
                milestones=[vnext_milestone_id],
            )
            raise RuntimeError("Compiler inputs mismatch; replan required.")
        compiled_outputs = self._write_vnext_compiled_artifacts(
            run_root,
            compiler_inputs=compiler_inputs,
            expectation_registry=vnext_expectation_registry,
            verification_plan=vnext_verification_plan,
            work_graph=vnext_work_graph,
            execution_plan=vnext_execution_plan,
            resume_mode=resume_mode,
        )
        vnext_expectation_registry = compiled_outputs.get("expectation_registry") or vnext_expectation_registry
        vnext_verification_plan = compiled_outputs.get("verification_plan") or vnext_verification_plan
        vnext_work_graph = compiled_outputs.get("work_graph") or vnext_work_graph
        vnext_execution_plan = compiled_outputs.get("execution_plan") or vnext_execution_plan
        self._write_vnext_job_specs(run_root, vnext_job_specs)
        append_vnext_event(
            run_root,
            "implementation",
            "EXECUTION_PLANNED",
            run_id=str(run_id),
            plan_hash=plan_content_hash,
            milestone_id=vnext_milestone_id,
            payload={"compiler_inputs": compiler_inputs_hashes},
        )

        lease_events_extra: list[dict[str, Any]] = []
        lease_requirements_extra: list[tuple[str, str]] = []
        patchsets: list[PatchsetPayload] = []

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "work_planning",
                "generate",
                objectives=["Generate patchsets if required"],
                completed_refs=[artifact_refs["work_graph"]],
                pending_blockers=[],
                next_actions=["Patch apply"],
                evidence=[EvidencePointer(artifact_ref=artifact_refs["work_graph"], json_pointer="/")],
            )
        )

        # Generate stage (emit patchsets if provided)
        self._event(run_root, run_id, stage="generate", event_type="stage_transition", payload={})
        patchset_payloads = v2_inputs.get("patchsets")
        if isinstance(patchset_payloads, dict):
            if "_list" in patchset_payloads:
                patchsets = [PatchsetPayload.model_validate(item) for item in patchset_payloads.get("_list", [])]
            else:
                patchsets = [PatchsetPayload.model_validate(item) for item in patchset_payloads.get("patchsets", [])]
        elif isinstance(patchset_payloads, list):
            patchsets = [PatchsetPayload.model_validate(item) for item in patchset_payloads]
        patchset_ids: list[str] = []
        for patchset in patchsets:
            patchset.operation_count = patchset_operation_count(patchset)
            patchset.bytes_changed = patchset_bytes_changed(patchset)
            self._write_artifact(
                store,
                workspace,
                run_id,
                "patchset",
                patchset.patchset_id,
                patchset.model_dump(),
                parents=[artifact_refs["work_graph"]],
                stage="generate",
                role=writer_role,
            )
            patchset_ids.append(patchset.patchset_id)
        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "generate",
                "patch_apply",
                objectives=["Apply patchsets"],
                completed_refs=patchset_ids,
                pending_blockers=[],
                next_actions=["Validation"],
                evidence=[EvidencePointer(artifact_ref=ref, json_pointer="/") for ref in patchset_ids]
                if patchset_ids
                else [EvidencePointer(artifact_ref=artifact_refs["work_graph"], json_pointer="/")],
            )
        )

        # Patch apply stage (apply patchsets with lease enforcement)
        self._event(run_root, run_id, stage="patch_apply", event_type="stage_transition", payload={})
        if patchsets:
            from fnmatch import fnmatch

            lease_manager = LeaseManager()
            leases_path = workspace.canonical_root / "surface_leases.jsonl"
            if not leases_path.exists():
                leases_path.write_text("", encoding="utf-8")
            workspace.index_artifact(
                leases_path,
                "surface_leases_log",
                self._schema_version,
                {"stage": "patch_apply", "role": "engine"},
            )

            def _append_lease(event: SurfaceLeaseEvent) -> None:
                lease_events_extra.append(event.model_dump())
                with leases_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.model_dump(), default=str) + "\n")

            resolver = SurfaceResolver(repo_context.surfaces)
            surface_map = {surface.surface_id: surface for surface in repo_context.surfaces}
            held: list[tuple[str, str, LeaseHolder]] = []
            for patchset in patchsets:
                surface_ids = resolver.resolve_patchset([change.path for change in patchset.changes])
                holder = LeaseHolder(run_id=str(run_id), lane_id=None, task_id=f"patch_apply:{patchset.patchset_id}")
                for surface_id in surface_ids:
                    surface = surface_map.get(surface_id)
                    if surface and surface.change_budget:
                        budget = surface.change_budget
                        changed_paths = [
                            change.path.replace("\\", "/").lstrip("/")
                            for change in patchset.changes
                            if any(
                                fnmatch(change.path.replace("\\", "/").lstrip("/"), pattern)
                                for pattern in surface.path_globs
                            )
                        ]
                        file_count = len(sorted(set(changed_paths)))
                        bytes_changed = 0
                        for change in patchset.changes:
                            rel_path = change.path.replace("\\", "/").lstrip("/")
                            if not any(fnmatch(rel_path, pattern) for pattern in surface.path_globs):
                                continue
                            before = change.before or ""
                            after = change.after or ""
                            if change.action == "add":
                                bytes_changed += len(after)
                            elif change.action == "delete":
                                bytes_changed += len(before)
                            else:
                                bytes_changed += abs(len(after) - len(before))
                        if budget.max_files is not None and file_count > budget.max_files:
                            raise RuntimeError(
                                f"Surface change budget exceeded for {surface_id}: "
                                f"files {file_count} > {budget.max_files}"
                            )
                        if budget.max_bytes is not None and bytes_changed > budget.max_bytes:
                            raise RuntimeError(
                                f"Surface change budget exceeded for {surface_id}: "
                                f"bytes {bytes_changed} > {budget.max_bytes}"
                            )
                        if budget.max_breaking_changes is not None:
                            breaking = 1 if patchset.semantic_change else 0
                            if breaking > budget.max_breaking_changes:
                                raise RuntimeError(
                                    f"Surface change budget exceeded for {surface_id}: "
                                    f"breaking_changes {breaking} > {budget.max_breaking_changes}"
                                )
                for surface_id in surface_ids:
                    lease_id = f"lease_patch_apply_{patchset.patchset_id}_{surface_id}"
                    event, preempted = lease_manager.request_lease(
                        lease_id=lease_id,
                        surface=SurfaceRef(type="surface", id=surface_id),
                        holder=holder,
                        ttl_seconds=3600,
                        priority=0,
                    )
                    _append_lease(event)
                    if preempted:
                        _append_lease(preempted)
                    if event.status != "granted":
                        raise RuntimeError("PatchApply lease denied")
                    held.append((surface_id, lease_id, holder))
                    lease_requirements_extra.append((surface_id, holder.task_id or ""))
            self._apply_patchsets_to_checkout(
                Path(repo_context.repo_root),
                patchsets,
                repo_context,
                patchset_limits,
                run_root,
                run_id,
                stage="patch_apply",
                record_patchsets=True,
                guardrails=guardrails,
                plan_hash=plan_content_hash,
                milestone_id=vnext_milestone_id,
            )
            for surface_id, lease_id, holder in held:
                event = lease_manager.release_lease(surface_id, lease_id, holder)
                if event:
                    _append_lease(event)

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "patch_apply",
                "validate",
                objectives=["Run WorkGraph tasks"],
                completed_refs=[p.patchset_id for p in patchsets] if patchsets else [],
                pending_blockers=[],
                next_actions=["Run scheduler"],
                evidence=(
                    [
                        EvidencePointer(artifact_ref=ref, json_pointer="/")
                        for ref in [p.patchset_id for p in patchsets]
                    ]
                    if patchsets
                    else [
                        EvidencePointer(
                            artifact_ref=artifact_refs["work_graph"],
                            json_pointer="/",
                        )
                    ]
                ),
            )
        )

        # Validate stage
        self._event(run_root, run_id, stage="validate", event_type="stage_transition", payload={})

        job_events: list[dict[str, Any]] = []
        lease_events: list[dict[str, Any]] = []
        work_events: list[dict[str, Any]] = []

        def _append_log(path: Path, payload: dict[str, Any]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")

        job_events_path = workspace.canonical_root / "job_events.jsonl"
        work_events_path = workspace.canonical_root / "work_graph_events.jsonl"
        leases_path = workspace.canonical_root / "surface_leases.jsonl"
        for path, item_type in (
            (job_events_path, "job_events_log"),
            (work_events_path, "work_graph_events_log"),
            (leases_path, "surface_leases_log"),
        ):
            if not path.exists():
                path.write_text("", encoding="utf-8")
            workspace.index_artifact(path, item_type, self._schema_version, {"stage": "validate", "role": "engine"})

        def job_logger(payload: dict[str, Any]) -> None:
            job_events.append(payload)
            _append_log(job_events_path, payload)
            job_id = str(payload.get("job_id") or "")
            if job_id:
                self._append_job_run_record(run_root, job_id, payload)
                event = payload.get("event")
                if event == "attempt_started":
                    append_vnext_event(
                        run_root,
                        "implementation",
                        "JOB_STARTED",
                        run_id=str(run_id),
                        plan_hash=plan_content_hash,
                        milestone_id=vnext_milestone_id,
                        job_id=job_id,
                    )
                if event == "completed":
                    append_vnext_event(
                        run_root,
                        "implementation",
                        "JOB_FINISHED",
                        run_id=str(run_id),
                        plan_hash=plan_content_hash,
                        milestone_id=vnext_milestone_id,
                        job_id=job_id,
                        payload={"status": payload.get("status")},
                    )

        def work_logger(payload: dict[str, Any]) -> None:
            work_events.append(payload)
            _append_log(work_events_path, payload)

        def lease_logger(payload: dict[str, Any]) -> None:
            lease_events.append(payload)
            _append_log(leases_path, payload)

        change_intents: dict[str, ChangeIntentPayload] = {}
        change_intent_payload = v2_inputs.get("change_intents")
        if isinstance(change_intent_payload, dict):
            items = change_intent_payload.get("change_intents")
            if items is None and "_list" in change_intent_payload:
                items = change_intent_payload.get("_list", [])
            for item in items or []:
                intent = ChangeIntentPayload.model_validate(item)
                change_intents[intent.intent_id] = intent
                self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "change_intent",
                    f"change_intent_{intent.intent_id}",
                    intent.model_dump(),
                    parents=[artifact_refs["plan_package_final"]],
                    stage="intake",
                )

        cache = IdempotencyCache()
        retry_policy = config.get("retry_policy", {}) if isinstance(config.get("retry_policy", {}), dict) else {}
        max_retries = int(retry_policy.get("max_retries", 0))
        job_runner = JobRunner(
            cache=cache,
            write_artifact=lambda at, aid, payload, parents, stage, role: self._write_artifact(
                store, workspace, run_id, at, aid, payload, parents, stage, role
            ),
            write_attestation=lambda **kwargs: self._write_attestation(
                store,
                workspace,
                run_id,
                repo_context=repo_context,
                profile_catalog=execution_profile_catalog,
                **kwargs,
            ),
            repo_root=repo_context.repo_root,
            change_intents=change_intents,
            job_event_logger=job_logger,
            expectation_registry=expectation_registry,
            artifact_reader=lambda ref: store.read_artifact(ref).payload,
            run_id=str(run_id),
            schema_version=self._schema_version,
            max_retries=max_retries,
        )

        lease_manager = LeaseManager()

        def update_evidence(entries: list[EvidenceIndexEntry]) -> None:
            nonlocal evidence_index_ref
            evidence_index_ref = self._update_evidence_index(
                store,
                workspace,
                run_id,
                evidence_index_ref,
                entries,
                stage="validate",
            )
            artifact_refs["evidence_index"] = evidence_index_ref

        scheduler = WorkScheduler(
            job_runner=job_runner,
            lease_manager=lease_manager,
            work_event_logger=work_logger,
            lease_event_logger=lease_logger,
            update_evidence_index=update_evidence,
            write_artifact=lambda at, aid, payload, parents, stage, role: self._write_artifact(
                store, workspace, run_id, at, aid, payload, parents, stage, role
            ),
        )
        exp_ids = {exp.expectation_id for exp in expectation_registry.expectations}
        missing_expectations = sorted(
            {
                exp_id
                for spec in job_spec_map.values()
                for exp_id in spec.expected_expectation_ids
                if exp_id not in exp_ids
            }
        )
        if missing_expectations:
            raise RuntimeError(f"Job specs reference missing expectations: {missing_expectations}")
        scheduler_result = scheduler.run(
            work_graph=work_graph,
            job_specs=job_spec_map,
            stage="validate",
            evidence_parents=[artifact_refs["work_graph"], evidence_index_ref],
        )

        job_events_payload = JobEventsPayload(
            schema_version=self._schema_version, events=[JobEvent.model_validate(e) for e in job_events]
        )
        artifact_refs["job_events"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "job_events",
            "job_events_v21",
            job_events_payload.model_dump(),
            parents=[artifact_refs["work_graph"]],
            stage="validate",
        )
        combined_lease_events = lease_events_extra + lease_events
        leases_payload = SurfaceLeasesPayload(
            schema_version=self._schema_version,
            events=[SurfaceLeaseEvent.model_validate(e) for e in combined_lease_events],
        )
        artifact_refs["surface_leases"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "surface_leases",
            "surface_leases_v21",
            leases_payload.model_dump(),
            parents=[artifact_refs["work_graph"]],
            stage="validate",
        )

        vnext_job_results = self._write_vnext_job_results(
            run_root,
            [JobResultPayload.model_validate(env.payload) for env in store.list_artifacts("job_result")],
            job_spec_map,
        )
        vnext_evidence_index, vnext_expectation_status = self._build_vnext_evidence_index(
            vnext_expectation_registry,
            job_results=vnext_job_results,
            job_spec_map=job_spec_map,
            milestone_id=vnext_milestone_id,
            plan_hash=plan_content_hash,
        )
        self._write_impl_json(run_root, "evidence_index.json", vnext_evidence_index)
        vnext_decision_record, must_failures = self._build_vnext_decision_record(
            vnext_expectation_registry,
            vnext_expectation_status,
            evidence_index=vnext_evidence_index,
            milestone_id=vnext_milestone_id,
            plan_hash=plan_content_hash,
        )
        self._write_impl_json(run_root, "decision_record.json", vnext_decision_record)
        baseline_report = self._build_baseline_report(
            expectation_registry=vnext_expectation_registry,
            evidence_index=vnext_evidence_index,
            expectation_status=vnext_expectation_status,
            plan_hash=plan_content_hash,
            milestone_id=vnext_milestone_id,
            run_root=run_root,
        )
        if baseline_report is not None:
            self._write_impl_json(run_root, "baseline_report.json", baseline_report)
        if vnext_decision_record.get("decision") == "PASS":
            append_vnext_event(
                run_root,
                "implementation",
                "MILESTONE_VERIFIED",
                run_id=str(run_id),
                plan_hash=plan_content_hash,
                milestone_id=vnext_milestone_id,
            )
        if vnext_enforce_gates and vnext_decision_record.get("decision") != "PASS":
            blocked_check_ids = must_failures
            blocked_job_ids = [
                job_id
                for job_id, spec in job_spec_map.items()
                if any(exp in blocked_check_ids for exp in spec.expected_expectation_ids)
            ]
            blocked_task_ids = [
                task.task_id
                for task in work_graph.tasks
                if any(job_id in task.job_specs for job_id in blocked_job_ids)
            ]
            self._emit_vnext_change_request(
                run_root,
                plan_hash=plan_content_hash,
                reason_code="INFEASIBLE",
                summary="Required expectations failed verification.",
                blocked_task_ids=blocked_task_ids,
                blocked_job_ids=blocked_job_ids,
                blocked_check_ids=blocked_check_ids,
                milestones=[vnext_milestone_id],
            )
            raise RuntimeError("Milestone verification failed; change request emitted.")

        if scheduler_result.diagnosis_report_id:
            raise RuntimeError("Scheduler stalled; diagnosis report emitted")

        # Update tool registry with probe evidence if available
        if tool_registry.tools:
            probe_artifacts = store.list_artifacts("tool_probe_results")
            probe_lookup: dict[str, tuple[str, int]] = {}
            for artifact in probe_artifacts:
                payload = artifact.payload
                probes = payload.get("probes", []) if isinstance(payload, dict) else []
                for idx, probe in enumerate(probes):
                    tool_id = probe.get("tool_id") if isinstance(probe, dict) else None
                    if tool_id and tool_id not in probe_lookup:
                        probe_lookup[tool_id] = (artifact.artifact_id, idx)
            updated = False
            for tool in tool_registry.tools:
                if tool.last_probe is None and tool.tool_id in probe_lookup:
                    artifact_id, idx = probe_lookup[tool.tool_id]
                    tool.last_probe = EvidencePointer(
                        artifact_ref=artifact_id, json_pointer=f"/probes/{idx}"
                    )
                    tool.probe_status = "probed"
                    updated = True
            if updated:
                new_artifact_id = f"{artifact_refs['tool_registry']}_{uuid4().hex[:8]}"
                path = store.patch_artifact(
                    artifact_id=artifact_refs["tool_registry"],
                    new_artifact_id=new_artifact_id,
                    patch_ops=[
                        {
                            "op": "replace",
                            "path": "/tools",
                            "value": [t.model_dump() for t in tool_registry.tools],
                        }
                    ],
                    run_id=run_id,
                )
                tool_registry = ToolRegistryPayload.model_validate(store.read_artifact(new_artifact_id).payload)
                artifact_refs["tool_registry"] = new_artifact_id
                workspace.index_artifact(
                    path,
                    "tool_registry",
                    self._schema_version,
                    {"stage": "validate", "role": "engine"},
                )

        # Validators
        artifacts = {artifact.artifact_id: artifact.payload for artifact in store.list_artifacts()}
        validate_profile_compliance_v21(
            Path(repo_context.repo_root),
            execution_profile_catalog,
            repo_context,
            list(job_spec_map.values()),
        )
        validate_tool_registry_v21(
            tool_registry,
            list(job_spec_map.values()),
            artifacts,
            repo_context=repo_context,
            profile_catalog=execution_profile_catalog,
            run_id=str(run_id),
            allow_unprobed=bool(config.get("tool_registry_policy", {}).get("allow_unprobed", False)),
        )
        validate_expectations_v21(expectation_registry, artifacts)
        evidence_payload = EvidenceIndexPayload.model_validate(store.read_artifact(evidence_index_ref).payload)
        validate_verification_plan_coverage_v21(program_graph, verification_plan, evidence_payload)
        validate_assumption_gate(assumption_registry)
        apply_records = [
            ApplyRecordPayload.model_validate(env.payload) for env in store.list_artifacts("apply_record")
        ]
        change_intent_ids = {
            ChangeIntentPayload.model_validate(env.payload).intent_id
            for env in store.list_artifacts("change_intent")
        }
        validate_side_effects_v21(list(job_spec_map.values()), change_intent_ids, apply_records)
        validate_world_state_v21(apply_records, artifacts)
        lease_requirements = [
            (surface_id, task.task_id) for task in work_graph.tasks for surface_id in task.surfaces
        ]
        lease_requirements.extend(lease_requirements_extra)
        lease_requirements = sorted(set(lease_requirements))
        validate_lease_compliance_v21(
            lease_requirements,
            [SurfaceLeaseEvent.model_validate(e) for e in combined_lease_events],
        )

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "validate",
                "integrate_validate",
                objectives=["Integrate and validate outputs"],
                completed_refs=[artifact_refs["work_graph"], evidence_index_ref],
                pending_blockers=[],
                next_actions=["Integration validation"],
                evidence=[EvidencePointer(artifact_ref=evidence_index_ref, json_pointer="/")],
            )
        )

        # Integrate+Validate stage (no-op for v2.1 minimal)
        self._event(run_root, run_id, stage="integrate_validate", event_type="stage_transition", payload={})
        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "integrate_validate",
                "review",
                objectives=["Review outputs"],
                completed_refs=[artifact_refs["work_graph"], evidence_index_ref],
                pending_blockers=[],
                next_actions=["Review findings"],
                evidence=[EvidencePointer(artifact_ref=evidence_index_ref, json_pointer="/")],
            )
        )

        # Review stage
        self._event(run_root, run_id, stage="review", event_type="stage_transition", payload={})
        review_findings = ReviewFindingsPayload(findings=[])
        artifact_refs["review_findings"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "review_findings",
            "review_findings_v21",
            review_findings.model_dump(),
            parents=[evidence_index_ref],
            stage="review",
            role=reviewer_role,
        )

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "review",
                "judge",
                objectives=["Judge readiness"],
                completed_refs=[artifact_refs["review_findings"]],
                pending_blockers=[],
                next_actions=["Decision record"],
                evidence=[EvidencePointer(artifact_ref=artifact_refs["review_findings"], json_pointer="/")],
            )
        )

        # Judge stage
        self._event(run_root, run_id, stage="judge", event_type="stage_transition", payload={})
        trace_report = TraceReportPayload(
            trace_complete=True,
            missing_links=[],
            evidence=[EvidencePointer(artifact_ref=evidence_index_ref, json_pointer="/")],
            recommendation="freeze",
        )
        artifact_refs["trace_report"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "trace_report",
            "trace_report_v21",
            trace_report.model_dump(),
            parents=[artifact_refs["review_findings"]],
            stage="judge",
            role=judge_role,
        )
        decision_record = DecisionRecordPayload(
            decision_id="decision_v21",
            summary="Freeze approved",
            reasons=["Verification plan satisfied"],
            what_would_change=["Failed evidence coverage"],
            evidence=[EvidencePointer(artifact_ref=artifact_refs["trace_report"], json_pointer="/")],
        )
        artifact_refs["decision_record"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "decision_record",
            "decision_record_v21",
            decision_record.model_dump(),
            parents=[artifact_refs["trace_report"]],
            stage="judge",
            role=judge_role,
        )

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                "judge",
                "freeze",
                objectives=["Freeze release"],
                completed_refs=[artifact_refs["decision_record"]],
                pending_blockers=[],
                next_actions=["Finalize release bundle"],
                evidence=[EvidencePointer(artifact_ref=artifact_refs["decision_record"], json_pointer="/")],
            )
        )

        # Freeze stage
        self._event(run_root, run_id, stage="freeze", event_type="stage_transition", payload={})
        release_bundle = ReleaseBundlePayload(
            version="v2_1",
            artifacts=[],
            notes=["v2.1 release bundle"],
            provenance={"run_id": str(run_id)},
        )
        artifact_refs["release_bundle"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "release_bundle",
            "release_bundle_v21",
            release_bundle.model_dump(),
            parents=[artifact_refs["decision_record"]],
            stage="freeze",
        )
        append_vnext_event(
            run_root,
            "implementation",
            "RELEASE_FROZEN",
            run_id=str(run_id),
            plan_hash=plan_content_hash,
            milestone_id=vnext_milestone_id,
        )

        self._validate_role_outputs(workspace, role_sets)
        workspace.assert_index_complete()
        store.freeze()
        workspace.freeze()

        return ImplementationRunResult(run_id=run_id, run_root=run_root)

    def run(
        self,
        plan_path: Path,
        repo_context_path: Path,
        workspace_context_path: Path,
        config_path: Path,
        handoff_bundle_path: Path,
        *,
        from_handoff: Path | None = None,
        allow_repo_override: bool = False,
    ) -> ImplementationRunResult:
        config_raw = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_raw)
        if not isinstance(config, dict):
            raise ValueError("config must be a mapping")
        roles = load_role_configs(config.get("roles", {}))
        self._enforce_role_separation(roles)
        role_sets = self._role_sets(roles)
        role_assignments = self._role_assignments(roles)
        writer_role = role_assignments["writer"]
        reviewer_role = role_assignments["reviewer"]
        judge_role = role_assignments["judge"]
        schema_version = self._schema_version_from_config(config)
        features = self._feature_flags(config, schema_version)
        self._schema_version = schema_version
        self._features = features
        self._load_tool_permissions(config)

        run_id = uuid4()
        run_root = self.storage_root / str(run_id)
        run_root.mkdir(parents=True, exist_ok=True)

        bundle_path = from_handoff or handoff_bundle_path
        is_manifest = False
        if bundle_path.name == "handoff_manifest.json":
            is_manifest = True
        else:
            try:
                payload = json.loads(bundle_path.read_text(encoding="utf-8"))
                handoff_schema = str(payload.get("schema_version", ""))
                if handoff_schema.startswith("handoff_manifest"):
                    is_manifest = True
            except Exception:
                is_manifest = False

        if is_manifest:
            accepted = accept_handoff_manifest(
                manifest_path=bundle_path,
                run_root=run_root,
                implementation_run_id=str(run_id),
                implementation_engine_version=str(config.get("version", schema_version)),
                allow_repo_override=allow_repo_override,
            )
        else:
            accepted = accept_handoff(
                bundle_path=bundle_path,
                run_root=run_root,
                implementation_run_id=str(run_id),
                implementation_engine_version=str(config.get("version", schema_version)),
                allow_repo_override=allow_repo_override,
            )
        plan_path = accepted.plan_path
        repo_context_path = accepted.repo_context_path
        workspace_context_path = accepted.workspace_context_path
        handoff_bundle_path = accepted.bundle_path

        if features.get("v2_1"):
            return self._run_v21(
                plan_path,
                repo_context_path,
                workspace_context_path,
                config_path,
                handoff_bundle_path,
                accepted.config_snapshot_path,
                run_id=run_id,
                run_root=run_root,
                config=config,
                config_raw=config_raw,
                roles=roles,
                role_sets=role_sets,
                role_assignments=role_assignments,
                plan_content_hash=accepted.plan_content_hash,
            )

        v2_inputs = {
            "execution_profile_catalog": self._load_config_payload(config, config_path, "execution_profile_catalog"),
            "tool_registry": self._load_config_payload(config, config_path, "tool_registry"),
            "tool_probe_results": self._load_config_payload(config, config_path, "tool_probe_results"),
            "expectation_registry": self._load_config_payload(config, config_path, "expectation_registry"),
            "remote_ops_manifest": self._load_config_payload(config, config_path, "remote_ops_manifest"),
            "research_contracts": self._load_config_payload(config, config_path, "research_contracts"),
            "experiment_manifest": self._load_config_payload(config, config_path, "experiment_manifest"),
            "experiment_results": self._load_config_payload(config, config_path, "experiment_results"),
            "replication_report": self._load_config_payload(config, config_path, "replication_report"),
            "portfolio_decisions": self._load_config_payload(config, config_path, "portfolio_decisions"),
        }
        compat_mode = False
        capability_level = "v1"
        if schema_version.startswith("2"):
            missing_inputs = [
                key
                for key in ("execution_profile_catalog", "tool_registry", "expectation_registry")
                if v2_inputs.get(key) is None
            ]
            compat_mode = bool(missing_inputs)
            capability_level = "v1_compat" if compat_mode else "v2"

        plan = self._load_plan_package(plan_path)
        handoff_bundle = PlanningHandoffBundlePayload.model_validate_json(
            handoff_bundle_path.read_text(encoding="utf-8")
        )
        if schema_version.startswith("2"):
            repo_context = RepoContextPayloadV2.model_validate_json(
                repo_context_path.read_text(encoding="utf-8")
            )
        else:
            repo_context = RepoContextPayload.model_validate_json(repo_context_path.read_text(encoding="utf-8"))
        workspace_context = WorkspaceContextPayload.model_validate_json(
            workspace_context_path.read_text(encoding="utf-8")
        )

        workspace = WorkspaceManager(
            run_root,
            workspace_context,
            schema_version=self._schema_version,
            run_id=str(run_id),
        )
        store = ImplementationArtifactStore(self.storage_root, run_id)

        policy_knobs = {
            "patchset_limits": config.get("patchset_limits", {}),
            "retry_policy": config.get("retry_policy", {}),
            "tool_policy": config.get("tool_policy", {}),
        }
        hitl_cfg = config.get("hitl_approvals", {})
        approved_patchsets = set()
        if isinstance(hitl_cfg, dict):
            approved_patchsets = {str(item) for item in hitl_cfg.get("patchsets", []) if isinstance(item, str)}

        create_manifest(
            run_root,
            ManifestInput(
                run_id=run_id,
                code_version=self._git_code_version(),
                schema_versions={"implementation": schema_version},
                run_config_version=str(config.get("version", "")),
                stage_machine_version=str(config.get("stage_machine_version", "1.0.0")),
                validator_suite_version=str(config.get("validator_suite_version", "1.0.0")),
                model_portfolio=self._model_portfolio(roles),
                toolchain_versions={"python": "3.12"},
                policy_knobs=policy_knobs,
                base_commit=repo_context.base_commit,
                head_commit=repo_context.head_commit,
                prompt_pack_hash=str(config.get("prompt_pack_hash", "")),
                config_raw=config_raw,
                capability_level=capability_level,
            ),
        )

        artifact_refs: dict[str, str] = {}
        stage_handoffs: list[str] = []

        # Intake stage
        event_cursor = self._event(run_root, run_id, stage="intake", event_type="stage_transition", payload={})
        routing_payload = {
            key: {
                "role": role_name,
                "provider": roles[role_name].model_provider,
                "model": roles[role_name].model_name,
            }
            for key, role_name in role_assignments.items()
        }
        self._event(
            run_root,
            run_id,
            stage="intake",
            event_type="decision",
            payload={"role_routing": routing_payload},
        )
        self._execute_tool_calls(run_root, run_id, "intake", repo_context=repo_context)
        artifact_refs["plan_package_final"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "plan_package_final",
            "plan_package_final_v1",
            plan.model_dump(by_alias=True),
            parents=[],
            stage="intake",
        )
        artifact_refs["planning_handoff_bundle"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "planning_handoff_bundle",
            "planning_handoff_bundle_v1",
            handoff_bundle.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )
        artifact_refs["repo_context"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "repo_context",
            "repo_context_v1",
            repo_context.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )
        artifact_refs["workspace_context"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "workspace_context",
            "workspace_context_v1",
            workspace_context.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="intake",
        )
        change_request_refs, change_request_artifacts, change_request_decisions = self._load_change_requests(
            config, store, workspace, run_id, stage="intake", decision_role=judge_role
        )
        for artifact_id in change_request_artifacts:
            artifact_refs[artifact_id] = artifact_id

        execution_profile_catalog: ExecutionProfileCatalogPayload | None = None
        tool_registry: ToolRegistryPayload | None = None
        tool_probe_results: ToolProbeResultsPayload | None = None
        expectation_registry: ExpectationRegistryPayload | None = None
        evidence_index_ref: str | None = None
        remote_ops_manifest: RemoteOpsManifestPayload | None = None
        remote_run_cache: RemoteRunCacheIndexPayload | None = None
        research_contracts: ResearchContractsPayload | None = None
        experiment_manifest: ExperimentManifestPayload | None = None
        experiment_results: ExperimentResultsPayload | None = None
        replication_report: ReplicationReportPayload | None = None
        portfolio_decisions: PortfolioDecisionsPayload | None = None

        if features.get("v2_profiles"):
            profile_payload = v2_inputs.get("execution_profile_catalog")
            execution_profile_catalog = (
                ExecutionProfileCatalogPayload.model_validate(profile_payload)
                if profile_payload
                else self._default_profile_catalog()
            )
            artifact_refs["execution_profile_catalog"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "execution_profile_catalog",
                "execution_profile_catalog_v2",
                execution_profile_catalog.model_dump(),
                parents=[artifact_refs["plan_package_final"]],
                stage="intake",
            )

        if features.get("v2_tool_registry"):
            allow_unprobed = False
            policy = config.get("tool_registry_policy", {})
            if isinstance(policy, dict):
                allow_unprobed = bool(policy.get("allow_unprobed", False))
            tool_registry, tool_probe_results, _attestations = self._prepare_tool_registry(
                v2_inputs.get("tool_registry"),
                v2_inputs.get("tool_probe_results"),
                store,
                workspace,
                run_id,
                stage="intake",
                repo_context=repo_context,
                profile_catalog=execution_profile_catalog,
                allow_unprobed=allow_unprobed,
            )
            if tool_probe_results:
                artifact_refs["tool_probe_results"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "tool_probe_results",
                    "tool_probe_results_v2",
                    tool_probe_results.model_dump(),
                    parents=[artifact_refs["plan_package_final"]],
                    stage="intake",
                )
                workspace.write_canonical_log(
                    "tool_probe_results.jsonl",
                    [probe.model_dump() for probe in tool_probe_results.probes],
                    {"stage": "intake", "role": "engine"},
                )
            artifact_refs["tool_registry"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "tool_registry",
                "tool_registry_v2",
                tool_registry.model_dump(),
                parents=[artifact_refs["plan_package_final"]],
                stage="intake",
            )

        if features.get("v2_expectations"):
            registry_payload = v2_inputs.get("expectation_registry")
            expectation_registry = (
                ExpectationRegistryPayload.model_validate(registry_payload)
                if registry_payload
                else self._compile_expectation_registry_from_plan(plan)
            )
            artifact_refs["expectation_registry"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "expectation_registry",
                "expectation_registry_v2",
                expectation_registry.model_dump(),
                parents=[artifact_refs["plan_package_final"]],
                stage="intake",
            )
            evidence_index = EvidenceIndexPayload(schema_version=IMPL_SCHEMA_VERSION_V2, evidence=[])
            evidence_index_ref = self._write_artifact(
                store,
                workspace,
                run_id,
                "evidence_index",
                "evidence_index_v2",
                evidence_index.model_dump(),
                parents=[artifact_refs["expectation_registry"]],
                stage="intake",
            )
            artifact_refs["evidence_index"] = evidence_index_ref

        if features.get("v2_remote_ops"):
            if compat_mode and not bool(config.get("allow_remote_ops_in_v1_compat", False)):
                features["v2_remote_ops"] = False
            else:
                manifest_payload = v2_inputs.get("remote_ops_manifest") or {
                    "schema_version": IMPL_SCHEMA_VERSION_V2,
                    "ops": [],
                }
                remote_ops_manifest = RemoteOpsManifestPayload.model_validate(manifest_payload)
                artifact_refs["remote_ops_manifest"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "remote_ops_manifest",
                    "remote_ops_manifest_v2",
                    remote_ops_manifest.model_dump(),
                    parents=[artifact_refs["plan_package_final"]],
                    stage="intake",
                )
                remote_run_cache = RemoteRunCacheIndexPayload(schema_version=IMPL_SCHEMA_VERSION_V2, entries=[])
                artifact_refs["remote_run_cache_index"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "remote_run_cache_index",
                    "remote_run_cache_index_v2",
                    remote_run_cache.model_dump(),
                    parents=[artifact_refs["plan_package_final"]],
                    stage="intake",
                )

        if features.get("v2_research"):
            contracts_payload = v2_inputs.get("research_contracts") or {
                "schema_version": IMPL_SCHEMA_VERSION_V2,
                "contracts": [],
            }
            research_contracts = ResearchContractsPayload.model_validate(contracts_payload)
            artifact_refs["research_contracts"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "research_contracts",
                "research_contracts_v2",
                research_contracts.model_dump(),
                parents=[artifact_refs["plan_package_final"]],
                stage="intake",
            )
            if v2_inputs.get("experiment_manifest"):
                experiment_manifest = ExperimentManifestPayload.model_validate(v2_inputs["experiment_manifest"])
                artifact_refs["experiment_manifest"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "experiment_manifest",
                    "experiment_manifest_v2",
                    experiment_manifest.model_dump(),
                    parents=[artifact_refs["research_contracts"]],
                    stage="intake",
                )
            if v2_inputs.get("experiment_results"):
                experiment_results = ExperimentResultsPayload.model_validate(v2_inputs["experiment_results"])
                artifact_refs["experiment_results"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "experiment_results",
                    "experiment_results_v2",
                    experiment_results.model_dump(),
                    parents=[artifact_refs.get("experiment_manifest", artifact_refs["research_contracts"])],
                    stage="intake",
                )
            elif experiment_manifest:
                experiment_results = self._run_experiments(
                    experiment_manifest,
                    store,
                    workspace,
                    run_id,
                    stage="intake",
                    repo_context=repo_context,
                    profile_catalog=execution_profile_catalog,
                )
                artifact_refs["experiment_results"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "experiment_results",
                    "experiment_results_v2",
                    experiment_results.model_dump(),
                    parents=[artifact_refs.get("experiment_manifest", artifact_refs["research_contracts"])],
                    stage="intake",
                )
            if experiment_results:
                att_ref = experiment_results.attestation_ref
                attestation_missing = False
                try:
                    store.read_artifact(att_ref)
                except Exception:
                    attestation_missing = True
                if attestation_missing:
                    profile_id = experiment_manifest.profile_id if experiment_manifest else "legacy_v1"
                    new_attestation = self._write_attestation(
                        store,
                        workspace,
                        run_id,
                        stage="intake",
                        profile_id=profile_id,
                        repo_context=repo_context,
                        profile_catalog=execution_profile_catalog,
                        tool_id=None,
                        inputs=experiment_manifest.dataset_snapshot_ids if experiment_manifest else [],
                        spec_versions={"experiment_manifest": experiment_manifest.schema_version}
                        if experiment_manifest
                        else None,
                    )
                    current_id = artifact_refs.get("experiment_results")
                    if current_id:
                        patched_id = f"{current_id}_attested"
                        patched_path = store.patch_artifact(
                            artifact_id=current_id,
                            new_artifact_id=patched_id,
                            patch_ops=[{"op": "replace", "path": "/attestation_ref", "value": new_attestation}],
                            run_id=run_id,
                        )
                        workspace.index_artifact(
                            patched_path,
                            "experiment_results",
                            self._schema_version,
                            {"stage": "intake", "role": "engine"},
                        )
                        artifact_refs["experiment_results"] = patched_id
                        experiment_results = ExperimentResultsPayload.model_validate(
                            store.read_artifact(patched_id).payload
                        )
            if v2_inputs.get("replication_report"):
                replication_report = ReplicationReportPayload.model_validate(v2_inputs["replication_report"])
                artifact_refs["replication_report"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "replication_report",
                    "replication_report_v2",
                    replication_report.model_dump(),
                    parents=[artifact_refs.get("experiment_results", artifact_refs["research_contracts"])],
                    stage="intake",
                )
            elif experiment_manifest and experiment_results:
                replication_report = self._run_replication(experiment_manifest, experiment_results)
                artifact_refs["replication_report"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "replication_report",
                    "replication_report_v2",
                    replication_report.model_dump(),
                    parents=[artifact_refs.get("experiment_results", artifact_refs["research_contracts"])],
                    stage="intake",
                )
            if v2_inputs.get("portfolio_decisions"):
                portfolio_decisions = PortfolioDecisionsPayload.model_validate(v2_inputs["portfolio_decisions"])
                artifact_refs["portfolio_decisions"] = self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "portfolio_decisions",
                    "portfolio_decisions_v2",
                    portfolio_decisions.model_dump(),
                    parents=[artifact_refs["research_contracts"]],
                    stage="intake",
                )

        if features.get("v2_tool_registry") and tool_registry:
            artifacts_for_validation = {artifact.artifact_id: artifact.payload for artifact in store.list_artifacts()}
            validate_tool_registry(
                tool_registry,
                artifacts_for_validation,
                repo_context=repo_context,
                profile_catalog=execution_profile_catalog,
                run_id=str(run_id),
            )

        if features.get("v2_expectations") and expectation_registry:
            artifacts_for_validation = {artifact.artifact_id: artifact.payload for artifact in store.list_artifacts()}
            validate_expectation_registry(expectation_registry, artifacts_for_validation, remote_ops_manifest)

        if features.get("v2_remote_ops") and remote_ops_manifest and tool_registry:
            tool_ids = {tool.tool_id for tool in tool_registry.tools}
            missing_tools = {op.tool_id for op in remote_ops_manifest.ops if op.tool_id not in tool_ids}
            if missing_tools:
                raise RuntimeError(f"Remote ops manifest references missing tool_ids: {sorted(missing_tools)}")
        if features.get("v2_remote_ops") and not expectation_registry:
            raise RuntimeError("v2_remote_ops requires expectation_registry")

        remote_validate_enabled = bool(features.get("v2_remote_ops") and remote_ops_manifest and expectation_registry)
        stage_offset = 1 if remote_validate_enabled else 0

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="intake",
                to_stage="generate",
                objectives=["Validate inputs", "Initialize audit spine"],
                completed_refs=list(artifact_refs.values()),
                pending_blockers=[],
                next_actions=["Generate work plan", "Draft patchsets"],
                evidence=[
                    EvidencePointer(artifact_ref=artifact_refs["plan_package_final"], json_pointer="/"),
                    EvidencePointer(artifact_ref=artifact_refs["planning_handoff_bundle"], json_pointer="/"),
                    EvidencePointer(artifact_ref=artifact_refs["repo_context"], json_pointer="/"),
                    EvidencePointer(artifact_ref=artifact_refs["workspace_context"], json_pointer="/"),
                    *(
                        [EvidencePointer(artifact_ref=artifact_refs["execution_profile_catalog"], json_pointer="/")]
                        if "execution_profile_catalog" in artifact_refs
                        else []
                    ),
                    *(
                        [EvidencePointer(artifact_ref=artifact_refs["tool_registry"], json_pointer="/")]
                        if "tool_registry" in artifact_refs
                        else []
                    ),
                    *(
                        [EvidencePointer(artifact_ref=artifact_refs["expectation_registry"], json_pointer="/")]
                        if "expectation_registry" in artifact_refs
                        else []
                    ),
                ],
            )
        )
        write_checkpoint(
            run_root,
            run_id,
            CheckpointState(
                stage_name="intake",
                stage_index=0,
                event_cursor=event_cursor,
                artifact_refs=dict(artifact_refs),
                routing_state={"stage": "intake"},
                tool_side_effect_ledger_ref=None,
            ),
        )
        # Generate stage
        event_cursor = self._event(run_root, run_id, stage="generate", event_type="stage_transition", payload={})
        self._execute_tool_calls(run_root, run_id, "generate", repo_context=repo_context)
        patchset_limits = PatchsetLimits(
            max_operations=int(config.get("patchset_limits", {}).get("max_operations", 25)),
            max_bytes=int(config.get("patchset_limits", {}).get("max_bytes", 2000)),
            max_operations_total=(
                int(config.get("patchset_limits", {}).get("max_operations_total"))
                if config.get("patchset_limits", {}).get("max_operations_total") is not None
                else None
            ),
        )
        if features.get("v2_profiles") or features.get("v2_expectations"):
            if not isinstance(repo_context, RepoContextPayloadV2):
                raise RuntimeError("v2 work_plan requires v2 repo_context")
            work_plan = self._build_work_plan_v2(
                plan,
                patchset_limits,
                repo_context,
                expectation_registry or self._compile_expectation_registry_from_plan(plan),
                execution_profile_catalog or self._default_profile_catalog(),
                remote_ops_manifest,
            )
            work_plan_id = "work_plan_v2"
        else:
            work_plan = self._build_work_plan(plan, patchset_limits)
            work_plan_id = "work_plan_v1"
        artifact_refs["work_plan"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "work_plan",
            work_plan_id,
            work_plan.model_dump(),
            parents=[artifact_refs["plan_package_final"]],
            stage="generate",
        )
        context_required = [artifact_refs["work_plan"]]
        context_refs = [artifact_refs["plan_package_final"], artifact_refs["work_plan"]]
        if features.get("v2_profiles") and "execution_profile_catalog" in artifact_refs:
            context_refs.append(artifact_refs["execution_profile_catalog"])
            context_required.append(artifact_refs["execution_profile_catalog"])
        if features.get("v2_tool_registry") and "tool_registry" in artifact_refs:
            context_refs.append(artifact_refs["tool_registry"])
            context_required.append(artifact_refs["tool_registry"])
        if features.get("v2_expectations") and "expectation_registry" in artifact_refs:
            context_refs.append(artifact_refs["expectation_registry"])
            context_required.append(artifact_refs["expectation_registry"])
        if features.get("v2_expectations") and "evidence_index" in artifact_refs:
            context_refs.append(artifact_refs["evidence_index"])
        self._write_context_pack(
            workspace,
            run_id,
            stage="generate",
            role=writer_role,
            artifact_refs=context_refs,
            evidence=[
                EvidencePointer(artifact_ref=artifact_refs["plan_package_final"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
            ],
            required_refs=context_required if context_required else None,
        )

        retry_cap = int(config.get("retry_policy", {}).get("max_retries", 1))
        lane_results: list[LaneResult] = []
        all_patchsets: list[PatchsetPayload] = []
        for lane in work_plan.lanes:
            tasks = self._lane_tasks(work_plan, lane.lane_id)
            if not tasks:
                continue
            lane_result = self._run_lane_pipeline(
                lane,
                tasks,
                plan,
                repo_context,
                artifact_refs["work_plan"],
                patchset_limits,
                run_root,
                run_id,
                store,
                workspace,
                change_request_refs,
                set(change_request_decisions.keys()),
                writer_role,
                reviewer_role,
                approved_patchsets,
                config,
                retry_cap,
            )
            lane_results.append(lane_result)
            all_patchsets.extend(lane_result.patchsets)
            stage_handoffs.extend(lane_result.stage_handoffs)

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="generate",
                to_stage="validate",
                objectives=["Assemble lane outputs", "Prepare integration validation"],
                completed_refs=[artifact_refs["work_plan"]] + [p.patchset_id for p in all_patchsets],
                pending_blockers=[],
                next_actions=["Integrate lanes", "Run integration validators"],
                evidence=[
                    EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
                ]
                + [EvidencePointer(artifact_ref=p.patchset_id, json_pointer="/") for p in all_patchsets],
            )
        )
        write_checkpoint(
            run_root,
            run_id,
            CheckpointState(
                stage_name="generate",
                stage_index=1,
                event_cursor=event_cursor,
                artifact_refs=dict(artifact_refs),
                routing_state={"stage": "generate"},
                tool_side_effect_ledger_ref=None,
            ),
        )
        build_provenance: list[dict[str, object]] = []
        integration_repo_root: Path | None = None
        # Validate / integration stage
        event_cursor = self._event(run_root, run_id, stage="validate", event_type="stage_transition", payload={})
        self._execute_tool_calls(run_root, run_id, "validate", repo_context=repo_context)
        validate_refs = [artifact_refs["work_plan"]] + [p.patchset_id for p in all_patchsets]
        if features.get("v2_profiles") and "execution_profile_catalog" in artifact_refs:
            validate_refs.append(artifact_refs["execution_profile_catalog"])
        if features.get("v2_tool_registry") and "tool_registry" in artifact_refs:
            validate_refs.append(artifact_refs["tool_registry"])
        if features.get("v2_expectations") and "expectation_registry" in artifact_refs:
            validate_refs.append(artifact_refs["expectation_registry"])
        if features.get("v2_expectations") and "evidence_index" in artifact_refs:
            validate_refs.append(artifact_refs["evidence_index"])
        self._write_context_pack(
            workspace,
            run_id,
            stage="validate",
            role="validator",
            artifact_refs=validate_refs,
            evidence=[
                EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
            ]
            + [EvidencePointer(artifact_ref=p.patchset_id, json_pointer="/") for p in all_patchsets],
            required_refs=context_required if context_required else None,
        )

        integration_order = self._integration_order(work_plan, all_patchsets)
        integration_checkout = run_root / "integration_checkout"
        if integration_checkout.exists():
            shutil.rmtree(integration_checkout)
        shutil.copytree(repo_context.repo_root, integration_checkout)
        integration_context = repo_context.model_copy(update={"repo_root": str(integration_checkout)})
        conflict_policy = config.get("conflict_policy", {})
        try:
            changed_files, file_hashes, conflicts, skipped_patchsets, applied_patchsets, blocked = (
                self._apply_patchsets_with_conflict_policy(
                    integration_checkout,
                    integration_order,
                    integration_context,
                    patchset_limits,
                    run_root,
                    run_id,
                    stage="integration_validate",
                    approved_patchsets=approved_patchsets,
                    conflict_policy=conflict_policy if isinstance(conflict_policy, dict) else {},
                )
            )
        except (PatchApplyError, PatchLimitError) as exc:
            conflict_report = ConflictReportPayload(
                conflicts=[ConflictItem(path="unknown", reason=str(exc))],
                escalation="hitl",
            )
            artifact_refs["conflict_report"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "conflict_report",
                "conflict_report_v1",
                conflict_report.model_dump(),
                parents=[artifact_refs["work_plan"]],
                stage="validate",
            )
            raise
        if conflicts:
            conflict_report = ConflictReportPayload(
                conflicts=conflicts,
                escalation="hitl" if blocked else "auto_resolved",
            )
            artifact_refs["conflict_report"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "conflict_report",
                "conflict_report_v1",
                conflict_report.model_dump(),
                parents=[artifact_refs["work_plan"]],
                stage="validate",
            )
            if blocked:
                raise RuntimeError("Integration conflicts exceed deterministic thresholds")
        if not conflicts:
            skipped_patchsets = []
        integration_applied = applied_patchsets

        repo_snapshot = RepoSnapshotPayload(
            base_commit=repo_context.base_commit,
            head_commit=repo_context.head_commit,
            changed_files=sorted(set(changed_files)),
            file_hashes=file_hashes,
        )
        artifact_refs["repo_snapshot"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "repo_snapshot",
            "repo_snapshot_v1",
            repo_snapshot.model_dump(),
            parents=[artifact_refs["work_plan"]],
            stage="validate",
        )
        integration_report = IntegrationReportPayload(
            merge_order=[p.patchset_id for p in integration_order],
            base_commit=repo_context.base_commit,
            head_commit=repo_snapshot.head_commit,
            skipped_patchsets=skipped_patchsets,
        )
        artifact_refs["integration_report"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "integration_report",
            "integration_report_v1",
            integration_report.model_dump(),
            parents=[artifact_refs["work_plan"]],
            stage="validate",
        )
        integration_repo_root = integration_checkout
        test_results, quality_reports, quarantined = self._run_validators(
            integration_context, run_root, run_id, "integration_validate", retry_cap
        )
        build_checks, build_provenance = self._run_builds(
            repo_context,
            integration_repo_root,
            run_root,
            run_id,
            stage="integration_validate",
            retry_cap=retry_cap,
        )
        security_checks = self._run_security_checks(
            config,
            integration_repo_root,
            run_root,
            run_id,
            stage="integration_validate",
            retry_cap=retry_cap,
        )
        combined_checks = quality_reports.checks + build_checks + security_checks
        if (
            features.get("v2_profiles")
            and execution_profile_catalog
            and isinstance(repo_context, RepoContextPayloadV2)
        ):
            profile_checks = validate_profile_compliance(
                integration_repo_root,
                execution_profile_catalog,
                repo_context,
                work_plan if isinstance(work_plan, WorkPlanPayloadV2) else self._build_work_plan_v2(
                    plan,
                    patchset_limits,
                    repo_context,
                    expectation_registry or self._compile_expectation_registry_from_plan(plan),
                    execution_profile_catalog,
                    remote_ops_manifest,
                ),
                remote_ops_manifest,
                require_validator_runners=not compat_mode,
            )
            combined_checks.extend(profile_checks)
        quality_status = "PASS" if all(check.status != "fail" for check in combined_checks) else "FAIL"
        quality_reports = QualityReportsPayload(checks=combined_checks, overall_status=quality_status)
        if quarantined:
            self._handle_quarantine(
                run_root,
                run_id,
                stage="integration_validate",
                validator_ids=quarantined,
                store=store,
                workspace=workspace,
                config=config,
            )
        artifact_refs["test_results"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "test_results",
            "test_results_v1",
            test_results.model_dump(),
            parents=[artifact_refs["repo_snapshot"]],
            stage="validate",
        )
        artifact_refs["quality_reports"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "quality_reports",
            "quality_reports_v1",
            quality_reports.model_dump(),
            parents=[artifact_refs["repo_snapshot"]],
            stage="validate",
        )
        if features.get("v2_expectations") and expectation_registry and artifact_refs.get("evidence_index"):
            outcome_map = {test.id: "missing" for test in plan.acceptance_tests}
            for outcome in test_results.results:
                for test_id in outcome.maps_to_acceptance_tests:
                    outcome_map[test_id] = outcome.status
            updates: list[EvidenceIndexEntry] = []
            for test_id, status in outcome_map.items():
                exp_id = f"EXP-AT-{test_id}"
                updates.append(
                    EvidenceIndexEntry(
                        expectation_id=exp_id,
                        status="pass" if status == "pass" else "fail",
                        evidence_pointers=[
                            EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/")
                        ],
                    )
                )
            for req in plan.requirements:
                exp_id = f"EXP-PLAN-{req.id}"
                mapped_tests = [t.id for t in plan.acceptance_tests if req.id in t.maps_to_requirements]
                if not mapped_tests:
                    updates.append(
                        EvidenceIndexEntry(
                            expectation_id=exp_id,
                            status="missing",
                            evidence_pointers=[],
                        )
                    )
                    continue
                req_status = "pass"
                for test_id in mapped_tests:
                    if outcome_map.get(test_id) != "pass":
                        req_status = "fail"
                        break
                updates.append(
                    EvidenceIndexEntry(
                        expectation_id=exp_id,
                        status=req_status,
                        evidence_pointers=[
                            EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/")
                        ],
                    )
                )
            new_evidence_ref = self._update_evidence_index(
                store,
                workspace,
                run_id,
                artifact_refs["evidence_index"],
                updates,
                stage="validate",
            )
            artifact_refs["evidence_index"] = new_evidence_ref

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="integration_validate",
                to_stage="integration_review",
                objectives=["Validate integrated patchsets"],
                completed_refs=[artifact_refs["test_results"], artifact_refs["quality_reports"]],
                pending_blockers=[],
                next_actions=["Review integration outputs"],
                evidence=[
                    EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
                    EvidencePointer(artifact_ref=artifact_refs["quality_reports"], json_pointer="/"),
                ],
            )
        )
        if remote_validate_enabled:
            stage_handoffs.append(
                self._write_stage_handoff(
                    workspace,
                    run_id,
                    from_stage="validate",
                    to_stage="remote_validate",
                    objectives=["Prepare remote validation"],
                    completed_refs=[artifact_refs["test_results"], artifact_refs["quality_reports"]],
                    pending_blockers=[],
                    next_actions=["Execute remote ops"],
                    evidence=[
                        EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
                        EvidencePointer(artifact_ref=artifact_refs["quality_reports"], json_pointer="/"),
                    ],
                )
            )
            write_checkpoint(
                run_root,
                run_id,
                CheckpointState(
                    stage_name="validate",
                    stage_index=2,
                    event_cursor=event_cursor,
                    artifact_refs=dict(artifact_refs),
                    routing_state={"stage": "validate"},
                    tool_side_effect_ledger_ref=None,
                ),
            )
            # RemoteValidate stage
            event_cursor = self._event(
                run_root,
                run_id,
                stage="remote_validate",
                event_type="stage_transition",
                payload={},
            )
            self._execute_tool_calls(run_root, run_id, "remote_validate", repo_context=repo_context)
            remote_refs = [artifact_refs["work_plan"], artifact_refs["test_results"], artifact_refs["quality_reports"]]
            if "remote_ops_manifest" in artifact_refs:
                remote_refs.append(artifact_refs["remote_ops_manifest"])
            if "execution_profile_catalog" in artifact_refs:
                remote_refs.append(artifact_refs["execution_profile_catalog"])
            if "tool_registry" in artifact_refs:
                remote_refs.append(artifact_refs["tool_registry"])
            if "expectation_registry" in artifact_refs:
                remote_refs.append(artifact_refs["expectation_registry"])
            if "evidence_index" in artifact_refs:
                remote_refs.append(artifact_refs["evidence_index"])
            self._write_context_pack(
                workspace,
                run_id,
                stage="remote_validate",
                role="remote_validator",
                artifact_refs=remote_refs,
                evidence=[
                    EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
                    EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
                ],
                required_refs=context_required if context_required else None,
            )
            cache_payload = remote_run_cache or RemoteRunCacheIndexPayload(
                schema_version=IMPL_SCHEMA_VERSION_V2,
                entries=[],
            )
            remote_results, remote_events, cache_payload, _, diff_ids = self._run_remote_ops(
                remote_ops_manifest,
                expectation_registry,
                artifact_refs.get("evidence_index", ""),
                store,
                workspace,
                run_id,
                stage="remote_validate",
                repo_context=repo_context,
                profile_catalog=execution_profile_catalog,
                cache=cache_payload,
                retry_cap=retry_cap,
                manifest_ref=artifact_refs.get("remote_ops_manifest"),
                expectation_registry_ref=artifact_refs.get("expectation_registry"),
            )
            artifact_refs["remote_ops_results"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "remote_ops_results",
                "remote_ops_results_v2",
                remote_results.model_dump(),
                parents=[artifact_refs["test_results"]],
                stage="remote_validate",
            )
            artifact_refs["remote_op_events"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "remote_op_events",
                "remote_op_events_v2",
                remote_events.model_dump(),
                parents=[artifact_refs["remote_ops_results"]],
                stage="remote_validate",
            )
            workspace.write_canonical_log(
                "remote_op_events.jsonl",
                [event.model_dump() for event in remote_events.events],
                {"stage": "remote_validate", "role": "engine"},
            )
            if artifact_refs.get("remote_run_cache_index"):
                cache_id = artifact_refs["remote_run_cache_index"]
                new_cache_id = f"{cache_id}_patched"
                patched_cache = store.patch_artifact(
                    artifact_id=cache_id,
                    new_artifact_id=new_cache_id,
                    patch_ops=[
                        {
                            "op": "replace",
                            "path": "/entries",
                            "value": [e.model_dump() for e in cache_payload.entries],
                        }
                    ],
                    run_id=run_id,
                )
                workspace.index_artifact(
                    patched_cache,
                    "remote_run_cache_index",
                    self._schema_version,
                    {"stage": "remote_validate", "role": "engine"},
                )
                artifact_refs["remote_run_cache_index"] = new_cache_id
            if artifact_refs.get("evidence_index"):
                updates: list[EvidenceIndexEntry] = []
                op_expected_map = {op.op_id: op.expected for op in remote_ops_manifest.ops}
                for result in remote_results.results:
                    for exp_id in op_expected_map.get(result.op_id, []):
                        pointers = [
                            EvidencePointer(artifact_ref=artifact_refs["remote_ops_results"], json_pointer="/")
                        ]
                        if result.diff_report_ref:
                            pointers.append(EvidencePointer(artifact_ref=result.diff_report_ref, json_pointer="/"))
                        updates.append(
                            EvidenceIndexEntry(
                                expectation_id=exp_id,
                                status="pass" if result.evaluator_verdict == "pass" else "fail",
                                evidence_pointers=pointers,
                            )
                        )
                new_evidence_ref = self._update_evidence_index(
                    store,
                    workspace,
                    run_id,
                    artifact_refs["evidence_index"],
                    updates,
                    stage="remote_validate",
                )
                artifact_refs["evidence_index"] = new_evidence_ref
                evidence_payload = EvidenceIndexPayload.model_validate(store.read_artifact(new_evidence_ref).payload)
                artifacts_for_validation = {
                    artifact.artifact_id: artifact.payload
                    for artifact in store.list_artifacts()
                }
                validate_expectation_evidence_coverage(expectation_registry, evidence_payload, artifacts_for_validation)
            stage_handoffs.append(
                self._write_stage_handoff(
                    workspace,
                    run_id,
                    from_stage="remote_validate",
                    to_stage="review",
                    objectives=["Remote validation complete"],
                    completed_refs=[artifact_refs["remote_ops_results"]],
                    pending_blockers=[],
                    next_actions=["Review findings"],
                    evidence=[EvidencePointer(artifact_ref=artifact_refs["remote_ops_results"], json_pointer="/")],
                )
            )
            write_checkpoint(
                run_root,
                run_id,
                CheckpointState(
                    stage_name="remote_validate",
                    stage_index=3,
                    event_cursor=event_cursor,
                    artifact_refs=dict(artifact_refs),
                    routing_state={"stage": "remote_validate"},
                    tool_side_effect_ledger_ref=None,
                ),
            )
        else:
            stage_handoffs.append(
                self._write_stage_handoff(
                    workspace,
                    run_id,
                    from_stage="validate",
                    to_stage="review",
                    objectives=["Integration validation complete"],
                    completed_refs=[artifact_refs["test_results"], artifact_refs["quality_reports"]],
                    pending_blockers=[],
                    next_actions=["Review findings"],
                    evidence=[
                        EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
                        EvidencePointer(artifact_ref=artifact_refs["quality_reports"], json_pointer="/"),
                    ],
                )
            )
            write_checkpoint(
                run_root,
                run_id,
                CheckpointState(
                    stage_name="validate",
                    stage_index=2,
                    event_cursor=event_cursor,
                    artifact_refs=dict(artifact_refs),
                    routing_state={"stage": "validate"},
                    tool_side_effect_ledger_ref=None,
                ),
            )
        # Review stage
        event_cursor = self._event(run_root, run_id, stage="review", event_type="stage_transition", payload={})
        self._execute_tool_calls(run_root, run_id, "review", repo_context=repo_context)
        review_refs = [
            ref
            for ref in [
                artifact_refs["work_plan"],
                artifact_refs["test_results"],
                artifact_refs["quality_reports"],
                artifact_refs.get("integration_report"),
            ]
            if ref
        ]
        if features.get("v2_profiles") and "execution_profile_catalog" in artifact_refs:
            review_refs.append(artifact_refs["execution_profile_catalog"])
        if features.get("v2_tool_registry") and "tool_registry" in artifact_refs:
            review_refs.append(artifact_refs["tool_registry"])
        if features.get("v2_expectations") and "expectation_registry" in artifact_refs:
            review_refs.append(artifact_refs["expectation_registry"])
        if features.get("v2_expectations") and "evidence_index" in artifact_refs:
            review_refs.append(artifact_refs["evidence_index"])
        self._write_context_pack(
            workspace,
            run_id,
            stage="review",
            role=reviewer_role,
            artifact_refs=review_refs,
            evidence=[
                EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["quality_reports"], json_pointer="/"),
            ],
            required_refs=context_required if context_required else None,
        )

        integration_findings: list[ReviewFinding] = []
        if test_results.overall_status == "FAIL" or quality_reports.overall_status == "FAIL":
            integration_findings.append(
                ReviewFinding(
                    id="RF-INTEGRATION-1",
                    severity="high",
                    summary="Integration validation failed",
                    evidence=[EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/")],
                    violated_gate="validation",
                )
            )
        integration_review = ReviewFindingsPayload(findings=integration_findings)
        integration_review_id = "review_findings_integration_v1"
        self._write_artifact(
            store,
            workspace,
            run_id,
            "review_findings",
            integration_review_id,
            integration_review.model_dump(),
            parents=[artifact_refs["test_results"]],
            stage="review",
            role=reviewer_role,
        )

        aggregate_findings: list[ReviewFinding] = []
        for lane_result in lane_results:
            aggregate_findings.extend(lane_result.review_findings.findings)
        aggregate_findings.extend(integration_review.findings)
        review_findings = ReviewFindingsPayload(findings=aggregate_findings)
        artifact_refs["review_findings"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "review_findings",
            "review_findings_v1",
            review_findings.model_dump(),
            parents=[artifact_refs["test_results"], artifact_refs["quality_reports"]],
            stage="review",
            role=reviewer_role,
        )

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="integration_review",
                to_stage="integration_patch",
                objectives=["Review integration outputs"],
                completed_refs=[integration_review_id],
                pending_blockers=[],
                next_actions=["Apply repairs if needed"],
                evidence=[EvidencePointer(artifact_ref=integration_review_id, json_pointer="/")],
            )
        )

        if test_results.overall_status == "FAIL" or quality_reports.overall_status == "FAIL":
            existing_ids = {p.patchset_id for p in integration_order}
            integration_order, test_results, quality_reports = self._repair_patchsets(
                integration_order,
                repo_context,
                patchset_limits,
                run_root,
                run_id,
                stage="integration_patch",
                lane_id="integration",
                retry_cap=retry_cap,
                approved_patchsets=approved_patchsets,
            )
            for patchset in integration_order:
                if patchset.patchset_id in existing_ids:
                    continue
                self._write_artifact(
                    store,
                    workspace,
                    run_id,
                    "patchset",
                    patchset.patchset_id,
                    patchset.model_dump(),
                    parents=[artifact_refs["work_plan"]],
                    stage="integration_patch",
                    role=writer_role,
                )
            integration_order = self._integration_order(work_plan, integration_order)

            integration_checkout = run_root / "integration_checkout_repaired"
            if integration_checkout.exists():
                shutil.rmtree(integration_checkout)
            shutil.copytree(repo_context.repo_root, integration_checkout)
            integration_context = repo_context.model_copy(update={"repo_root": str(integration_checkout)})
            changed_files, file_hashes = self._apply_patchsets_to_checkout(
                integration_checkout,
                integration_order,
                integration_context,
                patchset_limits,
                run_root,
                run_id,
                stage="integration_patch",
                approved_patchsets=approved_patchsets,
            )
            repo_snapshot = RepoSnapshotPayload(
                base_commit=repo_context.base_commit,
                head_commit=repo_context.head_commit,
                changed_files=sorted(set(changed_files)),
                file_hashes=file_hashes,
            )
            artifact_refs["repo_snapshot"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "repo_snapshot",
                "repo_snapshot_v2",
                repo_snapshot.model_dump(),
                parents=[artifact_refs["work_plan"]],
                stage="review",
            )
            integration_report = IntegrationReportPayload(
                merge_order=[p.patchset_id for p in integration_order],
                base_commit=repo_context.base_commit,
                head_commit=repo_snapshot.head_commit,
                skipped_patchsets=[],
            )
            integration_applied = integration_order
            artifact_refs["integration_report"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "integration_report",
                "integration_report_v2",
                integration_report.model_dump(),
                parents=[artifact_refs["work_plan"]],
                stage="review",
            )
            integration_repo_root = integration_checkout
            build_checks, build_provenance = self._run_builds(
                repo_context,
                integration_repo_root,
                run_root,
                run_id,
                stage="integration_patch",
                retry_cap=retry_cap,
            )
            security_checks = self._run_security_checks(
                config,
                integration_repo_root,
                run_root,
                run_id,
                stage="integration_patch",
                retry_cap=retry_cap,
            )
            combined_checks = quality_reports.checks + build_checks + security_checks
            quality_status = "PASS" if all(check.status != "fail" for check in combined_checks) else "FAIL"
            quality_reports = QualityReportsPayload(checks=combined_checks, overall_status=quality_status)
            if test_results.quarantined_validators:
                self._handle_quarantine(
                    run_root,
                    run_id,
                    stage="integration_patch",
                    validator_ids=test_results.quarantined_validators,
                    store=store,
                    workspace=workspace,
                    config=config,
                )
            artifact_refs["test_results"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "test_results",
                "test_results_v2",
                test_results.model_dump(),
                parents=[artifact_refs["repo_snapshot"]],
                stage="review",
            )
            artifact_refs["quality_reports"] = self._write_artifact(
                store,
                workspace,
                run_id,
                "quality_reports",
                "quality_reports_v2",
                quality_reports.model_dump(),
                parents=[artifact_refs["repo_snapshot"]],
                stage="review",
            )

        json_repairs = config.get("json_repairs", [])
        if isinstance(json_repairs, list):
            for repair in json_repairs:
                if not isinstance(repair, dict):
                    continue
                target_id = str(repair.get("artifact_id", "")).strip()
                candidates = repair.get("candidates", [])
                if not target_id or not isinstance(candidates, list):
                    continue
                new_id = self._apply_json_repairs(store, workspace, run_id, target_id, candidates, stage="review")
                if new_id != target_id:
                    for key, value in list(artifact_refs.items()):
                        if value == target_id:
                            artifact_refs[key] = new_id

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="integration_patch",
                to_stage="integration_complete",
                objectives=["Finalize integration"],
                completed_refs=[p.patchset_id for p in integration_order],
                pending_blockers=[],
                next_actions=["Proceed to judge"],
                evidence=[EvidencePointer(artifact_ref=p.patchset_id, json_pointer="/") for p in integration_order],
            )
        )
        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="review",
                to_stage="judge",
                objectives=["Complete review"],
                completed_refs=[artifact_refs["review_findings"]],
                pending_blockers=[],
                next_actions=["Judge trace completeness"],
                evidence=[EvidencePointer(artifact_ref=artifact_refs["review_findings"], json_pointer="/")],
            )
        )
        write_checkpoint(
            run_root,
            run_id,
            CheckpointState(
                stage_name="review",
                stage_index=3 + stage_offset,
                event_cursor=event_cursor,
                artifact_refs=dict(artifact_refs),
                routing_state={"stage": "review"},
                tool_side_effect_ledger_ref=None,
            ),
        )
        # Judge stage
        event_cursor = self._event(run_root, run_id, stage="judge", event_type="stage_transition", payload={})
        self._execute_tool_calls(run_root, run_id, "judge", repo_context=repo_context)
        judge_refs = [
            ref
            for ref in [
                artifact_refs["work_plan"],
                artifact_refs["review_findings"],
                artifact_refs["test_results"],
                artifact_refs["quality_reports"],
                artifact_refs.get("integration_report"),
            ]
            if ref
        ]
        if features.get("v2_profiles") and "execution_profile_catalog" in artifact_refs:
            judge_refs.append(artifact_refs["execution_profile_catalog"])
        if features.get("v2_tool_registry") and "tool_registry" in artifact_refs:
            judge_refs.append(artifact_refs["tool_registry"])
        if features.get("v2_expectations") and "expectation_registry" in artifact_refs:
            judge_refs.append(artifact_refs["expectation_registry"])
        if features.get("v2_expectations") and "evidence_index" in artifact_refs:
            judge_refs.append(artifact_refs["evidence_index"])
        self._write_context_pack(
            workspace,
            run_id,
            stage="judge",
            role=judge_role,
            artifact_refs=judge_refs,
            evidence=[
                EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["review_findings"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["quality_reports"], json_pointer="/"),
            ],
            required_refs=context_required if context_required else None,
        )

        missing_links: list[str] = []
        for patchset in integration_applied:
            if not patchset.maps_to_requirements:
                missing_links.append(f"patchset:{patchset.patchset_id}")
        for skipped in skipped_patchsets:
            missing_links.append(f"skipped_patchset:{skipped}")
        acceptance_ids = {test.id for test in plan.acceptance_tests}
        mapped_acceptance: set[str] = set()
        unmapped_tests: list[str] = []
        for outcome in test_results.results:
            if outcome.maps_to_acceptance_tests:
                mapped_acceptance.update(outcome.maps_to_acceptance_tests)
            else:
                unmapped_tests.append(outcome.name)
        for test_id in sorted(acceptance_ids - mapped_acceptance):
            missing_links.append(f"acceptance:{test_id}")
        for test_name in unmapped_tests:
            missing_links.append(f"unmapped_test:{test_name}")
        trace_complete = len(missing_links) == 0
        trace_report = TraceReportPayload(
            trace_complete=trace_complete,
            missing_links=missing_links,
            evidence=[
                EvidencePointer(artifact_ref=artifact_refs["work_plan"], json_pointer="/"),
                EvidencePointer(artifact_ref=artifact_refs["test_results"], json_pointer="/"),
            ],
            recommendation="freeze" if trace_complete else "no_freeze",
        )
        artifact_refs["trace_report"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "trace_report",
            "trace_report_v1",
            trace_report.model_dump(),
            parents=[artifact_refs["review_findings"]],
            stage="judge",
        )

        stage_handoffs.append(
            self._write_stage_handoff(
                workspace,
                run_id,
                from_stage="judge",
                to_stage="freeze",
                objectives=["Finalize decision inputs"],
                completed_refs=[artifact_refs["trace_report"]],
                pending_blockers=[],
                next_actions=["Freeze if gates pass"],
                evidence=[EvidencePointer(artifact_ref=artifact_refs["trace_report"], json_pointer="/")],
            )
        )
        write_checkpoint(
            run_root,
            run_id,
            CheckpointState(
                stage_name="judge",
                stage_index=4 + stage_offset,
                event_cursor=event_cursor,
                artifact_refs=dict(artifact_refs),
                routing_state={"stage": "judge"},
                tool_side_effect_ledger_ref=None,
            ),
        )
        # Freeze stage
        event_cursor = self._event(run_root, run_id, stage="freeze", event_type="stage_transition", payload={})
        self._execute_tool_calls(run_root, run_id, "freeze", repo_context=repo_context)
        required_artifacts = [
            "work_plan",
            "repo_snapshot",
            "test_results",
            "quality_reports",
            "trace_report",
            "review_findings",
            "integration_report",
        ]
        for key in required_artifacts:
            if key not in artifact_refs:
                raise RuntimeError(f"Freeze blocked: missing artifact {key}")
        if test_results.overall_status != "PASS" or quality_reports.overall_status != "PASS":
            raise RuntimeError("Freeze blocked: validation gates failing")
        if not trace_report.trace_complete:
            raise RuntimeError("Freeze blocked: trace completeness failing")
        artifacts_for_evidence = {artifact.artifact_id: artifact.payload for artifact in store.list_artifacts()}
        if features.get("v2_expectations") and expectation_registry and artifact_refs.get("evidence_index"):
            evidence_payload = EvidenceIndexPayload.model_validate(
                store.read_artifact(artifact_refs["evidence_index"]).payload
            )
            validate_expectation_evidence_coverage(expectation_registry, evidence_payload, artifacts_for_evidence)
        if (
            features.get("v2_profiles")
            and execution_profile_catalog
            and isinstance(repo_context, RepoContextPayloadV2)
        ):
            profile_checks = validate_profile_compliance(
                integration_repo_root or Path(repo_context.repo_root),
                execution_profile_catalog,
                repo_context,
                work_plan if isinstance(work_plan, WorkPlanPayloadV2) else self._build_work_plan_v2(
                    plan,
                    patchset_limits,
                    repo_context,
                    expectation_registry or self._compile_expectation_registry_from_plan(plan),
                    execution_profile_catalog,
                    remote_ops_manifest,
                ),
                remote_ops_manifest,
                require_validator_runners=not compat_mode,
            )
            if any(check.status == "fail" for check in profile_checks):
                raise RuntimeError("Freeze blocked: profile compliance failing")
        if features.get("v2_research") and experiment_results:
            if not replication_report or replication_report.status != "pass":
                raise RuntimeError("Freeze blocked: replication report required for research results")
        validate_risk_closure(plan)
        self._reproducibility_check(
            run_root,
            repo_context,
            integration_applied,
            patchset_limits,
            repo_snapshot,
            run_id,
            approved_patchsets=approved_patchsets,
        )

        handoff_pairs: set[tuple[str, str]] = set()
        for handoff_path in workspace.stage_handoffs_root.glob("*.json"):
            handoff = StageHandoffPayload.model_validate_json(handoff_path.read_text(encoding="utf-8"))
            handoff_pairs.add((handoff.from_stage, handoff.to_stage))
        expected_pairs: set[tuple[str, str]] = {
            ("intake", "generate"),
            ("generate", "validate"),
            ("review", "judge"),
            ("judge", "freeze"),
            ("integration_validate", "integration_review"),
            ("integration_review", "integration_patch"),
            ("integration_patch", "integration_complete"),
        }
        if remote_validate_enabled:
            expected_pairs.add(("validate", "remote_validate"))
            expected_pairs.add(("remote_validate", "review"))
        else:
            expected_pairs.add(("validate", "review"))
        for lane in work_plan.lanes:
            tasks = self._lane_tasks(work_plan, lane.lane_id)
            if not tasks:
                continue
            expected_pairs.update(
                {
                    (f"lane_{lane.lane_id}_generate", f"lane_{lane.lane_id}_validate"),
                    (f"lane_{lane.lane_id}_validate", f"lane_{lane.lane_id}_review"),
                    (f"lane_{lane.lane_id}_review", f"lane_{lane.lane_id}_patch"),
                    (f"lane_{lane.lane_id}_patch", f"lane_{lane.lane_id}_complete"),
                }
            )
        missing_handoffs = expected_pairs.difference(handoff_pairs)
        if missing_handoffs:
            missing_list = ", ".join(f"{a}->{b}" for a, b in sorted(missing_handoffs))
            raise RuntimeError(f"Freeze blocked: missing stage handoffs {missing_list}")

        all_pointers: list[EvidencePointer] = []
        for artifact in store.list_artifacts():
            all_pointers.extend(collect_evidence_pointers(artifact.payload))
        for pack_path in workspace.context_packs_root.glob("*.json"):
            pack_data = json.loads(pack_path.read_text(encoding="utf-8"))
            all_pointers.extend(collect_evidence_pointers(pack_data))
        for handoff_path in workspace.stage_handoffs_root.glob("*.json"):
            handoff_data = json.loads(handoff_path.read_text(encoding="utf-8"))
            all_pointers.extend(collect_evidence_pointers(handoff_data))
        validate_evidence_pointers(all_pointers, artifacts_for_evidence)
        workspace.assert_index_complete()
        self._validate_role_outputs(workspace, role_sets)
        if store.list_artifacts("attestation_bundle"):
            for artifact in store.list_artifacts("attestation_bundle"):
                attestation = AttestationBundlePayload.model_validate(artifact.payload)
                validate_attestation_bundle(
                    attestation,
                    artifacts_for_evidence,
                    repo_context,
                    execution_profile_catalog,
                    repo_snapshot=repo_snapshot,
                    run_id=str(run_id),
                )

        for artifact in store.list_artifacts():
            if scan_for_secrets(artifact.payload):
                raise RuntimeError("Freeze blocked: secrets detected in artifacts")
        for pack_path in workspace.context_packs_root.glob("*.json"):
            pack_data = json.loads(pack_path.read_text(encoding="utf-8"))
            if scan_for_secrets(pack_data):
                raise RuntimeError("Freeze blocked: secrets detected in context packs")
        for handoff_path in workspace.stage_handoffs_root.glob("*.json"):
            handoff_data = json.loads(handoff_path.read_text(encoding="utf-8"))
            if scan_for_secrets(handoff_data):
                raise RuntimeError("Freeze blocked: secrets detected in stage handoffs")
        event_log_path = run_root / "implementation_event_log.jsonl"
        if event_log_path.exists():
            for line in event_log_path.read_text(encoding="utf-8").splitlines():
                if scan_for_secrets(line):
                    raise RuntimeError("Freeze blocked: secrets detected in event log")
        for note_path in workspace.canonical_root.rglob("*.json"):
            try:
                payload = json.loads(note_path.read_text(encoding="utf-8"))
            except Exception:
                payload = note_path.read_text(encoding="utf-8")
            if scan_for_secrets(payload):
                raise RuntimeError("Freeze blocked: secrets detected in canonical notes")
        for log_path in workspace.canonical_root.rglob("*.jsonl"):
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if scan_for_secrets(line):
                    raise RuntimeError("Freeze blocked: secrets detected in canonical logs")

        decision_record = DecisionRecordPayload(
            decision_id="DEC-IMPLEMENT-1",
            summary="Freeze approved",
            reasons=["All gates passed", "Trace complete"],
            what_would_change=["Failed validation", "Trace gaps"],
            evidence=[EvidencePointer(artifact_ref=artifact_refs["trace_report"], json_pointer="/")],
        )
        artifact_refs["decision_record"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "decision_record",
            "decision_record_v1",
            decision_record.model_dump(),
            parents=[artifact_refs["trace_report"]],
            stage="freeze",
            role=judge_role,
        )
        artifacts_for_evidence["decision_record"] = store.read_artifact(artifact_refs["decision_record"]).payload
        validate_evidence_pointers(decision_record.evidence, artifacts_for_evidence)
        release_root = integration_repo_root or Path(repo_context.repo_root)
        release_bundle = self._build_release_bundle(release_root, repo_snapshot, build_provenance, config)
        artifact_refs["release_bundle"] = self._write_artifact(
            store,
            workspace,
            run_id,
            "release_bundle",
            "release_bundle_v1",
            release_bundle.model_dump(),
            parents=[artifact_refs["decision_record"]],
            stage="freeze",
        )

        workspace.freeze()
        store.freeze()
        write_checkpoint(
            run_root,
            run_id,
            CheckpointState(
                stage_name="freeze",
                stage_index=5 + stage_offset,
                event_cursor=event_cursor,
                artifact_refs=dict(artifact_refs),
                routing_state={"stage": "freeze"},
                tool_side_effect_ledger_ref=None,
            ),
        )

        return ImplementationRunResult(run_id=run_id, run_root=run_root)

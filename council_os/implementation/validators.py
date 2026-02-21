from __future__ import annotations

import json
import re
try:
    import tomllib
except ImportError:  # pragma: no cover - Python <3.11
    tomllib = None
from pathlib import Path
from typing import Any

from council_os.agents.schemas import PlanPackage
from council_os.implementation.schemas import (
    AttestationBundlePayload,
    DependencyPolicy,
    ExecutionProfileCatalogPayload,
    EvidenceIndexPayload,
    EvidencePointer,
    Expectation,
    ExpectationRegistryPayload,
    PatchsetPayload,
    QualityCheck,
    RepoContextPayload,
    RepoContextPayloadV2,
    RepoContextPayloadV21,
    RepoSnapshotPayload,
    RemoteOpsManifestPayload,
    ToolProbeResultsPayload,
    ToolRegistryPayload,
    WorkPlanPayloadV2,
)
from council_os.implementation.secrets import scan_for_secrets
from council_os.utils import deterministic_json_hash


class ValidationError(Exception):
    pass


_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "dist",
    "build",
    "runs",
    "artifacts",
    ".idea",
    ".vscode",
}


def _is_ignored_path(path: Path, repo_root: Path) -> bool:
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    return any(part in _IGNORE_DIRS for part in rel.parts)


def resolve_json_pointer(data: Any, pointer: str) -> Any:
    if pointer == "":
        return data
    if pointer == "/":
        return data
    if not pointer.startswith("/"):
        raise ValidationError("json_pointer must start with '/'")
    current: Any = data
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValidationError(f"json_pointer missing key: {token}")
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError as exc:
                raise ValidationError(f"json_pointer index invalid: {token}") from exc
            if index < 0 or index >= len(current):
                raise ValidationError(f"json_pointer index out of range: {token}")
            current = current[index]
        else:
            raise ValidationError("json_pointer traversal failed")
    return current


def validate_evidence_pointers(pointers: list[EvidencePointer], artifacts: dict[str, dict[str, Any]]) -> None:
    for pointer in pointers:
        artifact = artifacts.get(pointer.artifact_ref)
        if artifact is None:
            raise ValidationError(f"Evidence artifact_ref not found: {pointer.artifact_ref}")
        resolve_json_pointer(artifact, pointer.json_pointer)


def collect_evidence_pointers(payload: Any) -> list[EvidencePointer]:
    pointers: list[EvidencePointer] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            if "artifact_ref" in value and "json_pointer" in value:
                try:
                    filtered = {
                        "artifact_ref": value.get("artifact_ref"),
                        "entity_id": value.get("entity_id"),
                        "json_pointer": value.get("json_pointer"),
                        "note": value.get("note"),
                    }
                    pointers.append(EvidencePointer.model_validate(filtered))
                except Exception as exc:  # pragma: no cover - defensive
                    raise ValidationError("Invalid evidence pointer object") from exc
            for item in value.values():
                _walk(item)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload)
    return pointers


def validate_plan_package_frozen(plan: PlanPackage) -> None:
    if plan.meta.plan_status != "FROZEN":
        raise ValidationError("plan_package_final must be frozen")


def validate_risk_closure(plan: PlanPackage) -> None:
    for risk in plan.risk_register:
        if risk.severity in {"high", "critical"}:
            has_mitigation = risk.mitigation is not None and bool(risk.mitigation.text.strip())
            has_acceptance = risk.acceptance is not None and bool(risk.acceptance.signoff.strip())
            if not (has_mitigation or has_acceptance):
                raise ValidationError(f"Risk {risk.id} missing closure")


def validate_patchset_semantics(
    patchset: PatchsetPayload,
    plan: PlanPackage,
    change_request_refs: set[str],
    change_request_decisions: set[str] | None = None,
) -> None:
    plan_ids = {req.id for req in plan.requirements}
    plan_ids.update({test.id for test in plan.acceptance_tests})
    plan_ids.update({opt.id for opt in plan.architecture.options})
    plan_ids.update({risk.id for risk in plan.risk_register})
    referenced = set(patchset.maps_to_requirements) | set(patchset.maps_to_acceptance_tests)
    unknown = referenced.difference(plan_ids)
    if patchset.semantic_change:
        if not patchset.change_request_ref or patchset.change_request_ref not in change_request_refs:
            raise ValidationError("Semantic change requires approved ChangeRequest")
        if change_request_decisions is not None and patchset.change_request_ref not in change_request_decisions:
            raise ValidationError("Semantic change requires decision record")
    if unknown:
        if not patchset.change_request_ref or patchset.change_request_ref not in change_request_refs:
            raise ValidationError("Semantic change requires approved ChangeRequest")
        if change_request_decisions is not None and patchset.change_request_ref not in change_request_decisions:
            raise ValidationError("Semantic change requires decision record")


def validate_forbidden_paths(
    patchset: PatchsetPayload,
    repo_context: RepoContextPayload | RepoContextPayloadV2 | RepoContextPayloadV21,
    *,
    allow_protected: bool = False,
) -> None:
    from fnmatch import fnmatch

    forbidden = repo_context.forbidden_paths
    protected = repo_context.protected_paths
    for change in patchset.changes:
        path = change.path.replace("\\", "/").lstrip("/")
        if any(fnmatch(path, pattern) for pattern in forbidden):
            raise ValidationError(f"Patchset touches forbidden path: {path}")
        if not allow_protected and any(fnmatch(path, pattern) for pattern in protected):
            raise ValidationError(f"Patchset touches protected path: {path}")


def _scan_imports(repo_root: Path) -> set[str]:
    imports: set[str] = set()
    for path in repo_root.rglob("*.py"):
        if _is_ignored_path(path, repo_root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for match in re.findall(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", text, flags=re.MULTILINE):
            root = match.split(".", 1)[0]
            imports.add(root)
    return imports


_DEPENDENCY_FILES = {
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
_CONDA_FILES = {"environment.yml", "environment.yaml", "conda.yml"}
_NETWORK_TOKENS = ("http://", "https://", "git+", "git@", "ssh://", "svn+", "hg+")
_NETWORK_SCAN_FILES = {
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "pipfile",
    "package.json",
    "environment.yml",
    "environment.yaml",
    "conda.yml",
}


def _dependency_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if _is_ignored_path(path, repo_root):
            continue
        name = path.name.lower()
        if name in _DEPENDENCY_FILES or name in _CONDA_FILES:
            files.append(path)
    return files


def _scan_dependency_files(repo_root: Path, forbidden: set[str]) -> set[str]:
    hits: set[str] = set()
    if not forbidden:
        return hits
    lowered = {name.lower() for name in forbidden}
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in lowered) + r")\b", flags=re.IGNORECASE)
    for path in _dependency_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8").lower()
        except Exception:
            continue
        for match in pattern.findall(text):
            hits.add(match)
    return hits


def _string_has_network(value: str) -> bool:
    lowered = value.lower().strip()
    if any(lowered.startswith(token) for token in _NETWORK_TOKENS):
        return True
    if "@" in lowered:
        _, suffix = lowered.split("@", 1)
        if any(token in suffix for token in _NETWORK_TOKENS):
            return True
    return any(token in lowered for token in ("git+", "git@", "ssh://", "svn+", "hg+"))


def _dependency_value_has_network(value: object) -> bool:
    if isinstance(value, str):
        return _string_has_network(value)
    if isinstance(value, dict):
        for key in ("git", "url", "hg", "svn", "bzr"):
            if key in value:
                return True
    return False


def _scan_package_json(data: dict[str, object]) -> bool:
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            for dep_value in deps.values():
                if _dependency_value_has_network(dep_value):
                    return True
    return False


def _scan_pyproject_toml(data: dict[str, object]) -> bool:
    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            if any(_dependency_value_has_network(item) for item in deps):
                return True
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for items in optional.values():
                if isinstance(items, list) and any(_dependency_value_has_network(item) for item in items):
                    return True
    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            for key in ("dependencies", "dev-dependencies"):
                deps = poetry.get(key)
                if isinstance(deps, dict):
                    if any(_dependency_value_has_network(item) for item in deps.values()):
                        return True
            groups = poetry.get("group")
            if isinstance(groups, dict):
                for group in groups.values():
                    if isinstance(group, dict):
                        deps = group.get("dependencies")
                        if isinstance(deps, dict):
                            if any(_dependency_value_has_network(item) for item in deps.values()):
                                return True
        pdm = tool.get("pdm")
        if isinstance(pdm, dict):
            deps = pdm.get("dependencies")
            if isinstance(deps, list) and any(_dependency_value_has_network(item) for item in deps):
                return True
    return False


def _scan_lines_for_network(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("--", "-r", "-c")):
            continue
        lower = line.lower()
        if any(token in lower for token in ("project_url", "project-url", "homepage", "documentation", "repository", "bugtracker", "url=")):
            continue
        if _string_has_network(line):
            return True
    return False


def _scan_network_installs(repo_root: Path) -> list[str]:
    hits: list[str] = []
    for path in _dependency_files(repo_root):
        name = path.name.lower()
        if name not in _NETWORK_SCAN_FILES:
            continue
        try:
            if name == "package.json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and _scan_package_json(payload):
                    hits.append(path.name)
                continue
            if name.endswith(".toml") and tomllib is not None:
                payload = tomllib.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and _scan_pyproject_toml(payload):
                    hits.append(path.name)
                continue
        except Exception:
            pass
        if _scan_lines_for_network(path):
            hits.append(path.name)
    return hits


def _has_dependency_file(repo_root: Path, names: list[str]) -> bool:
    for name in names:
        if (repo_root / name).exists():
            return True
    return False


def validate_profile_compliance(
    repo_root: Path,
    profile_catalog: ExecutionProfileCatalogPayload,
    repo_context: RepoContextPayloadV2 | RepoContextPayloadV21,
    work_plan: WorkPlanPayloadV2,
    remote_ops_manifest: RemoteOpsManifestPayload | None = None,
    require_validator_runners: bool = True,
) -> list[QualityCheck]:
    checks: list[QualityCheck] = []
    catalog = {profile.profile_id: profile for profile in profile_catalog.profiles}
    used_profiles: set[str] = {task.profile_id for task in work_plan.tasks}
    if remote_ops_manifest:
        used_profiles.update({op.profile_id for op in remote_ops_manifest.ops})
    allowed_profiles = (
        set(repo_context.execution_profiles.allowed_profile_ids)
        if repo_context.execution_profiles
        else set()
    )
    imports = _scan_imports(repo_root)
    dependency_policy: DependencyPolicy | None = repo_context.dependency_policy
    dependency_hits: set[str] = set()

    for profile_id in sorted(used_profiles):
        profile = catalog.get(profile_id)
        if profile is None:
            checks.append(
                QualityCheck(
                    name=f"profile:{profile_id}",
                    status="fail",
                    details="profile_id not found in execution profile catalog",
                )
            )
            continue
        if allowed_profiles and profile_id not in allowed_profiles:
            checks.append(
                QualityCheck(
                    name=f"profile:{profile_id}:allowed",
                    status="fail",
                    details="profile_id not in allowed_profile_ids",
                )
            )
        forbidden = {dep.name for dep in profile.deps.forbidden}
        blocked = sorted(forbidden & imports)
        if blocked:
            checks.append(
                QualityCheck(
                    name=f"profile:{profile_id}:forbidden_imports",
                    status="fail",
                    details="forbidden imports detected: " + ", ".join(blocked),
                )
            )
        dependency_hits.update(_scan_dependency_files(repo_root, forbidden))
        if profile.package_manager and profile.package_manager.pip_install == "deny":
            if _has_dependency_file(
                repo_root,
                ["requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.cfg", "setup.py", "pipfile", "pipfile.lock"],
            ):
                checks.append(
                    QualityCheck(
                        name=f"profile:{profile_id}:pip_install",
                        status="fail",
                        details="pip install denied but dependency files present",
                    )
                )
        if profile.package_manager and profile.package_manager.conda_install == "deny":
            if _has_dependency_file(repo_root, ["environment.yml", "environment.yaml", "conda.yml"]):
                checks.append(
                    QualityCheck(
                        name=f"profile:{profile_id}:conda_install",
                        status="fail",
                        details="conda install denied but conda environment files present",
                    )
                )
        if dependency_policy and dependency_policy.required_lockfiles:
            missing = [
                name for name in dependency_policy.required_lockfiles if not (repo_root / name).exists()
            ]
            if missing:
                checks.append(
                    QualityCheck(
                        name=f"profile:{profile_id}:lockfiles",
                        status="fail",
                        details="missing lockfiles: " + ", ".join(missing),
                    )
                )
        if dependency_policy and not dependency_policy.network_installs_allowed:
            network_hits = _scan_network_installs(repo_root)
            if network_hits:
                checks.append(
                    QualityCheck(
                        name=f"profile:{profile_id}:network_installs",
                        status="fail",
                        details="network installs denied but dependency files include network refs: "
                        + ", ".join(sorted(set(network_hits))),
                    )
                )

    if dependency_hits:
        checks.append(
            QualityCheck(
                name="profile:forbidden_dependencies",
                status="fail",
                details="forbidden dependencies detected in lockfiles/manifests: "
                + ", ".join(sorted(dependency_hits)),
            )
        )

    if repo_context.validator_runners:
        runner_map = {runner.name: runner for runner in repo_context.validator_runners}
        for runner in repo_context.validator_runners:
            if runner.profile_id not in catalog:
                checks.append(
                    QualityCheck(
                        name=f"profile:validator:{runner.name}",
                        status="fail",
                        details="validator runner profile_id missing from catalog",
                    )
                )
            if allowed_profiles and runner.profile_id not in allowed_profiles:
                checks.append(
                    QualityCheck(
                        name=f"profile:validator:{runner.name}:allowed",
                        status="fail",
                        details="validator runner profile_id not in allowed_profile_ids",
                    )
                )
        for validator in repo_context.validator_entrypoints:
            if validator.name not in runner_map and validator.id not in runner_map:
                checks.append(
                    QualityCheck(
                        name=f"profile:validator:{validator.name}:missing",
                        status="fail",
                        details="validator entrypoint missing validator_runner profile mapping",
                    )
                )
    elif work_plan.tasks and require_validator_runners:
        checks.append(
            QualityCheck(
                name="profile:validator_runners",
                status="fail",
                details="validator_runners missing for v2 profile compliance",
            )
        )

    if not checks:
        checks.append(QualityCheck(name="profile:compliance", status="pass", details="ok"))
    return checks


def validate_tool_registry(
    registry: ToolRegistryPayload,
    artifacts: dict[str, dict[str, Any]],
    repo_context: RepoContextPayload | RepoContextPayloadV2 | RepoContextPayloadV21 | None = None,
    profile_catalog: ExecutionProfileCatalogPayload | None = None,
    run_id: str | None = None,
) -> None:
    for tool in registry.tools:
        if tool.probe_status in {"unprobed", "exempt"}:
            continue
        if tool.last_probe is None:
            raise ValidationError(f"Tool {tool.tool_id} missing last_probe evidence pointer")
        validate_evidence_pointers([tool.last_probe], artifacts)
        probe_payload = artifacts.get(tool.last_probe.artifact_ref)
        if probe_payload is None:
            raise ValidationError(f"Tool probe artifact missing: {tool.last_probe.artifact_ref}")
        probe_obj = resolve_json_pointer(probe_payload, tool.last_probe.json_pointer)
        if not isinstance(probe_obj, dict) or not probe_obj.get("attestation_ref"):
            raise ValidationError(f"Tool probe result missing attestation for {tool.tool_id}")
        att_ref = probe_obj.get("attestation_ref")
        att_payload = artifacts.get(att_ref) if isinstance(att_ref, str) else None
        if att_payload is None:
            raise ValidationError(f"Tool probe attestation missing for {tool.tool_id}")
        if repo_context and profile_catalog:
            attestation = AttestationBundlePayload.model_validate(att_payload)
            validate_attestation_bundle(attestation, artifacts, repo_context, profile_catalog, run_id=run_id)
        if scan_for_secrets(probe_obj):
            raise ValidationError("Secrets detected in tool probe result")


def _oracle_valid(expectation: Expectation) -> None:
    oracle = expectation.oracle
    if oracle.type not in {
        "schema_exact",
        "invariants",
        "metrics_threshold",
        "golden_hash",
        "differential_baseline",
        "exists",
    }:
        raise ValidationError(f"Unknown oracle type: {oracle.type}")
    if oracle.type == "schema_exact" and not oracle.schema_ref:
        raise ValidationError("schema_exact oracle requires schema_ref")
    if oracle.type == "invariants" and not oracle.checks:
        raise ValidationError("invariants oracle requires checks")
    if oracle.type == "golden_hash" and not oracle.expected_hash:
        raise ValidationError("golden_hash oracle requires expected_hash")
    if oracle.type == "differential_baseline" and not oracle.baseline_ref:
        raise ValidationError("differential_baseline oracle requires baseline_ref")


def validate_expectation_registry(
    registry: ExpectationRegistryPayload,
    artifacts: dict[str, dict[str, Any]],
    remote_ops_manifest: RemoteOpsManifestPayload | None = None,
) -> None:
    ids_list = [exp.expectation_id for exp in registry.expectations]
    ids = set(ids_list)
    if len(ids_list) != len(ids):
        counts: dict[str, int] = {}
        for exp_id in ids_list:
            counts[exp_id] = counts.get(exp_id, 0) + 1
        duplicates = sorted([exp_id for exp_id, count in counts.items() if count > 1])
        raise ValidationError(f"Expectation registry contains duplicate ids: {duplicates}")
    exp_map = {exp.expectation_id: exp for exp in registry.expectations}
    # Oracle checks
    for exp in registry.expectations:
        _oracle_valid(exp)
        for inp in exp.inputs:
            if inp.dataset_snapshot_ref not in artifacts:
                raise ValidationError(f"Expectation input missing: {inp.dataset_snapshot_ref}")
    # DAG cycle check
    graph = {exp.expectation_id: exp.children for exp in registry.expectations}
    for exp in registry.expectations:
        missing_children = set(exp.children).difference(ids)
        missing_parents = set(exp.parents).difference(ids)
        if missing_children or missing_parents:
            raise ValidationError(
                "Expectation registry contains dangling references: "
                f"{exp.expectation_id} children_missing={sorted(missing_children)} "
                f"parents_missing={sorted(missing_parents)}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def _dfs(node: str) -> None:
        if node in visiting:
            raise ValidationError("Expectation registry contains cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, []):
            _dfs(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        _dfs(node)
    if remote_ops_manifest:
        for op in remote_ops_manifest.ops:
            missing = set(op.expected).difference(ids)
            if missing:
                raise ValidationError(f"Remote op {op.op_id} references missing expectations: {missing}")
            for exp_id in op.expected:
                exp = exp_map.get(exp_id)
                if exp and not exp.must_exist_before_remote_ops:
                    raise ValidationError(
                        f"Remote op {op.op_id} references expectation not marked must_exist_before_remote_ops: {exp_id}"
                    )


def validate_expectation_evidence_coverage(
    registry: ExpectationRegistryPayload,
    evidence_index: EvidenceIndexPayload,
    artifacts: dict[str, dict[str, Any]] | None = None,
) -> None:
    index = {entry.expectation_id: entry for entry in evidence_index.evidence}
    for exp in registry.expectations:
        if exp.level != "plan":
            continue
        entry = index.get(exp.expectation_id)
        if entry is None:
            raise ValidationError(f"Expectation evidence missing: {exp.expectation_id}")
        if entry.status == "pass":
            if not entry.evidence_pointers:
                raise ValidationError(f"Expectation evidence pointers missing: {exp.expectation_id}")
            if artifacts is not None:
                validate_evidence_pointers(entry.evidence_pointers, artifacts)
            continue
        if entry.status == "waived" and entry.waiver_approvals:
            if not entry.evidence_pointers:
                raise ValidationError(f"Expectation waiver missing evidence pointers: {exp.expectation_id}")
            if artifacts is not None:
                validate_evidence_pointers(entry.evidence_pointers, artifacts)
            continue
        raise ValidationError(f"Expectation evidence coverage failed: {exp.expectation_id}")


def validate_attestation_bundle(
    attestation: AttestationBundlePayload,
    artifacts: dict[str, dict[str, Any]],
    repo_context: RepoContextPayload | RepoContextPayloadV2 | RepoContextPayloadV21,
    profile_catalog: ExecutionProfileCatalogPayload | None,
    repo_snapshot: RepoSnapshotPayload | None = None,
    run_id: str | None = None,
) -> None:
    code_sha = attestation.code_sha
    base_commit = repo_snapshot.base_commit if repo_snapshot else repo_context.base_commit
    head_commit = repo_snapshot.head_commit if repo_snapshot else repo_context.head_commit
    if run_id and str(attestation.run_id) != str(run_id):
        raise ValidationError("Attestation run_id does not match current run")
    if not attestation.run_id:
        raise ValidationError("Attestation run_id missing")
    if code_sha.get("base_commit") != base_commit or code_sha.get("head_commit") != head_commit:
        raise ValidationError("Attestation code_sha does not match repo_context commits")
    if not attestation.timestamps.get("start") or not attestation.timestamps.get("end"):
        raise ValidationError("Attestation timestamps missing start/end")
    if not attestation.runner_version:
        raise ValidationError("Attestation runner_version missing")
    if "implementation" not in attestation.spec_versions:
        raise ValidationError("Attestation spec_versions missing implementation")
    if profile_catalog:
        profiles = {p.profile_id: p for p in profile_catalog.profiles}
        profile = profiles.get(attestation.profile_id)
        if profile and profile.base_image.digest != attestation.base_image_digest:
            raise ValidationError("Attestation base_image_digest mismatch")
    for inp in attestation.inputs:
        payload = artifacts.get(inp.artifact_ref)
        if payload is None:
            if inp.hash is None:
                continue
            raise ValidationError(f"Attestation input missing: {inp.artifact_ref}")
        if inp.hash and inp.hash != deterministic_json_hash(payload):
            raise ValidationError(f"Attestation hash mismatch for {inp.artifact_ref}")


def _schema_type_ok(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _minimal_schema_validate(schema: dict[str, Any], output: Any) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _schema_type_ok(expected_type, output):
        diffs.append({"reason": "type_mismatch", "expected": expected_type})
        return diffs
    if expected_type == "object" and isinstance(output, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in output:
                    diffs.append({"reason": "missing_required", "key": key})
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, prop_schema in properties.items():
                if key not in output or not isinstance(prop_schema, dict):
                    continue
                prop_type = prop_schema.get("type")
                if isinstance(prop_type, str) and not _schema_type_ok(prop_type, output.get(key)):
                    diffs.append({"reason": "property_type_mismatch", "key": key, "expected": prop_type})
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            extras = set(output.keys()) - set(properties.keys())
            if extras:
                diffs.append({"reason": "additional_properties", "keys": sorted(extras)})
    if expected_type == "array" and isinstance(output, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            item_type = items_schema.get("type")
            if isinstance(item_type, str):
                for idx, item in enumerate(output):
                    if not _schema_type_ok(item_type, item):
                        diffs.append({"reason": "item_type_mismatch", "index": idx, "expected": item_type})
    return diffs


def evaluate_expectation(
    expectation: Expectation,
    output: Any,
    artifacts: dict[str, dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    oracle = expectation.oracle
    diffs: list[dict[str, Any]] = []
    if oracle.type == "exists":
        return (output is not None, diffs)
    if oracle.type == "schema_exact":
        if not oracle.schema_ref:
            return False, [{"reason": "schema_ref_missing"}]
        schema_payload = artifacts.get(oracle.schema_ref)
        if schema_payload is None:
            return False, [{"reason": "schema_ref_not_found"}]
        schema = schema_payload.get("schema") if isinstance(schema_payload, dict) else None
        if schema is None and isinstance(schema_payload, dict):
            schema = schema_payload
        if not isinstance(schema, dict):
            return False, [{"reason": "schema_invalid"}]
        schema_error: str | None = None
        try:
            import jsonschema  # type: ignore[import-not-found]

            jsonschema.validate(output, schema)
            return True, diffs
        except ImportError:
            schema_error = None
        except Exception as exc:
            schema_error = str(exc)
        diffs = _minimal_schema_validate(schema, output)
        if schema_error:
            diffs.append({"reason": "schema_validation_failed", "error": schema_error})
        return (len(diffs) == 0), diffs
    if oracle.type == "golden_hash":
        digest = deterministic_json_hash(output)
        if digest == (oracle.expected_hash or ""):
            return True, diffs
        return False, [{"expected": oracle.expected_hash, "actual": digest}]
    if oracle.type == "differential_baseline":
        baseline = artifacts.get(oracle.baseline_ref or "")
        if baseline is None:
            return False, [{"reason": "baseline_missing"}]
        if not isinstance(output, dict) or not isinstance(baseline, dict):
            return False, [{"reason": "baseline_type_mismatch"}]
        tolerance = expectation.tolerance.abs if expectation.tolerance else 0.0
        for key, value in output.items():
            if key in baseline and isinstance(value, (int, float)) and isinstance(baseline[key], (int, float)):
                diff = abs(value - baseline[key])
                if diff > tolerance:
                    diffs.append({"key": key, "diff": diff, "tolerance": tolerance})
        return (len(diffs) == 0, diffs)
    if oracle.type == "metrics_threshold":
        metric = oracle.metric or (oracle.checks[0].metric if oracle.checks else None)
        threshold = oracle.threshold
        if metric is None or threshold is None:
            return False, [{"reason": "metrics_threshold_missing"}]
        if isinstance(output, dict) and isinstance(output.get(metric), (int, float)):
            value = float(output[metric])
            if value >= threshold:
                return True, diffs
            return False, [{"metric": metric, "value": value, "threshold": threshold}]
        return False, [{"reason": "metric_missing"}]
    if oracle.type == "invariants":
        all_ok = True
        for check in oracle.checks:
            path = check.path or ""
            try:
                value = resolve_json_pointer(output, path) if path else output
            except ValidationError as exc:
                diffs.append({"check": check.type, "error": str(exc)})
                all_ok = False
                continue
            if check.type == "no_nulls":
                if isinstance(value, list):
                    ok = all(item is not None for item in value)
                else:
                    ok = value is not None
                if not ok:
                    diffs.append({"check": "no_nulls", "path": path})
                    all_ok = False
            elif check.type in {"ge", "le", "eq"}:
                if not isinstance(value, (int, float)):
                    diffs.append({"check": check.type, "path": path, "reason": "non_numeric"})
                    all_ok = False
                else:
                    target = float(check.value) if check.value is not None else 0.0
                    if check.type == "ge" and value < target:
                        diffs.append({"check": "ge", "path": path, "value": value, "target": target})
                        all_ok = False
                    if check.type == "le" and value > target:
                        diffs.append({"check": "le", "path": path, "value": value, "target": target})
                        all_ok = False
                    if check.type == "eq" and value != target:
                        diffs.append({"check": "eq", "path": path, "value": value, "target": target})
                        all_ok = False
        return all_ok, diffs
    return False, [{"reason": "oracle_unhandled"}]

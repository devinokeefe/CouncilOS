#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from council_os.implementation.schemas import (
    ExecutionProfilesConfig,
    RepoContextPayload,
    RepoContextPayloadV2,
    ValidatorRunner,
    WorkPlanPayload,
    WorkPlanPayloadV2,
    WorkTaskV2,
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate v1 implementation artifacts to v2.")
    parser.add_argument("--repo-context", required=True, help="Path to repo_context.v1.json")
    parser.add_argument("--work-plan", required=True, help="Path to work_plan.v1.json")
    parser.add_argument("--test-results", required=False, help="Path to test_results.v1.json")
    parser.add_argument("--output-dir", required=True, help="Directory to write v2 artifacts")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    repo_context_v1 = RepoContextPayload.model_validate(_load_json(Path(args.repo_context)))
    work_plan_v1 = WorkPlanPayload.model_validate(_load_json(Path(args.work_plan)))
    test_results = None
    if args.test_results:
        from council_os.implementation.schemas import TestResultsPayload

        test_results = TestResultsPayload.model_validate(_load_json(Path(args.test_results)))

    default_profile = "legacy_v1"
    exec_profiles = ExecutionProfilesConfig(
        catalog_ref="artifact_ref:execution_profile_catalog@v2",
        allowed_profile_ids=[default_profile],
        default_profile_id_by_stage={"Generate": default_profile, "Validate": default_profile, "RemoteValidate": default_profile},
    )

    validator_runners = [
        ValidatorRunner(name=entry.name, cmd=" ".join(entry.command or []), profile_id=default_profile)
        for entry in repo_context_v1.validator_entrypoints
    ]
    repo_context_v2 = RepoContextPayloadV2(
        repo_root=repo_context_v1.repo_root,
        base_commit=repo_context_v1.base_commit,
        head_commit=repo_context_v1.head_commit,
        branch_name=repo_context_v1.branch_name,
        build_entrypoints=repo_context_v1.build_entrypoints,
        validator_entrypoints=repo_context_v1.validator_entrypoints,
        protected_paths=repo_context_v1.protected_paths,
        forbidden_paths=repo_context_v1.forbidden_paths,
        metadata=repo_context_v1.metadata,
        execution_profiles=exec_profiles,
        validator_runners=validator_runners,
        dependency_policy=None,
    )

    tasks_v2: list[WorkTaskV2] = []
    for task in work_plan_v1.tasks:
        verifies = [f"EXP-AT-{test_id}" for test_id in task.maps_to_acceptance_tests]
        tasks_v2.append(
            WorkTaskV2(
                task_id=task.task_id,
                title=task.title,
                lane_id=task.lane_id,
                depends_on=task.depends_on,
                maps_to_requirements=task.maps_to_requirements,
                maps_to_acceptance_tests=task.maps_to_acceptance_tests,
                profile_id=default_profile,
                satisfies_expectations=[],
                verifies_expectations=verifies,
                remote_ops=[],
            )
        )

    work_plan_v2 = WorkPlanPayloadV2(
        lanes=work_plan_v1.lanes,
        tasks=tasks_v2,
        patchset_limits=work_plan_v1.patchset_limits,
        generated_at=work_plan_v1.generated_at,
    )

    mapped_acceptance = []
    if test_results:
        for outcome in test_results.results:
            mapped_acceptance.extend(outcome.maps_to_acceptance_tests)
    report = {
        "missing_validator_profiles": [
            entry.name
            for entry in repo_context_v1.validator_entrypoints
            if not entry.command
        ],
        "default_profile_id": default_profile,
        "test_results_loaded": bool(test_results),
        "mapped_acceptance_tests": sorted(set(mapped_acceptance)),
    }

    (out_dir / "repo_context.v2.json").write_text(repo_context_v2.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "work_plan.v2.json").write_text(work_plan_v2.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "migration_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

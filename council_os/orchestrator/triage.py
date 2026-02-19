from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import jsonpatch

from council_os.agents.schemas import (
    EvidencePointer,
    PlanCandidatePayload,
    PlanPackage,
    TriageDefect,
    TriageFindingsPayload,
)
from council_os.validation.validators import validate_candidate


@dataclass(frozen=True)
class TriageRepairResult:
    updated_plan: PlanPackage
    repaired: bool


def _candidate_pointer(candidate_id: str, artifact_ref: str, pointer: str) -> list[EvidencePointer]:
    return [
        EvidencePointer(
            candidate_id=candidate_id,
            artifact_ref=artifact_ref,
            artifact_id=candidate_id,
            json_pointer=pointer,
            note="triage-generated evidence",
        )
    ]


def _normalize_patch_path(path: str) -> str:
    if path.startswith("/plan_package"):
        return path.replace("/plan_package", "", 1) or "/"
    return path


def _next_at_ids(candidate: PlanCandidatePayload, count: int) -> list[str]:
    existing_nums: list[int] = []
    for at in candidate.plan_package.acceptance_tests:
        if at.id.startswith("AT") and at.id[2:].isdigit():
            existing_nums.append(int(at.id[2:]))
    base = max(existing_nums, default=0)
    return [f"AT{base + idx}" for idx in range(1, count + 1)]


def critic_findings(
    candidate_ref: str,
    candidate: PlanCandidatePayload,
    critic_id: Literal["A", "B"],
) -> TriageFindingsPayload:
    defects: list[TriageDefect] = []
    plan = candidate.plan_package

    must_requirements = {r.id for r in plan.requirements if r.priority == "MUST"}
    mapped = {rid for at in plan.acceptance_tests for rid in at.maps_to_requirements}
    missing = sorted(must_requirements - mapped)
    if missing:
        generated_ids = _next_at_ids(candidate, len(missing))
        patch_ops = []
        for idx, req_id in enumerate(missing):
            patch_ops.append(
                {
                    "op": "add",
                    "path": "/plan_package/acceptance_tests/-",
                    "value": {
                        "id": generated_ids[idx],
                        "maps_to_requirements": [req_id],
                        "type": "system",
                        "procedure": f"Validate requirement {req_id}",
                        "pass_criteria": f"Requirement {req_id} is satisfied",
                    },
                }
            )
        defects.append(
            TriageDefect(
                label="blocker",
                severity="critical",
                summary="MUST requirement missing acceptance coverage",
                evidence=_candidate_pointer(candidate.candidate_id, candidate_ref, "/plan_package/acceptance_tests"),
                violated_gate="coverage",
                patch=patch_ops,
            )
        )

    weak_milestones = [m.id for m in plan.milestones if not m.exit_criteria]
    if weak_milestones:
        defects.append(
            TriageDefect(
                label="fix",
                severity="high",
                summary="Milestones missing exit criteria",
                evidence=_candidate_pointer(candidate.candidate_id, candidate_ref, "/plan_package/milestones"),
                violated_gate="milestone_exit_criteria",
                patch=[
                    {
                        "op": "replace",
                        "path": "/plan_package/milestones/0/exit_criteria",
                        "value": ["Autofixed measurable criteria"],
                    }
                ],
            )
        )

    verdict: Literal["approve", "approve_with_fixes", "reject"] = "approve"
    if any(d.label == "blocker" for d in defects):
        verdict = "reject"
    elif defects:
        verdict = "approve_with_fixes"

    return TriageFindingsPayload(
        candidate_ref=candidate_ref,
        critic_id=critic_id,
        verdict=verdict,
        defects=defects,
    )


def merge_findings(a: TriageFindingsPayload, b: TriageFindingsPayload) -> TriageFindingsPayload:
    defects = [*a.defects, *b.defects]
    verdict: Literal["approve", "approve_with_fixes", "reject"]
    if any(d.label == "blocker" for d in defects):
        verdict = "reject"
    elif defects:
        verdict = "approve_with_fixes"
    else:
        verdict = "approve"

    return TriageFindingsPayload(
        candidate_ref=a.candidate_ref,
        critic_id="MERGED",
        verdict=verdict,
        defects=defects,
    )


def apply_repairs(plan: PlanPackage, findings: TriageFindingsPayload) -> TriageRepairResult:
    operations = []
    seen_ops: set[str] = set()
    for defect in findings.defects:
        for op in defect.patch:
            normalized = dict(op)
            if "path" in normalized:
                normalized["path"] = _normalize_patch_path(str(normalized["path"]))
            signature = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
            if signature in seen_ops:
                continue
            seen_ops.add(signature)
            operations.append(normalized)

    if not operations:
        return TriageRepairResult(updated_plan=plan, repaired=False)

    payload = plan.model_dump(by_alias=True)
    patched = jsonpatch.apply_patch(payload, operations, in_place=False)
    updated = PlanPackage.model_validate(patched)
    return TriageRepairResult(updated_plan=updated, repaired=True)


def triage_repair_loop(
    candidate_ref: str,
    candidate: PlanCandidatePayload,
    max_repairs: int,
) -> tuple[PlanCandidatePayload, TriageFindingsPayload]:
    current = candidate
    merged = merge_findings(
        critic_findings(candidate_ref, current, "A"),
        critic_findings(candidate_ref, current, "B"),
    )

    repairs = 0
    while any(d.label == "blocker" for d in merged.defects):
        if repairs >= max_repairs:
            return current, merged

        result = apply_repairs(current.plan_package, merged)
        if not result.repaired:
            return current, merged

        current.plan_package = result.updated_plan
        _ = validate_candidate(candidate_ref, current.plan_package)
        merged = merge_findings(
            critic_findings(candidate_ref, current, "A"),
            critic_findings(candidate_ref, current, "B"),
        )
        if not any(d.label == "blocker" for d in merged.defects):
            return current, merged
        repairs += 1

    return current, merged

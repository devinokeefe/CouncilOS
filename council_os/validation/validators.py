from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from council_os.agents.schemas import PlanPackage, ValidatorCheck, ValidatorReportPayload, Violation
from council_os.artifacts.ids import assert_referential_integrity
from council_os.orchestrator.policies import parse_tool_policy


def validate_candidate(candidate_ref: str, plan_package: PlanPackage) -> ValidatorReportPayload:
    checks: list[ValidatorCheck] = []
    structural_failures = 0

    schema_violations: list[Violation] = []
    try:
        PlanPackage.model_validate(plan_package.model_dump(by_alias=True))
    except ValidationError as exc:
        schema_violations.append(
            Violation(
                severity="critical",
                json_pointer="/plan_package",
                message=f"Schema validation failed: {exc}",
            )
        )
    checks.append(
        ValidatorCheck(
            name="schema",
            hard_gate=True,
            **{"pass": len(schema_violations) == 0},
            violations=schema_violations,
        )
    )

    integrity_violations = assert_referential_integrity(plan_package)
    checks.append(
        ValidatorCheck(
            name="id_integrity",
            hard_gate=True,
            **{"pass": len(integrity_violations) == 0},
            violations=integrity_violations,
        )
    )

    must_requirements = [r.id for r in plan_package.requirements if r.priority == "MUST"]
    mapped = {rid for at in plan_package.acceptance_tests for rid in at.maps_to_requirements}
    coverage_violations: list[Violation] = []
    for req_id in must_requirements:
        if req_id not in mapped:
            coverage_violations.append(
                Violation(
                    severity="critical",
                    json_pointer="/plan_package/acceptance_tests",
                    message=f"Missing acceptance mapping for MUST requirement {req_id}",
                )
            )
    checks.append(
        ValidatorCheck(
            name="coverage",
            hard_gate=True,
            **{"pass": len(coverage_violations) == 0},
            violations=coverage_violations,
        )
    )

    milestone_violations = [
        Violation(
            severity="high",
            json_pointer="/plan_package/milestones",
            message=f"Milestone {m.id} has empty exit criteria",
        )
        for m in plan_package.milestones
        if not m.exit_criteria
    ]
    if not plan_package.milestones:
        milestone_violations.append(
            Violation(
                severity="critical",
                json_pointer="/plan_package/milestones",
                message="No milestones defined",
            )
        )
        structural_failures += 1
    checks.append(
        ValidatorCheck(
            name="milestone_exit_criteria",
            hard_gate=True,
            **{"pass": len(milestone_violations) == 0},
            violations=milestone_violations,
        )
    )

    risk_violations: list[Violation] = []
    for risk in plan_package.risk_register:
        if risk.severity in {"high", "critical"}:
            has_mitigation = (
                risk.mitigation is not None
                and bool(risk.mitigation.text.strip())
                and bool(risk.mitigation.owner.strip())
                and risk.mitigation.status in {"planned", "in_progress", "done"}
            )
            has_acceptance = (
                risk.acceptance is not None
                and bool(risk.acceptance.rationale.strip())
                and bool(risk.acceptance.signoff.strip())
            )
            if not (has_mitigation or has_acceptance):
                risk_violations.append(
                    Violation(
                        severity="critical",
                        json_pointer="/plan_package/risk_register",
                        message=f"Risk {risk.id} missing closure",
                    )
                )
    checks.append(
        ValidatorCheck(
            name="risk_closure",
            hard_gate=True,
            **{"pass": len(risk_violations) == 0},
            violations=risk_violations,
        )
    )

    tool_policy_ok = True
    tool_policy_violations: list[Violation] = []
    try:
        parsed_tool_policy = parse_tool_policy(plan_package.governance.tool_policy.model_dump(by_alias=True))
        tool_policy_ok = len(parsed_tool_policy.stage_policies) > 0
    except Exception as exc:
        tool_policy_ok = False
        tool_policy_violations.append(
            Violation(
                severity="critical",
                json_pointer="/plan_package/governance/tool_policy",
                message=f"Tool policy parse error: {exc}",
            )
        )
    if tool_policy_ok is False and not tool_policy_violations:
        tool_policy_violations.append(
            Violation(
                severity="critical",
                json_pointer="/plan_package/governance/tool_policy",
                message="Tool policy missing stage_policies",
            )
        )

    checks.append(
        ValidatorCheck(
            name="tool_policy_parse",
            hard_gate=True,
            **{"pass": tool_policy_ok},
            violations=tool_policy_violations,
        )
    )

    soft_signal_violations: list[Violation] = []
    for milestone in plan_package.milestones:
        if any(len(item.strip()) < 8 for item in milestone.exit_criteria):
            soft_signal_violations.append(
                Violation(
                    severity="low",
                    json_pointer="/plan_package/milestones",
                    message=f"Milestone {milestone.id} exit criteria may be weakly measurable",
                )
            )
    checks.append(
        ValidatorCheck(
            name="measurability_heuristic",
            hard_gate=False,
            **{"pass": len(soft_signal_violations) == 0},
            violations=soft_signal_violations,
        )
    )
    contradiction_violations: list[Violation] = []
    non_goals_lower = [n.lower() for n in plan_package.project_capsule.non_goals]
    for req in plan_package.requirements:
        req_lower = req.text.lower()
        for non_goal in non_goals_lower:
            if non_goal and non_goal in req_lower:
                contradiction_violations.append(
                    Violation(
                        severity="low",
                        json_pointer="/plan_package/requirements",
                        message=f"Potential contradiction with non-goal: {req.id}",
                    )
                )
    allowlisted_tools = [t for sp in plan_package.governance.tool_policy.stage_policies for t in sp.allowlisted_tools]
    if "autonomous code execution" in " ".join(non_goals_lower) and allowlisted_tools:
        contradiction_violations.append(
            Violation(
                severity="low",
                json_pointer="/plan_package/governance/tool_policy",
                message="Non-goals mention no autonomous execution but tool allowlist is non-empty",
            )
        )
    checks.append(
        ValidatorCheck(
            name="contradiction_heuristic",
            hard_gate=False,
            **{"pass": len(contradiction_violations) == 0},
            violations=contradiction_violations,
        )
    )
    completeness_violations: list[Violation] = []
    if len(plan_package.requirements) < 2:
        completeness_violations.append(
            Violation(
                severity="low",
                json_pointer="/plan_package/requirements",
                message="Low requirement richness (<2 requirements)",
            )
        )
    if len(plan_package.acceptance_tests) < 1:
        completeness_violations.append(
            Violation(
                severity="low",
                json_pointer="/plan_package/acceptance_tests",
                message="No acceptance tests present",
            )
        )
    if len(plan_package.decision_log) < 1:
        completeness_violations.append(
            Violation(
                severity="low",
                json_pointer="/plan_package/decision_log",
                message="Decision log is empty",
            )
        )
    checks.append(
        ValidatorCheck(
            name="completeness_heuristic",
            hard_gate=False,
            **{"pass": len(completeness_violations) == 0},
            violations=completeness_violations,
        )
    )

    if not plan_package.requirements or not plan_package.architecture.options:
        structural_failures += 1

    passed = all(c.passed for c in checks)
    if passed:
        status: Literal["PASS", "FAIL_REPAIRABLE", "FAIL_STRUCTURAL"] = "PASS"
    elif structural_failures > 0:
        status = "FAIL_STRUCTURAL"
    else:
        status = "FAIL_REPAIRABLE"
    return ValidatorReportPayload(
        candidate_ref=candidate_ref,
        overall_status=status,
        checks=checks,
    )

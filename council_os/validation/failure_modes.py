from __future__ import annotations

from council_os.agents.schemas import (
    Acceptance,
    FailureModeClosure,
    FailureModeFindingItem,
    FailureModeFindingsPayload,
    FailureModeItem,
    PlanPackage,
)


def analyze_failure_modes(candidate_ref: str, plan_package: PlanPackage) -> FailureModeFindingsPayload:
    findings: list[FailureModeFindingItem] = []
    existing_nums = [
        int(fm.id[2:])
        for fm in plan_package.governance.failure_modes
        if fm.id.startswith("FM") and fm.id[2:].isdigit()
    ]
    next_num = max(existing_nums, default=0) + 1

    for fm in plan_package.governance.failure_modes:
        findings.append(
            FailureModeFindingItem(
                id=fm.id,
                category=fm.category,
                severity=fm.severity,
                finding=fm.finding,
                required_closure="mitigate",
                status="mitigated" if fm.closure.status == "mitigated" else "accepted",
                closure={"rationale": fm.closure.rationale, "signoff": fm.closure.signoff},
            )
        )

    used_ids = {f.id for f in findings}
    for risk in plan_package.risk_register:
        if risk.severity in {"high", "critical"}:
            has_mitigation = risk.mitigation is not None and bool(risk.mitigation.owner)
            has_acceptance = risk.acceptance is not None and bool(risk.acceptance.signoff)
            if not (has_mitigation or has_acceptance):
                finding_id = f"FM{next_num}"
                while finding_id in used_ids:
                    next_num += 1
                    finding_id = f"FM{next_num}"
                used_ids.add(finding_id)
                next_num += 1
                findings.append(
                    FailureModeFindingItem(
                        id=finding_id,
                        category="risk_closure",
                        severity="critical",
                        finding=f"High/critical risk {risk.id} is open",
                        required_closure="mitigate",
                        status="open",
                        closure={"rationale": "", "signoff": "", "linked_risk_id": risk.id},
                    )
                )

    closure_pass = all(f.status != "open" for f in findings)
    return FailureModeFindingsPayload(
        candidate_ref=candidate_ref,
        taxonomy="CUSTOM_V1",
        critical_findings=findings,
        closure_pass=closure_pass,
    )


def close_open_findings(plan_package: PlanPackage, findings: FailureModeFindingsPayload) -> PlanPackage:
    open_findings = [f for f in findings.critical_findings if f.status == "open"]
    if not open_findings:
        return plan_package

    existing_ids = {fm.id for fm in plan_package.governance.failure_modes}
    existing_nums = [
        int(fm_id[2:])
        for fm_id in existing_ids
        if fm_id.startswith("FM") and fm_id[2:].isdigit()
    ]
    next_num = max(existing_nums, default=0) + 1
    for finding in open_findings:
        if finding.id in existing_ids:
            continue
        new_fm_id = finding.id
        if not (new_fm_id.startswith("FM") and new_fm_id[2:].isdigit()):
            new_fm_id = f"FM{next_num}"
            next_num += 1
        plan_package.governance.failure_modes.append(
            FailureModeItem(
                id=new_fm_id,
                taxonomy="CUSTOM_V1",
                category=finding.category,
                finding=finding.finding,
                severity=finding.severity,
                closure=FailureModeClosure(
                    status="mitigated",
                    rationale="Auto-closed in deterministic failure-mode repair loop",
                    signoff="system",
                ),
            )
        )

        linked_risk_id = str(finding.closure.get("linked_risk_id", "")).strip()
        if linked_risk_id:
            for risk in plan_package.risk_register:
                if risk.id == linked_risk_id:
                    risk.acceptance = Acceptance(
                        rationale="Auto-accepted during failure-mode closure repair",
                        signoff="system",
                    )
                    break
    return PlanPackage.model_validate(plan_package.model_dump(by_alias=True))

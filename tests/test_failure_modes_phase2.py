from __future__ import annotations

import re
from uuid import uuid4

from council_os.agents.schemas import PlanCandidatePayload, ProjectCapsulePayload
from council_os.orchestrator.stages import plan_candidate
from council_os.validation.failure_modes import analyze_failure_modes, close_open_findings


def _candidate_with_open_risk() -> PlanCandidatePayload:
    capsule = ProjectCapsulePayload(
        brief="brief",
        problem_statement="problem",
        goals=["g"],
        non_goals=["n"],
        constraints=[{"id": "CNS1", "type": "tech", "text": "t"}],
        assumptions=[
            {
                "id": "A1",
                "text": "a",
                "impact": "low",
                "confidence": "high",
                "needs_confirmation": False,
            }
        ],
        open_questions=[{"id": "Q1", "text": "q", "impact": "low", "blocking": False}],
        success_metrics=["s"],
        stakeholders=[],
        glossary=[],
    )
    env = plan_candidate(
        uuid4(),
        capsule,
        "branch_B1_v1",
        "B1",
        "SA",
        ["requirements_v1"],
    )
    payload = env.payload
    payload["plan_package"]["risk_register"][0]["mitigation"] = None
    payload["plan_package"]["risk_register"][0]["acceptance"] = None
    return PlanCandidatePayload.model_validate(payload)


def test_failure_mode_closure_gate_and_repair() -> None:
    candidate = _candidate_with_open_risk()
    findings = analyze_failure_modes("candidate_B1-SA_v1", candidate.plan_package)
    assert findings.closure_pass is False
    assert all(re.match(r"^FM\d+$", finding.id) for finding in findings.critical_findings)

    repaired_plan = close_open_findings(candidate.plan_package, findings)
    findings_after = analyze_failure_modes("candidate_B1-SA_v1", repaired_plan)
    assert findings_after.closure_pass is True
    assert all(re.match(r"^FM\d+$", fm.id) for fm in repaired_plan.governance.failure_modes)

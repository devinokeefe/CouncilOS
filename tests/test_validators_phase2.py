from __future__ import annotations

from uuid import uuid4

from council_os.agents.schemas import PlanCandidatePayload, ProjectCapsulePayload
from council_os.orchestrator.stages import plan_candidate
from council_os.validation.validators import validate_candidate


def _candidate() -> PlanCandidatePayload:
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
    return PlanCandidatePayload.model_validate(env.payload)


def test_validator_reports_repairable_fail_for_coverage_gap() -> None:
    candidate = _candidate()
    candidate.plan_package.acceptance_tests = []
    report = validate_candidate("candidate_B1-SA_v1", candidate.plan_package)
    assert report.overall_status == "FAIL_REPAIRABLE"
    assert any(check.name == "coverage" and not check.passed for check in report.checks)


def test_validator_reports_structural_for_missing_sections() -> None:
    candidate = _candidate()
    candidate.plan_package.requirements = []
    candidate.plan_package.architecture.options = []
    report = validate_candidate("candidate_B1-SA_v1", candidate.plan_package)
    assert report.overall_status == "FAIL_STRUCTURAL"

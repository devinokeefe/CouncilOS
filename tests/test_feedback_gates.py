from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from council_os.agents.schemas import (
    AcceptanceTest,
    ArchitectureChosen,
    ArchitectureOption,
    ArchitectureSection,
    Component,
    Constraint,
    FailureModeClosure,
    FailureModeItem,
    GovernanceSection,
    HitlPolicy,
    Interface,
    Mitigation,
    OpenQuestion,
    PlanMeta,
    PlanPackage,
    ProjectCapsulePayload,
    Requirement,
    RiskItem,
    ToolPolicy,
    ToolPolicyStage,
    Tradeoffs,
)
from council_os.orchestrator.feedback.diff import diff_plan
from council_os.orchestrator.feedback.gates import HumanFeedbackGate, NeedsUserInput
from council_os.orchestrator.feedback.human_input import FileProvider
from council_os.orchestrator.feedback.plan_edits import PlanEditError, apply_plan_edits
from council_os.orchestrator.feedback.render import render_plan_review_markdown
from council_os.orchestrator.feedback.schemas import (
    ClarificationQuestions,
    ClarificationResponses,
    PlanEdits,
    UpdateRequirementOp,
    AddRequirementOp,
    RemoveRequirementOp,
    UpdateAcceptanceTestOp,
)
from council_os.orchestrator.feedback.store import PlanningArtifactStore
from council_os.handoff.hashing import plan_content_hash


def _make_plan_dict() -> dict[str, object]:
    plan = PlanPackage(
        meta=PlanMeta(
            plan_id=uuid4(),
            version="v1",
            created_at=datetime.now(UTC),
            source_run_id=uuid4(),
            schema_version="1.0.0",
            candidate_id="B1-SA",
            branch_id="B1",
            plan_status="CANDIDATE",
        ),
        project_capsule=ProjectCapsulePayload(
            brief="Brief",
            problem_statement="Problem",
            goals=["G1"],
            non_goals=["NG1"],
            constraints=[Constraint(id="C1", type="tech", text="X")],
            assumptions=[],
            open_questions=[OpenQuestion(id="Q1", text="Q", impact="low", blocking=False)],
            success_metrics=["M1"],
        ),
        requirements=[
            Requirement(id="R1", priority="MUST", text="Do thing", rationale="Rationale"),
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT1",
                maps_to_requirements=["R1"],
                type="system",
                procedure="Proc",
                pass_criteria="Pass",
            )
        ],
        architecture=ArchitectureSection(
            options=[
                ArchitectureOption(
                    id="O1",
                    summary="Option",
                    components=[Component(id="C1", name="Comp", responsibilities=["X"])],
                    interfaces=[Interface(id="IF1", **{"from": "C1"}, to="C1", contract="none")],
                    tradeoffs=Tradeoffs(pros=["p"], cons=["c"]),
                    risks=["K1"],
                )
            ],
            chosen=ArchitectureChosen(
                option_id="O1",
                rationale="Best",
                high_level_dataflow="flow",
                key_design_decisions=["DEC1"],
            ),
        ),
        milestones=[],
        risk_register=[
            RiskItem(
                id="K1",
                severity="high",
                description="Risk",
                mitigation=Mitigation(text="Mitigate", owner="owner", status="planned"),
                acceptance=None,
            )
        ],
        governance=GovernanceSection(
            failure_modes=[
                FailureModeItem(
                    id="FM1",
                    taxonomy="CUSTOM",
                    category="cat",
                    finding="finding",
                    severity="high",
                    closure=FailureModeClosure(status="mitigated", rationale="ok", signoff="owner"),
                )
            ],
            tool_policy=ToolPolicy(
                stage_policies=[
                    ToolPolicyStage(stage="IMPLEMENTATION", allowlisted_tools=[], restrictions=[], hitl_triggers=[])
                ]
            ),
            hitl_policy=HitlPolicy(when_to_interrupt=[], approval_roles=[]),
        ),
        decision_log=[],
    )
    return plan.model_dump(by_alias=True, mode="json")


def test_plan_hash_stable() -> None:
    plan = _make_plan_dict()
    assert plan_content_hash(plan) == plan_content_hash(plan)


def test_render_orders_by_id() -> None:
    plan = _make_plan_dict()
    plan["requirements"] = [
        {"id": "R2", "priority": "SHOULD", "text": "Second", "rationale": "R2"},
        {"id": "R1", "priority": "MUST", "text": "First", "rationale": "R1"},
    ]
    output = render_plan_review_markdown(plan)
    assert output.find("R1") < output.find("R2")


def test_apply_plan_edits_update_requirement() -> None:
    plan = _make_plan_dict()
    edits = PlanEdits(
        schema_version="1.0",
        round=1,
        plan_hash=plan_content_hash(plan),
        ops=[
            UpdateRequirementOp(op="update_requirement", id="R1", set={"text": "Updated text"}),
        ],
    )
    updated = apply_plan_edits(plan, edits)
    assert updated["requirements"][0]["text"] == "Updated text"


def test_apply_plan_edits_add_remove_requirement() -> None:
    plan = _make_plan_dict()
    edits = PlanEdits(
        schema_version="1.0",
        round=1,
        plan_hash=plan_content_hash(plan),
        ops=[
            AddRequirementOp(
                op="add_requirement",
                value={"id": "R2", "priority": "MUST", "text": "New", "rationale": "R2"},
                after_id="R1",
            ),
            UpdateAcceptanceTestOp(
                op="update_acceptance_test",
                id="AT1",
                set={"maps_to_requirements": ["R2"]},
            ),
            RemoveRequirementOp(op="remove_requirement", id="R1"),
        ],
    )
    updated = apply_plan_edits(plan, edits)
    req_ids = [req["id"] for req in updated["requirements"]]
    assert req_ids == ["R2"]
    assert updated["acceptance_tests"][0]["maps_to_requirements"] == ["R2"]


def test_apply_plan_edits_missing_id() -> None:
    plan = _make_plan_dict()
    edits = PlanEdits(
        schema_version="1.0",
        round=1,
        plan_hash=plan_content_hash(plan),
        ops=[UpdateRequirementOp(op="update_requirement", id="R9", set={"text": "X"})],
    )
    with pytest.raises(PlanEditError):
        apply_plan_edits(plan, edits)


def test_diff_summary_detects_modified() -> None:
    plan = _make_plan_dict()
    updated = deepcopy(plan)
    updated["requirements"][0]["text"] = "Changed"
    summary = diff_plan(plan, updated)
    assert any(
        item.anchor.kind == "requirement" and item.anchor.id == "R1" and item.change_type == "modified"
        for item in summary.changed
    )


class _NullProvider:
    def is_interactive(self) -> bool:
        return False

    def request_response(
        self,
        *,
        gate_type: str,
        request_artifact_path: str,
        expected_response_schema: str,
        round: int,
    ) -> dict | None:
        return None


def test_human_feedback_gate_pause_and_resume(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    store = PlanningArtifactStore(run_root)
    questions = ClarificationQuestions(
        schema_version="1.0",
        questions=[
            {
                "id": "CQ-1",
                "text": "Test?",
                "why_it_matters": "Test",
                "decision_impact": ["x"],
                "default_resolution": {"value": True, "explain": "default"},
                "answer_format": {"type": "boolean", "accepted": ["true", "false"]},
            }
        ],
    )
    store.write_json("clarification_questions.json", questions.model_dump())
    gate = HumanFeedbackGate(run_id="run1", run_root=run_root, provider=_NullProvider())  # type: ignore[arg-type]
    with pytest.raises(NeedsUserInput):
        gate.request_response(
            gate_type="clarify_intent",
            request_artifact="clarification_questions.json",
            response_artifact="clarification_responses.json",
            response_model=ClarificationResponses,
            round=0,
            instructions={"how_to_resume": "resume"},
        )
    assert store.exists("pending_action.json")

    response_file = tmp_path / "response.json"
    responses = ClarificationResponses(
        schema_version="1.0",
        responses=[{"question_id": "CQ-1", "response": ""}],
    )
    response_file.write_text(json.dumps(responses.model_dump()), encoding="utf-8")
    gate_resume = HumanFeedbackGate(run_id="run1", run_root=run_root, provider=FileProvider(response_file))
    gate_resume.request_response(
        gate_type="clarify_intent",
        request_artifact="clarification_questions.json",
        response_artifact="clarification_responses.json",
        response_model=ClarificationResponses,
        round=0,
        instructions={"how_to_resume": "resume"},
    )
    assert store.exists("clarification_responses.json")

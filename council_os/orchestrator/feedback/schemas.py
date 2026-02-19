from __future__ import annotations

from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictFeedbackModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnchorRef(StrictFeedbackModel):
    kind: Literal[
        "requirement",
        "acceptance_test",
        "assumption",
        "open_question",
        "architecture_option",
        "risk",
        "governance_policy",
        "general",
    ]
    id: str


class PlanningEvent(StrictFeedbackModel):
    ts: str
    run_id: str
    stage: str
    event_type: Literal[
        "HUMAN_FEEDBACK_REQUESTED",
        "WAIT_FOR_USER",
        "HUMAN_FEEDBACK_RECEIVED",
        "HUMAN_FEEDBACK_SKIPPED",
        "PLAN_APPROVED",
        "PLAN_REVIEW_ROUND_LIMIT_REACHED",
    ]
    gate_type: Literal["clarify_intent", "plan_review"] | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    message: str | None = None


class PendingAction(StrictFeedbackModel):
    schema_version: str = "1.0"
    run_id: str
    gate_type: Literal["clarify_intent", "plan_review"]
    request_artifact: str
    expected_response_artifact: str
    round: int = 0
    instructions: dict[str, str]


class PrePlanAvenue(StrictFeedbackModel):
    id: str
    summary: str


class PrePlanUncertainty(StrictFeedbackModel):
    id: str
    summary: str
    impact: Literal["low", "medium", "high"]


class PrePlanTriage(StrictFeedbackModel):
    schema_version: str = "1.0"
    avenues_considered: list[PrePlanAvenue] = Field(default_factory=list)
    uncertainties: list[PrePlanUncertainty] = Field(default_factory=list)


class ClarificationDefault(StrictFeedbackModel):
    value: Any
    explain: str


class ClarificationAnswerFormat(StrictFeedbackModel):
    type: Literal["boolean", "enum", "string"]
    accepted: list[str] = Field(default_factory=list)


class ClarificationQuestion(StrictFeedbackModel):
    id: str
    text: str
    why_it_matters: str
    decision_impact: list[str]
    default_resolution: ClarificationDefault
    answer_format: ClarificationAnswerFormat


class ClarificationQuestions(StrictFeedbackModel):
    schema_version: str = "1.0"
    questions: list[ClarificationQuestion] = Field(default_factory=list)


class ClarificationResponseItem(StrictFeedbackModel):
    question_id: str
    response: str


class ClarificationResponses(StrictFeedbackModel):
    schema_version: str = "1.0"
    responses: list[ClarificationResponseItem] = Field(default_factory=list)


class ClarificationResolution(StrictFeedbackModel):
    question_id: str
    resolved_value: Any
    source: Literal["default", "user"]
    raw_response: str


class ClarificationResolutions(StrictFeedbackModel):
    schema_version: str = "1.0"
    resolutions: list[ClarificationResolution] = Field(default_factory=list)


class PlanReviewAttention(StrictFeedbackModel):
    anchor: AnchorRef
    note: str


class PlanReviewInstructions(StrictFeedbackModel):
    approve_keyword: str = "APPROVE"
    how_to_give_feedback: list[str] = Field(default_factory=list)


class PlanReviewPacket(StrictFeedbackModel):
    schema_version: str = "1.0"
    draft_plan_ref: str
    plan_hash: str
    high_attention: list[PlanReviewAttention] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    instructions: PlanReviewInstructions


class OpenQuestionAnswer(StrictFeedbackModel):
    open_question_id: str
    answer: str


class PlanReviewStructuredAnswers(StrictFeedbackModel):
    answers: list[OpenQuestionAnswer] = Field(default_factory=list)


class PlanReviewFeedback(StrictFeedbackModel):
    schema_version: str = "1.0"
    plan_hash: str
    action: Literal["feedback", "approve"]
    feedback_text: str | None = None
    structured: PlanReviewStructuredAnswers | None = None
    note: str | None = None


class PlanFeedbackItem(StrictFeedbackModel):
    id: str
    anchor: AnchorRef
    intent: str
    text: str


class PlanFeedbackItems(StrictFeedbackModel):
    schema_version: str = "1.0"
    items: list[PlanFeedbackItem] = Field(default_factory=list)


class UpdateRequirementOp(StrictFeedbackModel):
    op: Literal["update_requirement"]
    id: str
    set: dict[str, Any]


class AddRequirementOp(StrictFeedbackModel):
    op: Literal["add_requirement"]
    value: dict[str, Any]
    after_id: str | None = None


class RemoveRequirementOp(StrictFeedbackModel):
    op: Literal["remove_requirement"]
    id: str


class UpdateAcceptanceTestOp(StrictFeedbackModel):
    op: Literal["update_acceptance_test"]
    id: str
    set: dict[str, Any]


class AddAcceptanceTestOp(StrictFeedbackModel):
    op: Literal["add_acceptance_test"]
    value: dict[str, Any]
    after_id: str | None = None


class RemoveAcceptanceTestOp(StrictFeedbackModel):
    op: Literal["remove_acceptance_test"]
    id: str


class SetAssumptionOp(StrictFeedbackModel):
    op: Literal["set_assumption"]
    id: str
    set: dict[str, Any]


class ResolveOpenQuestionOp(StrictFeedbackModel):
    op: Literal["resolve_open_question"]
    id: str
    answer: str
    rationale: str | None = None


class ChooseArchitectureOptionOp(StrictFeedbackModel):
    op: Literal["choose_architecture_option"]
    id: str
    status: Literal["selected", "considered"]
    rationale: str | None = None


class SetGovernancePolicyOp(StrictFeedbackModel):
    op: Literal["set_governance_policy"]
    path: str
    value: Any


PlanEditOperation = Annotated[
    (
        UpdateRequirementOp
        | AddRequirementOp
        | RemoveRequirementOp
        | UpdateAcceptanceTestOp
        | AddAcceptanceTestOp
        | RemoveAcceptanceTestOp
        | SetAssumptionOp
        | ResolveOpenQuestionOp
        | ChooseArchitectureOptionOp
        | SetGovernancePolicyOp
    ),
    Field(discriminator="op"),
]


class PlanEdits(StrictFeedbackModel):
    schema_version: str = "1.0"
    round: int
    plan_hash: str
    ops: list[PlanEditOperation] = Field(default_factory=list)


class PlanDiffChange(StrictFeedbackModel):
    anchor: AnchorRef
    change_type: Literal["modified", "added", "removed"]


class PlanDiffSummary(StrictFeedbackModel):
    schema_version: str = "1.0"
    from_plan_hash: str
    to_plan_hash: str
    changed: list[PlanDiffChange] = Field(default_factory=list)
    added_ids: list[str] = Field(default_factory=list)
    removed_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PlanApproval(StrictFeedbackModel):
    schema_version: str = "1.0"
    approved: bool
    approved_plan_ref: str
    approved_plan_hash: str
    approved_by: str
    note: str | None = None

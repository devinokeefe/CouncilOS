from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from council_os.orchestrator.feedback.config import FeedbackConfig
from council_os.orchestrator.feedback.diff import diff_plan
from council_os.orchestrator.feedback.gates import HumanFeedbackGate, NeedsUserInput
from council_os.orchestrator.feedback.llm import build_json_schema_messages
from council_os.orchestrator.feedback.plan_edits import PlanEditError, apply_plan_edits
from council_os.orchestrator.feedback.render import render_plan_review_markdown
from council_os.orchestrator.feedback.schemas import (
    AnchorRef,
    PendingAction,
    PlanApproval,
    PlanDiffSummary,
    PlanEdits,
    PlanFeedbackItems,
    PlanReviewAttention,
    PlanReviewFeedback,
    PlanReviewInstructions,
    PlanReviewPacket,
)
from council_os.orchestrator.feedback.store import PlanningArtifactStore
from council_os.handoff.hashing import plan_content_hash
from council_os.orchestrator.feedback.utils import write_planning_artifact
from council_os.orchestrator.feedback import event_log


Invoker = Callable[[list[dict[str, str]], type[BaseModel], str, dict[str, object]], BaseModel]


def _draft_versions(store: PlanningArtifactStore) -> list[int]:
    versions = []
    for path in store.root.glob("plan_package_draft_v*.json"):
        match = re.match(r"plan_package_draft_v(\\d+)\\.json", path.name)
        if match:
            versions.append(int(match.group(1)))
    return sorted(versions)


def _load_plan(store: PlanningArtifactStore, name: str) -> dict[str, Any]:
    return store.read_json(name)


def _write_plan(store: PlanningArtifactStore, name: str, plan: dict[str, Any]) -> Path:
    path = store.write_json(name, plan)
    try:
        write_planning_artifact(store.run_root, name, plan)
    except Exception:
        pass
    return path


def _build_review_packet(plan: dict[str, Any], plan_ref: str) -> PlanReviewPacket:
    capsule = plan.get("project_capsule", {}) if isinstance(plan, dict) else {}
    assumptions = capsule.get("assumptions", []) if isinstance(capsule, dict) else []
    open_questions = capsule.get("open_questions", []) if isinstance(capsule, dict) else []

    high_attention: list[PlanReviewAttention] = []
    for assumption in assumptions:
        if not isinstance(assumption, dict):
            continue
        if assumption.get("impact") in {"high", "medium"}:
            high_attention.append(
                PlanReviewAttention(
                    anchor=AnchorRef(kind="assumption", id=str(assumption.get("id", ""))),
                    note="High impact assumption",
                )
            )
    for question in open_questions:
        if not isinstance(question, dict):
            continue
        if question.get("blocking") or question.get("impact") in {"high", "medium"}:
            high_attention.append(
                PlanReviewAttention(
                    anchor=AnchorRef(kind="open_question", id=str(question.get("id", ""))),
                    note="Open question to resolve",
                )
            )

    instructions = PlanReviewInstructions(
        approve_keyword="APPROVE",
        how_to_give_feedback=[
            "Reference IDs like R-003, AT-004, A-002, Q-007",
            "For vNext plans, reference IDs like REQ-001, WI-001, CHK-001/EXP-001, MS-001",
            "Or describe changes in plain text; the planner will normalize",
        ],
    )
    return PlanReviewPacket(
        schema_version="1.0",
        draft_plan_ref=plan_ref,
        plan_hash=plan_content_hash(plan),
        high_attention=high_attention,
        open_questions=[str(q.get("id")) for q in open_questions if isinstance(q, dict) and q.get("id")],
        instructions=instructions,
    )


def _pause_for_user(
    gate: HumanFeedbackGate,
    *,
    gate_type: str,
    request_artifact: str,
    response_artifact: str,
    round: int,
    message: str,
) -> None:
    pending = PendingAction(
        run_id=gate.run_id,
        gate_type=gate_type,  # type: ignore[arg-type]
        request_artifact=request_artifact,
        expected_response_artifact=response_artifact,
        round=round,
        instructions={"how_to_resume": f"rerun with --resume-run {gate.run_id} --response-file <path>"},
    )
    gate.store.write_json("pending_action.json", pending.model_dump())
    event_log.append_event(
        gate.run_root,
        event_log.new_event(
            run_id=gate.run_id,
            stage="planning",
            event_type="WAIT_FOR_USER",
            gate_type=gate_type,
            artifact_refs=[request_artifact, "pending_action.json"],
            message=message,
        ),
    )
    raise NeedsUserInput(gate.run_id, gate.store.path("pending_action.json"))


def _normalize_feedback_items(
    *,
    invoke_json: Invoker | None,
    plan: dict[str, Any],
    feedback: PlanReviewFeedback,
    stage: str,
    llm_enabled: bool,
) -> PlanFeedbackItems:
    if not llm_enabled or invoke_json is None:
        return PlanFeedbackItems(schema_version="1.0", items=[])
    task = "Normalize feedback into anchored items tied to plan IDs."
    context = (
        f"Plan hash: {plan_content_hash(plan)}\n"
        f"Plan JSON:\n{plan}\n\n"
        f"Feedback:\n{feedback.model_dump()}"
    )
    messages, meta = build_json_schema_messages(PlanFeedbackItems, task=task, context=context)
    return PlanFeedbackItems.model_validate(
        invoke_json(messages, PlanFeedbackItems, stage, meta).model_dump()
    )


def _generate_plan_edits(
    *,
    invoke_json: Invoker | None,
    plan: dict[str, Any],
    feedback_items: PlanFeedbackItems,
    plan_hash_value: str,
    round: int,
    stage: str,
    llm_enabled: bool,
) -> PlanEdits:
    if not llm_enabled or invoke_json is None:
        return PlanEdits(schema_version="1.0", round=round, plan_hash=plan_hash_value, ops=[])
    task = (
        "Generate deterministic plan edits using only the supported operations in the PlanEdits schema. "
        "Use the provided feedback items."
    )
    context = (
        f"Plan hash: {plan_hash_value}\n"
        f"Plan JSON:\n{plan}\n\n"
        f"Feedback items:\n{feedback_items.model_dump()}"
    )
    messages, meta = build_json_schema_messages(PlanEdits, task=task, context=context)
    payload = invoke_json(messages, PlanEdits, stage, meta)
    return PlanEdits.model_validate(payload.model_dump())


def run_plan_review_loop(
    *,
    run_id: str,
    run_root: Path,
    plan: dict[str, Any],
    gate: HumanFeedbackGate,
    feedback_cfg: FeedbackConfig,
    invoke_json: Invoker | None,
    llm_enabled: bool,
) -> tuple[dict[str, Any], PlanApproval | None]:
    store = gate.store

    pending_round = None
    if store.exists("pending_action.json"):
        try:
            pending = PendingAction.model_validate_json(
                store.path("pending_action.json").read_text(encoding="utf-8")
            )
            if pending.gate_type == "plan_review":
                pending_round = pending.round
        except Exception:
            pending_round = None

    existing_versions = _draft_versions(store)
    if existing_versions:
        latest_version = existing_versions[-1]
        plan = _load_plan(store, f"plan_package_draft_v{latest_version}.json")
        round_idx = latest_version
    else:
        round_idx = 1
        _write_plan(store, f"plan_package_draft_v{round_idx}.json", plan)

    if pending_round and pending_round > round_idx:
        round_idx = pending_round

    if store.exists("plan_approval.json"):
        approval = PlanApproval.model_validate_json(store.path("plan_approval.json").read_text(encoding="utf-8"))
        if approval.approved and approval.approved_plan_hash == plan_content_hash(plan):
            return plan, approval

    while True:
        if round_idx > feedback_cfg.max_plan_review_rounds:
            event_log.append_event(
                run_root,
                event_log.new_event(
                    run_id=run_id,
                    stage="planning",
                    event_type="PLAN_REVIEW_ROUND_LIMIT_REACHED",
                    gate_type="plan_review",
                    message="Plan review round limit reached",
                ),
            )
            _pause_for_user(
                gate,
                gate_type="plan_review",
                request_artifact=f"plan_review_packet_v{round_idx}.json",
                response_artifact=f"plan_review_feedback_v{round_idx}.json",
                round=round_idx,
                message="Plan review round limit reached; increase max rounds or approve.",
            )

        plan_ref = f"plan_package_draft_v{round_idx}.json"
        packet = _build_review_packet(plan, plan_ref)
        store.write_json(f"plan_review_packet_v{round_idx}.json", packet.model_dump())
        render = render_plan_review_markdown(plan)
        store.write_text(f"plan_review_render_v{round_idx}.md", render)

        current_hash = plan_content_hash(plan)
        feedback_resp = gate.request_response(
            gate_type="plan_review",
            request_artifact=f"plan_review_packet_v{round_idx}.json",
            response_artifact=f"plan_review_feedback_v{round_idx}.json",
            response_model=PlanReviewFeedback,
            round=round_idx,
            instructions={"how_to_resume": f"rerun with --resume-run {run_id} --response-file <path>"},
            validator=lambda resp: (
                (False, "Plan hash mismatch; please respond to the latest review packet.")
                if resp.plan_hash != current_hash
                else (True, None)
            ),
        )
        feedback = PlanReviewFeedback.model_validate(feedback_resp.payload)

        if feedback.action == "approve" or (
            feedback.action == "feedback"
            and not feedback_cfg.require_explicit_approval
            and not (feedback.feedback_text or "").strip()
            and not (feedback.structured and feedback.structured.answers)
        ):
            approval = PlanApproval(
                schema_version="1.0",
                approved=True,
                approved_plan_ref=plan_ref,
                approved_plan_hash=current_hash,
                approved_by="user",
                note=feedback.note or "Approved in plan review",
            )
            store.write_json("plan_approval.json", approval.model_dump())
            write_planning_artifact(run_root, "plan_approval.json", approval.model_dump())
            event_log.append_event(
                run_root,
                event_log.new_event(
                    run_id=run_id,
                    stage="planning",
                    event_type="PLAN_APPROVED",
                    gate_type="plan_review",
                    artifact_refs=["plan_approval.json"],
                ),
            )
            return plan, approval

        feedback_items = _normalize_feedback_items(
            invoke_json=invoke_json,
            plan=plan,
            feedback=feedback,
            stage="plan_review",
            llm_enabled=llm_enabled,
        )
        store.write_json(f"plan_feedback_items_v{round_idx}.json", feedback_items.model_dump())
        write_planning_artifact(
            run_root,
            f"plan_feedback_items_v{round_idx}.json",
            feedback_items.model_dump(),
        )

        edits = _generate_plan_edits(
            invoke_json=invoke_json,
            plan=plan,
            feedback_items=feedback_items,
            plan_hash_value=current_hash,
            round=round_idx,
            stage="plan_review",
            llm_enabled=llm_enabled,
        )
        store.write_json(f"plan_edits_v{round_idx}.json", edits.model_dump())
        write_planning_artifact(
            run_root,
            f"plan_edits_v{round_idx}.json",
            edits.model_dump(),
        )

        try:
            plan_next = apply_plan_edits(plan, edits)
        except PlanEditError as exc:
            store.write_json(
                "plan_edit_apply_error.json",
                {"schema_version": "1.0", "error": str(exc), "round": round_idx},
            )
            raise

        diff_summary = diff_plan(plan, plan_next)
        store.write_json(f"plan_diff_summary_v{round_idx}.json", diff_summary.model_dump())
        write_planning_artifact(
            run_root,
            f"plan_diff_summary_v{round_idx}.json",
            diff_summary.model_dump(),
        )

        round_idx += 1
        plan = plan_next
        _write_plan(store, f"plan_package_draft_v{round_idx}.json", plan)

    # Unreachable

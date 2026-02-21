from __future__ import annotations

from typing import Any

from council_os.handoff.hashing import canonical_json_dumps, plan_content_hash
from council_os.orchestrator.feedback.schemas import AnchorRef, PlanDiffChange, PlanDiffSummary


def _index(plan: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for req in plan.get("requirements", []) or []:
        if isinstance(req, dict) and req.get("id"):
            index[("requirement", str(req["id"]))] = req
    for test in plan.get("acceptance_tests", []) or []:
        if isinstance(test, dict) and test.get("id"):
            index[("acceptance_test", str(test["id"]))] = test
    capsule = plan.get("project_capsule", {}) if isinstance(plan, dict) else {}
    for assumption in capsule.get("assumptions", []) or []:
        if isinstance(assumption, dict) and assumption.get("id"):
            index[("assumption", str(assumption["id"]))] = assumption
    for question in capsule.get("open_questions", []) or []:
        if isinstance(question, dict) and question.get("id"):
            index[("open_question", str(question["id"]))] = question
    architecture = plan.get("architecture", {}) if isinstance(plan, dict) else {}
    for option in architecture.get("options", []) or []:
        if isinstance(option, dict) and option.get("id"):
            index[("architecture_option", str(option["id"]))] = option
    for risk in plan.get("risk_register", []) or []:
        if isinstance(risk, dict) and risk.get("id"):
            index[("risk", str(risk["id"]))] = risk
    return index


def diff_plan(old_plan: dict[str, Any], new_plan: dict[str, Any]) -> PlanDiffSummary:
    old_index = _index(old_plan)
    new_index = _index(new_plan)
    old_keys = set(old_index.keys())
    new_keys = set(new_index.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    common = sorted(old_keys & new_keys)

    changed: list[PlanDiffChange] = []
    notes: list[str] = []

    for kind, item_id in added:
        changed.append(
            PlanDiffChange(anchor=AnchorRef(kind=kind, id=item_id), change_type="added")
        )
        notes.append(f"{kind} {item_id} added")
    for kind, item_id in removed:
        changed.append(
            PlanDiffChange(anchor=AnchorRef(kind=kind, id=item_id), change_type="removed")
        )
        notes.append(f"{kind} {item_id} removed")
    for kind, item_id in common:
        if canonical_json_dumps(old_index[(kind, item_id)]) != canonical_json_dumps(new_index[(kind, item_id)]):
            changed.append(
                PlanDiffChange(anchor=AnchorRef(kind=kind, id=item_id), change_type="modified")
            )
            notes.append(f"{kind} {item_id} modified")

    return PlanDiffSummary(
        schema_version="1.0",
        from_plan_hash=plan_content_hash(old_plan),
        to_plan_hash=plan_content_hash(new_plan),
        changed=changed,
        added_ids=[item_id for _, item_id in added],
        removed_ids=[item_id for _, item_id in removed],
        notes=notes,
    )

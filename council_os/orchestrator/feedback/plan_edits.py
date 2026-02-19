from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from council_os.agents.schemas import PlanPackage
from council_os.orchestrator.feedback.schemas import PlanEdits


@dataclass(frozen=True)
class Anchor:
    kind: str
    id: str
    path: list[int | str]


class PlanEditError(RuntimeError):
    pass


def _get_list(plan: dict[str, Any], kind: str) -> tuple[list[dict[str, Any]], list[str]]:
    if kind == "requirement":
        return plan.get("requirements", []), ["requirements"]
    if kind == "acceptance_test":
        return plan.get("acceptance_tests", []), ["acceptance_tests"]
    if kind == "assumption":
        capsule = plan.get("project_capsule", {})
        return capsule.get("assumptions", []), ["project_capsule", "assumptions"]
    if kind == "open_question":
        capsule = plan.get("project_capsule", {})
        return capsule.get("open_questions", []), ["project_capsule", "open_questions"]
    if kind == "architecture_option":
        architecture = plan.get("architecture", {})
        return architecture.get("options", []), ["architecture", "options"]
    if kind == "risk":
        return plan.get("risk_register", []), ["risk_register"]
    raise PlanEditError(f"Unknown plan list kind: {kind}")


def build_plan_index(plan: dict[str, Any]) -> dict[tuple[str, str], Anchor]:
    index: dict[tuple[str, str], Anchor] = {}
    for kind in ("requirement", "acceptance_test", "assumption", "open_question", "architecture_option", "risk"):
        items, base_path = _get_list(plan, kind)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not item_id:
                continue
            index[(kind, str(item_id))] = Anchor(kind=kind, id=str(item_id), path=base_path + [idx])
    return index


def _ensure_unique_ids(items: list[dict[str, Any]], kind: str) -> None:
    ids = [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]
    if len(ids) != len(set(ids)):
        raise PlanEditError(f"Duplicate IDs detected in {kind}")


def _ensure_reference_integrity(plan: dict[str, Any]) -> None:
    requirements = {str(item.get("id")) for item in plan.get("requirements", []) if isinstance(item, dict)}
    for test in plan.get("acceptance_tests", []) or []:
        if not isinstance(test, dict):
            continue
        mapped = test.get("maps_to_requirements", [])
        if not isinstance(mapped, list):
            continue
        for rid in mapped:
            if str(rid) not in requirements:
                raise PlanEditError(f"Acceptance test maps to missing requirement: {rid}")
    architecture = plan.get("architecture", {})
    if isinstance(architecture, dict):
        chosen = architecture.get("chosen", {})
        option_id = chosen.get("option_id") if isinstance(chosen, dict) else None
        option_ids = {str(opt.get("id")) for opt in architecture.get("options", []) if isinstance(opt, dict)}
        if option_id and str(option_id) not in option_ids:
            raise PlanEditError(f"Chosen architecture option not found: {option_id}")


def _apply_update(item: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        item[key] = value


def _insert_after(items: list[dict[str, Any]], value: dict[str, Any], after_id: str | None) -> None:
    if after_id:
        for idx, existing in enumerate(items):
            if isinstance(existing, dict) and str(existing.get("id")) == str(after_id):
                items.insert(idx + 1, value)
                return
    items.append(value)


def apply_plan_edits(plan: dict[str, Any], edits: PlanEdits) -> dict[str, Any]:
    working = copy.deepcopy(plan)
    index = build_plan_index(working)

    for op in edits.ops:
        if op.op == "update_requirement":
            anchor = index.get(("requirement", op.id))
            if anchor is None:
                raise PlanEditError(f"Requirement not found: {op.id}")
            reqs, _ = _get_list(working, "requirement")
            _apply_update(reqs[anchor.path[-1]], op.set)
        elif op.op == "add_requirement":
            reqs, _ = _get_list(working, "requirement")
            _insert_after(reqs, op.value, op.after_id)
        elif op.op == "remove_requirement":
            anchor = index.get(("requirement", op.id))
            if anchor is None:
                raise PlanEditError(f"Requirement not found: {op.id}")
            reqs, _ = _get_list(working, "requirement")
            reqs.pop(anchor.path[-1])
        elif op.op == "update_acceptance_test":
            anchor = index.get(("acceptance_test", op.id))
            if anchor is None:
                raise PlanEditError(f"Acceptance test not found: {op.id}")
            tests, _ = _get_list(working, "acceptance_test")
            _apply_update(tests[anchor.path[-1]], op.set)
        elif op.op == "add_acceptance_test":
            tests, _ = _get_list(working, "acceptance_test")
            _insert_after(tests, op.value, op.after_id)
        elif op.op == "remove_acceptance_test":
            anchor = index.get(("acceptance_test", op.id))
            if anchor is None:
                raise PlanEditError(f"Acceptance test not found: {op.id}")
            tests, _ = _get_list(working, "acceptance_test")
            tests.pop(anchor.path[-1])
        elif op.op == "set_assumption":
            anchor = index.get(("assumption", op.id))
            if anchor is None:
                raise PlanEditError(f"Assumption not found: {op.id}")
            assumptions, _ = _get_list(working, "assumption")
            _apply_update(assumptions[anchor.path[-1]], op.set)
        elif op.op == "resolve_open_question":
            anchor = index.get(("open_question", op.id))
            if anchor is None:
                raise PlanEditError(f"Open question not found: {op.id}")
            questions, _ = _get_list(working, "open_question")
            current = questions[anchor.path[-1]]
            text = str(current.get("text", ""))
            answer = op.answer.strip()
            if answer:
                text = text + f" (Resolved: {answer})"
            current["text"] = text
            current["blocking"] = False
        elif op.op == "choose_architecture_option":
            architecture = working.get("architecture", {})
            if not isinstance(architecture, dict):
                raise PlanEditError("Architecture section missing")
            option_ids = {str(opt.get("id")) for opt in architecture.get("options", []) if isinstance(opt, dict)}
            if str(op.id) not in option_ids:
                raise PlanEditError(f"Architecture option not found: {op.id}")
            if op.status == "selected":
                chosen = architecture.get("chosen", {})
                if not isinstance(chosen, dict):
                    chosen = {}
                    architecture["chosen"] = chosen
                chosen["option_id"] = op.id
                if op.rationale is not None:
                    chosen["rationale"] = op.rationale
        elif op.op == "set_governance_policy":
            governance = working.get("governance", {})
            if not isinstance(governance, dict):
                raise PlanEditError("Governance section missing")
            if op.path == "tool_policy":
                governance["tool_policy"] = op.value
            elif op.path == "hitl_policy":
                governance["hitl_policy"] = op.value
            elif op.path == "tool_policy.stage_policies":
                tool_policy = governance.get("tool_policy", {})
                if not isinstance(tool_policy, dict):
                    tool_policy = {}
                    governance["tool_policy"] = tool_policy
                tool_policy["stage_policies"] = op.value
            else:
                raise PlanEditError(f"Unsupported governance path: {op.path}")
        else:
            raise PlanEditError(f"Unsupported plan edit op: {op.op}")
        index = build_plan_index(working)

    # Structural validation
    _ensure_unique_ids(working.get("requirements", []), "requirements")
    _ensure_unique_ids(working.get("acceptance_tests", []), "acceptance_tests")
    capsule = working.get("project_capsule", {})
    if isinstance(capsule, dict):
        _ensure_unique_ids(capsule.get("assumptions", []), "assumptions")
        _ensure_unique_ids(capsule.get("open_questions", []), "open_questions")
    _ensure_unique_ids(working.get("risk_register", []), "risk_register")
    architecture = working.get("architecture", {})
    if isinstance(architecture, dict):
        _ensure_unique_ids(architecture.get("options", []), "architecture.options")
    _ensure_reference_integrity(working)

    # Schema validation
    PlanPackage.model_validate(working)
    return working

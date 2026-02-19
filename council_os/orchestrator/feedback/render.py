from __future__ import annotations

from typing import Any


def _sorted_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: str(item.get("id", "")))


def render_plan_review_markdown(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Plan Review Draft")

    requirements = _sorted_by_id(list(plan.get("requirements", []) if isinstance(plan, dict) else []))
    lines.append("\n## Requirements")
    for req in requirements:
        lines.append(f"- {req.get('id')}: {req.get('text')}")

    acceptance = _sorted_by_id(list(plan.get("acceptance_tests", []) if isinstance(plan, dict) else []))
    lines.append("\n## Acceptance Tests")
    for test in acceptance:
        lines.append(
            f"- {test.get('id')}: {test.get('procedure')} "
            f"(pass: {test.get('pass_criteria')})"
        )

    capsule = plan.get("project_capsule", {}) if isinstance(plan, dict) else {}
    assumptions = _sorted_by_id(list(capsule.get("assumptions", []) if isinstance(capsule, dict) else []))
    lines.append("\n## Assumptions")
    for assumption in assumptions:
        lines.append(
            f"- {assumption.get('id')}: {assumption.get('text')} "
            f"(impact: {assumption.get('impact')})"
        )

    open_questions = _sorted_by_id(list(capsule.get("open_questions", []) if isinstance(capsule, dict) else []))
    lines.append("\n## Open Questions")
    for question in open_questions:
        lines.append(f"- {question.get('id')}: {question.get('text')}")

    architecture = plan.get("architecture", {}) if isinstance(plan, dict) else {}
    options = _sorted_by_id(list(architecture.get("options", []) if isinstance(architecture, dict) else []))
    chosen = architecture.get("chosen", {}) if isinstance(architecture, dict) else {}
    chosen_id = chosen.get("option_id")
    lines.append("\n## Architecture Options")
    for option in options:
        opt_id = option.get("id")
        status = "selected" if opt_id == chosen_id else "considered"
        lines.append(f"- {opt_id} ({status}): {option.get('summary')}")

    governance = plan.get("governance", {}) if isinstance(plan, dict) else {}
    tool_policy = governance.get("tool_policy", {}) if isinstance(governance, dict) else {}
    stage_policies = tool_policy.get("stage_policies", []) if isinstance(tool_policy, dict) else []
    lines.append("\n## Governance Highlights")
    if stage_policies:
        lines.append("- Tool policy stages: " + ", ".join(str(p.get("stage", "")) for p in stage_policies))
    failure_modes = governance.get("failure_modes", []) if isinstance(governance, dict) else []
    if failure_modes:
        lines.append(f"- Failure modes tracked: {len(failure_modes)}")

    return "\n".join(lines).strip() + "\n"

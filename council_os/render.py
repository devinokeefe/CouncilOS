from __future__ import annotations

import json
from collections.abc import Iterable

from council_os.agents.schemas import (
    AcceptanceTest,
    FailureModeItem,
    PlanPackage,
    Requirement,
    RiskItem,
    ToolPolicyStage,
)


def render_plan_markdown(plan: PlanPackage) -> str:
    lines: list[str] = []
    title = "Frozen Plan" if plan.meta.plan_status == "FROZEN" else "Plan Candidate"
    lines.append(f"# {title}")
    lines.append("")

    _section(lines, "Meta")
    lines.append(f"- plan_id: `{plan.meta.plan_id}`")
    lines.append(f"- version: `{plan.meta.version}`")
    lines.append(f"- created_at: `{plan.meta.created_at.isoformat()}`")
    lines.append(f"- source_run_id: `{plan.meta.source_run_id}`")
    lines.append(f"- schema_version: `{plan.meta.schema_version}`")
    lines.append(f"- candidate_id: `{plan.meta.candidate_id}`")
    lines.append(f"- branch_id: `{plan.meta.branch_id}`")
    lines.append(f"- plan_status: `{plan.meta.plan_status}`")
    lines.append("")

    capsule = plan.project_capsule
    _section(lines, "Project Capsule")
    lines.append(f"- problem_statement: {capsule.problem_statement}")
    lines.append("- brief:")
    for line in _safe_lines(capsule.brief):
        lines.append(f"  {line}")
    _subsection(lines, "Goals")
    _bullets(lines, capsule.goals)
    _subsection(lines, "Non-Goals")
    _bullets(lines, capsule.non_goals)
    _subsection(lines, "Constraints")
    _bullets(lines, [_format_constraint(c) for c in capsule.constraints])
    _subsection(lines, "Assumptions")
    _bullets(lines, [_format_assumption(a) for a in capsule.assumptions])
    _subsection(lines, "Open Questions")
    _bullets(lines, [_format_open_question(q) for q in capsule.open_questions])
    _subsection(lines, "Success Metrics")
    _bullets(lines, capsule.success_metrics)
    if capsule.stakeholders:
        _subsection(lines, "Stakeholders")
        _bullets(lines, capsule.stakeholders)
    if capsule.glossary:
        _subsection(lines, "Glossary")
        _bullets(lines, [f"{item.term}: {item.definition}" for item in capsule.glossary])
    lines.append("")

    if plan.executive_summary:
        _section(lines, "Executive Summary")
        for line in _safe_lines(plan.executive_summary):
            lines.append(f"- {line}")
        lines.append("")

    _section(lines, "Requirements")
    _bullets(lines, [_format_requirement(r) for r in plan.requirements])
    lines.append("")

    _section(lines, "Acceptance Tests")
    _bullets(lines, [_format_acceptance_test(t) for t in plan.acceptance_tests])
    lines.append("")

    _section(lines, "Architecture Options")
    if not plan.architecture.options:
        lines.append("- (none)")
    for option in plan.architecture.options:
        _subsection(lines, f"Option {option.id}")
        lines.append(f"- summary: {option.summary}")
        lines.append("- components:")
        for comp in option.components:
            lines.append(f"  - {comp.id}: {comp.name} — {', '.join(comp.responsibilities)}")
        lines.append("- interfaces:")
        for iface in option.interfaces:
            lines.append(
                f"  - {iface.id}: {iface.from_component} -> {iface.to} — {iface.contract}"
            )
        lines.append("- tradeoffs:")
        lines.append(f"  - pros: {', '.join(option.tradeoffs.pros) if option.tradeoffs.pros else '(none)'}")
        lines.append(f"  - cons: {', '.join(option.tradeoffs.cons) if option.tradeoffs.cons else '(none)'}")
        lines.append(f"- risks: {', '.join(option.risks) if option.risks else '(none)'}")
    lines.append("")

    _section(lines, "Chosen Architecture")
    chosen = plan.architecture.chosen
    lines.append(f"- option_id: {chosen.option_id}")
    lines.append(f"- rationale: {chosen.rationale}")
    lines.append(f"- high_level_dataflow: {chosen.high_level_dataflow}")
    lines.append(f"- key_design_decisions: {', '.join(chosen.key_design_decisions)}")
    lines.append("")

    _section(lines, "Milestones")
    if not plan.milestones:
        lines.append("- (none)")
    for milestone in plan.milestones:
        _subsection(lines, f"{milestone.id}: {milestone.name}")
        lines.append("- deliverables:")
        for deliverable in milestone.deliverables:
            lines.append(f"  - {deliverable.id}: {deliverable.text}")
        lines.append("- exit_criteria:")
        for criteria in milestone.exit_criteria:
            lines.append(f"  - {criteria}")
        lines.append(
            f"- depends_on: {', '.join(milestone.depends_on) if milestone.depends_on else '(none)'}"
        )
    lines.append("")

    _section(lines, "Risk Register")
    _bullets(lines, [_format_risk(risk) for risk in plan.risk_register])
    lines.append("")

    _section(lines, "Governance")
    _subsection(lines, "Failure Modes")
    _bullets(lines, [_format_failure_mode(fm) for fm in plan.governance.failure_modes])
    _subsection(lines, "Tool Policy")
    _bullets(lines, [_format_tool_policy(stage) for stage in plan.governance.tool_policy.stage_policies])
    _subsection(lines, "HITL Policy")
    lines.append(f"- when_to_interrupt: {', '.join(plan.governance.hitl_policy.when_to_interrupt)}")
    lines.append(f"- approval_roles: {', '.join(plan.governance.hitl_policy.approval_roles)}")
    lines.append("")

    _section(lines, "Decision Log")
    if not plan.decision_log:
        lines.append("- (none)")
    for decision in plan.decision_log:
        _subsection(lines, decision.id)
        lines.append(f"- question: {decision.question}")
        lines.append(f"- choice: {decision.choice}")
        lines.append(f"- rationale: {decision.rationale}")
        alternatives = (
            ", ".join(decision.alternatives_considered) if decision.alternatives_considered else "(none)"
        )
        lines.append(f"- alternatives_considered: {alternatives}")
        changes = (
            ", ".join(decision.what_would_change_this_decision)
            if decision.what_would_change_this_decision
            else "(none)"
        )
        lines.append(f"- what_would_change_this_decision: {changes}")
        lines.append("- evidence_pointers:")
        for evidence in decision.evidence_pointers:
            note = f" ({evidence.note})" if evidence.note else ""
            entity_ref = evidence.entity_id or evidence.artifact_id or ""
            lines.append(f"  - {evidence.artifact_ref} {entity_ref} {evidence.json_pointer}{note}")
    if plan.supplemental_evaluations:
        lines.append("")
        _section(lines, "Supplemental Evaluations")
        for evaluation in plan.supplemental_evaluations:
            _subsection(lines, evaluation.title)
            for line in _safe_lines(evaluation.notes):
                lines.append(f"- {line}")
            if evaluation.evidence:
                lines.append("- evidence:")
                for evidence in evaluation.evidence:
                    note = f" ({evidence.note})" if evidence.note else ""
                    entity_ref = evidence.entity_id or evidence.artifact_id or ""
                    lines.append(
                        f"  - {evidence.artifact_ref} {entity_ref} {evidence.json_pointer}{note}"
                    )
    if plan.synthesis_warnings:
        lines.append("")
        _section(lines, "Synthesis Warnings")
        for warning in plan.synthesis_warnings:
            _subsection(lines, warning.id)
            lines.append(f"- severity: {warning.severity}")
            lines.append(f"- summary: {warning.summary}")
            lines.append(f"- routing_target: {warning.routing_target}")
            if warning.evidence:
                lines.append("- evidence:")
                for evidence in warning.evidence:
                    note = f" ({evidence.note})" if evidence.note else ""
                    entity_ref = evidence.entity_id or evidence.artifact_id or ""
                    lines.append(
                        f"  - {evidence.artifact_ref} {entity_ref} {evidence.json_pointer}{note}"
                    )
    if plan.appendix and plan.appendix.glossary:
        lines.append("")
        _section(lines, "Appendix")
        _subsection(lines, "Glossary")
        _bullets(lines, [f"{item.term}: {item.definition}" for item in plan.appendix.glossary])

    return "\n".join(lines) + "\n"


def _section(lines: list[str], title: str) -> None:
    lines.append(f"## {title}")


def _subsection(lines: list[str], title: str) -> None:
    lines.append(f"### {title}")


def _bullets(lines: list[str], items: Iterable[str]) -> None:
    items_list = list(items)
    if not items_list:
        lines.append("- (none)")
        return
    for item in items_list:
        lines.append(f"- {item}")


def _safe_lines(text: str) -> list[str]:
    stripped = text.strip("\n")
    if not stripped:
        return ["(empty)"]
    return stripped.splitlines()


def _format_constraint(constraint: object) -> str:
    return f"{constraint.id} ({constraint.type}): {constraint.text}"


def _format_assumption(assumption: object) -> str:
    return (
        f"{assumption.id} (impact: {assumption.impact}, confidence: {assumption.confidence}, "
        f"needs_confirmation: {assumption.needs_confirmation}): {assumption.text}"
    )


def _format_open_question(question: object) -> str:
    return (
        f"{question.id} (impact: {question.impact}, blocking: {question.blocking}): {question.text}"
    )


def _format_requirement(req: Requirement) -> str:
    return f"{req.id} [{req.priority}]: {req.text} — rationale: {req.rationale}"


def _format_acceptance_test(test: AcceptanceTest) -> str:
    refs = ", ".join(test.maps_to_requirements) if test.maps_to_requirements else "(none)"
    return (
        f"{test.id} [{test.type}] -> {refs}: procedure: {test.procedure}; pass_criteria: {test.pass_criteria}"
    )


def _format_risk(risk: RiskItem) -> str:
    mitigation = ""
    if risk.mitigation is not None:
        mitigation = (
            f" mitigation: {risk.mitigation.text} (owner: {risk.mitigation.owner}, "
            f"status: {risk.mitigation.status})"
        )
    acceptance = ""
    if risk.acceptance is not None:
        acceptance = f" acceptance: {risk.acceptance.rationale} (signoff: {risk.acceptance.signoff})"
    return f"{risk.id} [{risk.severity}]: {risk.description}{mitigation}{acceptance}"


def _format_failure_mode(failure_mode: FailureModeItem) -> str:
    closure = (
        f"{failure_mode.closure.status} — {failure_mode.closure.rationale} "
        f"(signoff: {failure_mode.closure.signoff})"
    )
    return (
        f"{failure_mode.id} [{failure_mode.severity}] ({failure_mode.taxonomy} / {failure_mode.category}): "
        f"{failure_mode.finding}; closure: {closure}"
    )


def _format_tool_policy(stage: ToolPolicyStage) -> str:
    restrictions = json.dumps(stage.restrictions, ensure_ascii=True)
    return (
        f"{stage.stage}: allowlisted_tools={stage.allowlisted_tools}; "
        f"restrictions={restrictions}; hitl_triggers={stage.hitl_triggers}"
    )

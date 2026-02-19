from __future__ import annotations

import re
from typing import Literal

from council_os.agents.schemas import PlanPackage, Violation

Namespace = Literal["R", "AT", "O", "C", "IF", "M", "DL", "K", "DEC", "A", "Q", "FM"]

_PATTERNS: dict[Namespace, re.Pattern[str]] = {
    "R": re.compile(r"^R\d+$"),
    "AT": re.compile(r"^AT\d+$"),
    "O": re.compile(r"^O\d+$"),
    "C": re.compile(r"^C\d+$"),
    "IF": re.compile(r"^IF\d+$"),
    "M": re.compile(r"^M\d+$"),
    "DL": re.compile(r"^DL\d+$"),
    "K": re.compile(r"^K\d+$"),
    "DEC": re.compile(r"^DEC\d+$"),
    "A": re.compile(r"^A\d+$"),
    "Q": re.compile(r"^Q\d+$"),
    "FM": re.compile(r"^FM\d+$"),
}


def validate_namespaced_id(id_str: str, namespace: Namespace) -> None:
    if not _PATTERNS[namespace].match(id_str):
        raise ValueError(f"Invalid id for namespace {namespace}: {id_str}")


def assert_referential_integrity(plan_package: PlanPackage) -> list[Violation]:
    violations: list[Violation] = []
    req_ids = {r.id for r in plan_package.requirements}
    at_ids = {at.id for at in plan_package.acceptance_tests}
    option_ids = {opt.id for opt in plan_package.architecture.options}
    component_ids = {comp.id for opt in plan_package.architecture.options for comp in opt.components}
    interface_ids = {iface.id for opt in plan_package.architecture.options for iface in opt.interfaces}
    risk_ids = {k.id for k in plan_package.risk_register}
    milestone_ids = {m.id for m in plan_package.milestones}
    deliverable_ids = {d.id for m in plan_package.milestones for d in m.deliverables}
    decision_ids = {d.id for d in plan_package.decision_log}
    assumption_ids = {a.id for a in plan_package.project_capsule.assumptions}
    question_ids = {q.id for q in plan_package.project_capsule.open_questions}
    failure_mode_ids = {fm.id for fm in plan_package.governance.failure_modes}

    for req in plan_package.requirements:
        if not _PATTERNS["R"].match(req.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/requirements",
                    message=f"Requirement id does not match R#: {req.id}",
                )
            )
    for at in plan_package.acceptance_tests:
        if not _PATTERNS["AT"].match(at.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/acceptance_tests",
                    message=f"Acceptance test id does not match AT#: {at.id}",
                )
            )
    for option in plan_package.architecture.options:
        if not _PATTERNS["O"].match(option.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/architecture/options",
                    message=f"Architecture option id does not match O#: {option.id}",
                )
            )
        option_component_ids = {comp.id for comp in option.components}
        if len(option_component_ids) != len(option.components):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/architecture/options",
                    message=f"Duplicate component IDs in option {option.id}",
                )
            )
        for component in option.components:
            if not _PATTERNS["C"].match(component.id):
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/architecture/options",
                        message=f"Component id does not match C#: {component.id}",
                    )
                )
        option_interface_ids = {iface.id for iface in option.interfaces}
        if len(option_interface_ids) != len(option.interfaces):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/architecture/options",
                    message=f"Duplicate interface IDs in option {option.id}",
                )
            )
        for interface in option.interfaces:
            if not _PATTERNS["IF"].match(interface.id):
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/architecture/options",
                        message=f"Interface id does not match IF#: {interface.id}",
                    )
                )
            if interface.from_component not in option_component_ids or interface.to not in option_component_ids:
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/architecture/options",
                        message=(
                            f"Interface {interface.id} references missing component "
                            f"({interface.from_component} -> {interface.to})"
                        ),
                    )
                )
    for milestone in plan_package.milestones:
        if not _PATTERNS["M"].match(milestone.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/milestones",
                    message=f"Milestone id does not match M#: {milestone.id}",
                )
            )
        if len({d.id for d in milestone.deliverables}) != len(milestone.deliverables):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/milestones",
                    message=f"Duplicate deliverable IDs in milestone {milestone.id}",
                )
            )
        for deliverable in milestone.deliverables:
            if not _PATTERNS["DL"].match(deliverable.id):
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/milestones",
                        message=f"Deliverable id does not match DL#: {deliverable.id}",
                    )
                )
    for risk in plan_package.risk_register:
        if not _PATTERNS["K"].match(risk.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/risk_register",
                    message=f"Risk id does not match K#: {risk.id}",
                )
            )
    for decision in plan_package.decision_log:
        if not _PATTERNS["DEC"].match(decision.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/decision_log",
                    message=f"Decision id does not match DEC#: {decision.id}",
                )
            )
    for assumption in plan_package.project_capsule.assumptions:
        if not _PATTERNS["A"].match(assumption.id):
            violations.append(
                Violation(
                    severity="medium",
                    json_pointer="/plan_package/project_capsule/assumptions",
                    message=f"Assumption id does not match A#: {assumption.id}",
                )
            )
    for question in plan_package.project_capsule.open_questions:
        if not _PATTERNS["Q"].match(question.id):
            violations.append(
                Violation(
                    severity="medium",
                    json_pointer="/plan_package/project_capsule/open_questions",
                    message=f"Open question id does not match Q#: {question.id}",
                )
            )
    for failure_mode in plan_package.governance.failure_modes:
        if not _PATTERNS["FM"].match(failure_mode.id):
            violations.append(
                Violation(
                    severity="high",
                    json_pointer="/plan_package/governance/failure_modes",
                    message=f"Failure mode id does not match FM#: {failure_mode.id}",
                )
            )

    if len(req_ids) != len(plan_package.requirements):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/requirements",
                message="Duplicate requirement IDs",
            )
        )
    if len(at_ids) != len(plan_package.acceptance_tests):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/acceptance_tests",
                message="Duplicate acceptance test IDs",
            )
        )
    if len(option_ids) != len(plan_package.architecture.options):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/architecture/options",
                message="Duplicate architecture option IDs",
            )
        )
    if len(component_ids) != sum(len(opt.components) for opt in plan_package.architecture.options):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/architecture/options",
                message="Duplicate component IDs across architecture options",
            )
        )
    if len(interface_ids) != sum(len(opt.interfaces) for opt in plan_package.architecture.options):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/architecture/options",
                message="Duplicate interface IDs across architecture options",
            )
        )
    if len(risk_ids) != len(plan_package.risk_register):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/risk_register",
                message="Duplicate risk IDs",
            )
        )
    if len(milestone_ids) != len(plan_package.milestones):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/milestones",
                message="Duplicate milestone IDs",
            )
        )
    if len(deliverable_ids) != sum(len(m.deliverables) for m in plan_package.milestones):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/milestones",
                message="Duplicate deliverable IDs across milestones",
            )
        )
    if len(decision_ids) != len(plan_package.decision_log):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/decision_log",
                message="Duplicate decision IDs",
            )
        )
    if len(assumption_ids) != len(plan_package.project_capsule.assumptions):
        violations.append(
            Violation(
                severity="medium",
                json_pointer="/plan_package/project_capsule/assumptions",
                message="Duplicate assumption IDs",
            )
        )
    if len(question_ids) != len(plan_package.project_capsule.open_questions):
        violations.append(
            Violation(
                severity="medium",
                json_pointer="/plan_package/project_capsule/open_questions",
                message="Duplicate open question IDs",
            )
        )
    if len(failure_mode_ids) != len(plan_package.governance.failure_modes):
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/governance/failure_modes",
                message="Duplicate failure mode IDs",
            )
        )

    for at in plan_package.acceptance_tests:
        for rid in at.maps_to_requirements:
            if rid not in req_ids:
                violations.append(
                    Violation(
                        severity="critical",
                        json_pointer="/plan_package/acceptance_tests",
                        message=f"Acceptance {at.id} references missing requirement {rid}",
                    )
                )

    for option in plan_package.architecture.options:
        for risk in option.risks:
            if risk not in risk_ids:
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/architecture/options",
                        message=f"Option {option.id} references missing risk {risk}",
                    )
                )

    if plan_package.architecture.chosen.option_id not in option_ids:
        violations.append(
            Violation(
                severity="high",
                json_pointer="/plan_package/architecture/chosen/option_id",
                message=f"Chosen architecture option missing: {plan_package.architecture.chosen.option_id}",
            )
        )

    for milestone in plan_package.milestones:
        for dep in milestone.depends_on:
            if dep not in milestone_ids:
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/milestones",
                        message=f"Milestone {milestone.id} depends on missing {dep}",
                    )
                )

    for dec in plan_package.architecture.chosen.key_design_decisions:
        if dec not in decision_ids:
            violations.append(
                Violation(
                    severity="medium",
                    json_pointer="/plan_package/architecture/chosen/key_design_decisions",
                    message=f"Decision reference missing: {dec}",
                )
            )

    all_ids = (
        req_ids
        | at_ids
        | option_ids
        | component_ids
        | interface_ids
        | milestone_ids
        | deliverable_ids
        | risk_ids
        | decision_ids
        | assumption_ids
        | question_ids
        | failure_mode_ids
    )
    for decision in plan_package.decision_log:
        for evidence in decision.evidence_pointers:
            entity_ref = evidence.entity_id or evidence.artifact_id
            if entity_ref and entity_ref not in all_ids:
                violations.append(
                    Violation(
                        severity="high",
                        json_pointer="/plan_package/decision_log",
                        message=f"Evidence pointer references missing id: {entity_ref}",
                    )
                )

    return violations

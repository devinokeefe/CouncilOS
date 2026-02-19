from __future__ import annotations

from council_os.agents.schemas import PlanPackage
from council_os.implementation.schemas import AssumptionEntry, AssumptionRegistryPayload


def compile_assumption_registry(plan: PlanPackage) -> AssumptionRegistryPayload:
    assumptions = []
    for assumption in plan.project_capsule.assumptions:
        status = "unvalidated" if assumption.needs_confirmation else "accepted_with_monitoring"
        assumptions.append(
            AssumptionEntry(
                assumption_id=assumption.id,
                statement=assumption.text,
                impact=assumption.impact,
                confidence=assumption.confidence,
                validation_task_ids=[],
                status=status,
                evidence_pointers=[],
            )
        )
    return AssumptionRegistryPayload(schema_version="2.1.0", assumptions=assumptions)


def unresolved_high_impact(registry: AssumptionRegistryPayload) -> list[AssumptionEntry]:
    return [
        entry
        for entry in registry.assumptions
        if entry.impact == "high" and entry.status in {"unvalidated", "rejected"}
    ]

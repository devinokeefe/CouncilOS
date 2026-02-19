from __future__ import annotations

from council_os.agents.schemas import PlanPackage
from council_os.implementation.schemas import (
    ExpectationRegistryPayload,
    ProgramGraphNode,
    ProgramGraphPayload,
    ProgramGraphRequiredEvidence,
)


class ProgramGraphCompiler:
    def compile(self, plan: PlanPackage, expectations: ExpectationRegistryPayload) -> ProgramGraphPayload:
        nodes: list[ProgramGraphNode] = []
        root_id = "PG-ROOT"
        nodes.append(ProgramGraphNode(id=root_id, type="root", parent_id=None))
        req_expectations = {exp.source.id: exp.expectation_id for exp in expectations.expectations}
        for milestone in plan.milestones:
            milestone_id = f"MILESTONE-{milestone.id}"
            nodes.append(ProgramGraphNode(id=milestone_id, type="milestone", parent_id=root_id))
            for deliverable in milestone.deliverables:
                deliverable_id = f"DELIV-{milestone.id}-{deliverable.id}"
                nodes.append(ProgramGraphNode(id=deliverable_id, type="deliverable", parent_id=milestone_id))
        for req in sorted(plan.requirements, key=lambda r: r.id):
            exp_id = req_expectations.get(req.id, f"EXP-PLAN-{req.id}")
            nodes.append(
                ProgramGraphNode(
                    id=f"CHECK-{req.id}",
                    type="check",
                    parent_id=root_id,
                    expectation_ids=[exp_id],
                    required_evidence=[ProgramGraphRequiredEvidence(artifact_type="work_plan", env_class="local")],
                    policy_gates=[],
                )
            )
        for test in sorted(plan.acceptance_tests, key=lambda t: t.id):
            exp_id = req_expectations.get(test.id, f"EXP-AT-{test.id}")
            nodes.append(
                ProgramGraphNode(
                    id=f"CHECK-AT-{test.id}",
                    type="check",
                    parent_id=root_id,
                    expectation_ids=[exp_id],
                    required_evidence=[ProgramGraphRequiredEvidence(artifact_type="test_results", env_class="local")],
                    policy_gates=[],
                )
            )
        return ProgramGraphPayload(schema_version="2.1.0", program_graph_id="program_graph_default", nodes=nodes)

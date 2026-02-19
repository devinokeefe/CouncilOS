from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from council_os.implementation.schemas import (
    AssumptionRegistryPayload,
    JobSpecPayload,
    ProgramGraphPayload,
    RepoContextPayloadV21,
    ToolRegistryPayload,
    VerificationPlanPayload,
    WorkGraphPayload,
    WorkGraphTask,
)
@dataclass
class WorkGraphBuildResult:
    work_graph: WorkGraphPayload
    job_specs: list[JobSpecPayload]


class WorkGraphBuilder:
    def _job_type_for_suite(self, suite_id: str, env_class: str) -> str:
        normalized = suite_id.lower()
        if "security" in normalized:
            return "security_scan"
        if "secrets" in normalized:
            return "secrets_scan"
        if "sbom" in normalized:
            return "sbom_generate"
        if normalized in {"remote_op", "remote_canary", "remote_scaled"}:
            return "remote_op"
        if env_class in {"canary", "scaled", "staging"}:
            return "remote_op"
        if normalized in {"e2e", "contract", "load"}:
            return "remote_op" if env_class != "local" else "validate_local"
        return "validate_local"

    def build(
        self,
        *,
        program_graph: ProgramGraphPayload,
        verification_plan: VerificationPlanPayload,
        tool_registry: ToolRegistryPayload | None,
        assumption_registry: AssumptionRegistryPayload | None,
        repo_context: RepoContextPayloadV21,
        profile_id: str,
    ) -> WorkGraphBuildResult:
        tasks: list[WorkGraphTask] = []
        job_specs: list[JobSpecPayload] = []
        tool_probe_task_ids: list[str] = []

        if tool_registry:
            for tool in tool_registry.tools:
                if tool.probe_status == "exempt":
                    continue
                if tool.probe_status != "probed" or tool.last_probe is None:
                    task_id = f"task_probe_{tool.tool_id}"
                    job_id = f"job_probe_{tool.tool_id}"
                    tasks.append(
                        WorkGraphTask(
                            task_id=task_id,
                            kind="tool_probe",
                            deps=[],
                            profile_id=profile_id,
                            program_check_ids=[],
                            expectation_ids=[],
                            assumption_ids=[],
                            surfaces=[],
                            job_specs=[job_id],
                            risk_class="low",
                            budget=None,
                            rollback_or_comp_plan_ref=None,
                            priority=1,
                        )
                    )
                    job_specs.append(
                        JobSpecPayload(
                            schema_version="2.1.0",
                            job_id=job_id,
                            job_type="tool_probe",
                            profile_id=profile_id,
                            inputs=[],
                            idempotency_key=f"probe:{tool.tool_id}",
                            budget=None,
                            side_effects="none",
                            compensation=None,
                            expected_expectation_ids=[],
                            surfaces=[],
                            assumption_ids=[],
                            tool_id=tool.tool_id,
                            change_intent_id=None,
                            mock_output=None,
                            mock_attempts=[],
                            priority=1,
                        )
                    )
                    tool_probe_task_ids.append(task_id)

        check_expectations = {node.id: node.expectation_ids for node in program_graph.nodes if node.type == "check"}
        for check in verification_plan.check_requirements:
            for suite in check.required_suites:
                task_id = f"task_{check.program_check_id}_{suite.suite_id}"
                job_id = f"job_{suite.suite_id}_{check.program_check_id}"
                job_type = self._job_type_for_suite(suite.suite_id, suite.env_class)
                tasks.append(
                    WorkGraphTask(
                        task_id=task_id,
                        kind=job_type,
                        deps=list(tool_probe_task_ids),
                        profile_id=profile_id,
                        program_check_ids=[check.program_check_id],
                        expectation_ids=check_expectations.get(check.program_check_id, []),
                        assumption_ids=[],
                        surfaces=[],
                        job_specs=[job_id],
                        risk_class="low",
                        budget=None,
                        rollback_or_comp_plan_ref=None,
                        priority=0,
                    )
                )
                job_specs.append(
                    JobSpecPayload(
                        schema_version="2.1.0",
                        job_id=job_id,
                        job_type=job_type,
                        profile_id=profile_id,
                        inputs=[],
                        idempotency_key=f"{job_type}:{check.program_check_id}:{suite.suite_id}",
                        budget=None,
                        side_effects="none",
                        compensation=None,
                        expected_expectation_ids=check_expectations.get(check.program_check_id, []),
                        surfaces=[],
                        assumption_ids=[],
                        tool_id=None,
                        change_intent_id=None,
                        mock_output={"status": "ok"},
                        mock_attempts=[],
                        priority=0,
                    )
                )

        work_graph = WorkGraphPayload(
            schema_version="2.1.0",
            work_graph_id="work_graph_default",
            tasks=tasks,
            generated_at=datetime.now(UTC),
        )
        return WorkGraphBuildResult(work_graph=work_graph, job_specs=job_specs)

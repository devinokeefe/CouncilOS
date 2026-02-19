from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from council_os.implementation.schemas import (
    DiagnosisBlockedTask,
    DiagnosisReportPayload,
    EvidenceIndexEntry,
    EvidencePointer,
    LeaseHolder,
    SurfaceRef,
    WorkGraphPayload,
    WorkGraphTask,
)
from council_os.implementation.v2_1.execution.job_runner import JobRunner
from council_os.implementation.v2_1.governance.leases import LeaseManager


@dataclass
class SchedulerResult:
    diagnosis_report_id: str | None = None


class WorkScheduler:
    def __init__(
        self,
        *,
        job_runner: JobRunner,
        lease_manager: LeaseManager,
        work_event_logger: Callable[[dict[str, Any]], None],
        lease_event_logger: Callable[[dict[str, Any]], None],
        update_evidence_index: Callable[[list[EvidenceIndexEntry]], None],
        write_artifact: Callable[[str, str, dict[str, Any], list[str], str, str], str],
    ) -> None:
        self._job_runner = job_runner
        self._lease_manager = lease_manager
        self._work_event_logger = work_event_logger
        self._lease_event_logger = lease_event_logger
        self._update_evidence_index = update_evidence_index
        self._write_artifact = write_artifact

    def _log_work_event(self, task_id: str, event: str, status: str | None = None, detail: str | None = None) -> None:
        self._work_event_logger(
            {
                "timestamp": datetime.now(UTC),
                "task_id": task_id,
                "event": event,
                "status": status,
                "detail": detail,
            }
        )

    def run(
        self,
        *,
        work_graph: WorkGraphPayload,
        job_specs: dict[str, Any],
        stage: str,
        evidence_parents: list[str],
    ) -> SchedulerResult:
        task_map: dict[str, WorkGraphTask] = {task.task_id: task for task in work_graph.tasks}
        completed: set[str] = set()
        started: set[str] = set()

        while len(completed) < len(task_map):
            runnable = [
                task
                for task in task_map.values()
                if task.task_id not in started and all(dep in completed for dep in task.deps)
            ]
            if not runnable:
                blocked = [
                    DiagnosisBlockedTask(task_id=task.task_id, reason="dependencies_unmet")
                    for task in task_map.values()
                    if task.task_id not in completed
                ]
                report = DiagnosisReportPayload(
                    schema_version="2.1.0",
                    status="stalled",
                    reason_codes=["no_runnable_tasks"],
                    blocked_tasks=blocked,
                    replan_tickets=[],
                    evidence_pointers=[],
                )
                report_id = self._write_artifact(
                    "diagnosis_report",
                    f"diagnosis_report_{uuid4().hex[:8]}",
                    report.model_dump(),
                    evidence_parents,
                    stage,
                    "scheduler",
                )
                return SchedulerResult(diagnosis_report_id=report_id)

            runnable.sort(key=lambda t: (-t.priority, t.task_id))
            for task in runnable:
                self._log_work_event(task.task_id, "started", status="running")
                started.add(task.task_id)
                held_leases: list[tuple[str, str]] = []
                lease_denied = False
                for surface_id in task.surfaces:
                    lease_id = f"lease_{task.task_id}_{surface_id}"
                    holder = LeaseHolder(run_id="", lane_id=None, task_id=task.task_id)
                    surface = SurfaceRef(type="surface", id=surface_id)
                    event, preempted = self._lease_manager.request_lease(
                        lease_id=lease_id,
                        surface=surface,
                        holder=holder,
                        ttl_seconds=3600,
                        priority=task.priority,
                    )
                    self._lease_event_logger(event.model_dump())
                    if preempted:
                        self._lease_event_logger(preempted.model_dump())
                    if event.status != "granted":
                        lease_denied = True
                        break
                    held_leases.append((surface_id, lease_id))

                if lease_denied:
                    self._log_work_event(task.task_id, "blocked", status="lease_denied")
                    continue

                for job_id in task.job_specs:
                    spec = job_specs.get(job_id)
                    if spec is None:
                        self._log_work_event(task.task_id, "failed", status="missing_job_spec")
                        break
                    try:
                        run_result = self._job_runner.run(spec, stage=stage, parents=evidence_parents)
                    except Exception as exc:
                        report = DiagnosisReportPayload(
                            schema_version="2.1.0",
                            status="stalled",
                            reason_codes=["job_blocked"],
                            blocked_tasks=[DiagnosisBlockedTask(task_id=task.task_id, reason=str(exc))],
                            replan_tickets=[],
                            evidence_pointers=[],
                        )
                        report_id = self._write_artifact(
                            "diagnosis_report",
                            f"diagnosis_report_{uuid4().hex[:8]}",
                            report.model_dump(),
                            evidence_parents,
                            stage,
                            "scheduler",
                        )
                        return SchedulerResult(diagnosis_report_id=report_id)
                    evaluator = run_result.job_result.evaluator
                    if evaluator and evaluator.verdict == "pass":
                        entry_status = "pass"
                    elif evaluator and evaluator.verdict == "fail":
                        entry_status = "fail"
                    else:
                        entry_status = "pass" if run_result.job_result.status in {"success", "cache_hit"} else "fail"
                    for exp_id in spec.expected_expectation_ids:
                        pointers = [EvidencePointer(artifact_ref=run_result.job_result_id, json_pointer="/")]
                        if evaluator and evaluator.diff_report_ref:
                            pointers.append(
                                EvidencePointer(artifact_ref=evaluator.diff_report_ref, json_pointer="/")
                            )
                        entry = EvidenceIndexEntry(
                            expectation_id=exp_id,
                            status=entry_status,
                            evidence_pointers=pointers,
                            waiver_approvals=[],
                        )
                        self._update_evidence_index([entry])

                for surface_id, lease_id in held_leases:
                    holder = LeaseHolder(run_id="", lane_id=None, task_id=task.task_id)
                    event = self._lease_manager.release_lease(surface_id, lease_id, holder)
                    if event:
                        self._lease_event_logger(event.model_dump())

                self._log_work_event(task.task_id, "completed", status="success")
                completed.add(task.task_id)

        return SchedulerResult(diagnosis_report_id=None)

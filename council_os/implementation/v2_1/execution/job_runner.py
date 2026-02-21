from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
import time

from council_os.implementation.schemas import (
    ApplyRecordPayload,
    ChangeIntentPayload,
    JobAttempt,
    JobEvaluator,
    JobOutputRef,
    JobResultPayload,
    JobSpecPayload,
    ExpectationRegistryPayload,
    JobSpecInputRef,
    ToolProbeResultsPayload,
)
from council_os.utils import deterministic_json_hash
from council_os.implementation.v2_1.execution.cache import IdempotencyCache
from council_os.implementation.v2_1.execution.evaluators import build_diff_report, evaluate_expectations
from council_os.implementation.v2_1.execution.tool_prober import probe_tool
from council_os.implementation.v2_1.governance.side_effects import require_change_intent
from council_os.implementation.v2_1.governance.world_state import capture_snapshot, diff_snapshots


@dataclass
class JobRunResult:
    job_result_id: str
    job_result: JobResultPayload
    output_refs: list[str]
    cache_hit: bool
    diff_report_id: str | None = None
    apply_record_id: str | None = None


class JobRunner:
    def __init__(
        self,
        *,
        cache: IdempotencyCache,
        write_artifact: Callable[[str, str, dict[str, Any], list[str], str, str], str],
        write_attestation: Callable[..., str],
        repo_root: str,
        change_intents: dict[str, ChangeIntentPayload],
        job_event_logger: Callable[[dict[str, Any]], None],
        expectation_registry: ExpectationRegistryPayload | None = None,
        artifact_reader: Callable[[str], dict[str, Any]] | None = None,
        run_id: str | None = None,
        schema_version: str = "2.1.0",
        max_retries: int = 0,
    ) -> None:
        self._cache = cache
        self._write_artifact = write_artifact
        self._write_attestation = write_attestation
        self._repo_root = repo_root
        self._change_intents = change_intents
        self._job_event_logger = job_event_logger
        self._max_retries = max_retries
        self._expectation_registry = expectation_registry
        self._artifact_reader = artifact_reader
        self._run_id = run_id or ""
        self._schema_version = schema_version

    def _log_event(self, job_id: str, event: str, status: str | None = None, detail: str | None = None) -> None:
        payload = {
            "timestamp": datetime.now(UTC),
            "job_id": job_id,
            "event": event,
            "status": status,
            "detail": detail,
        }
        self._job_event_logger(payload)

    def _resolve_input_refs(self, inputs: list[JobSpecInputRef]) -> list[str]:
        refs: list[str] = []
        for inp in inputs:
            if inp.artifact_ref:
                refs.append(inp.artifact_ref)
            if inp.snapshot_ref:
                refs.append(inp.snapshot_ref)
        return refs

    def _load_artifacts_for_expectations(self, expectation_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not self._expectation_registry or not self._artifact_reader:
            return {}
        exp_map = {exp.expectation_id: exp for exp in self._expectation_registry.expectations}
        refs: list[str] = []
        for exp_id in expectation_ids:
            exp = exp_map.get(exp_id)
            if exp is None:
                continue
            for inp in exp.inputs:
                refs.append(inp.dataset_snapshot_ref)
        artifacts: dict[str, dict[str, Any]] = {}
        for ref in refs:
            if ref in artifacts:
                continue
            try:
                artifacts[ref] = self._artifact_reader(ref)
            except Exception:
                continue
        return artifacts

    def submit(self, job_spec: JobSpecPayload) -> str:
        self._log_event(job_spec.job_id, "submitted", status="queued")
        return job_spec.job_id

    def cancel(self, job_id: str) -> None:
        self._log_event(job_id, "cancelled", status="cancelled")

    def materialize_logs(self, job_id: str) -> str | None:
        _ = job_id
        return None

    def run(self, job_spec: JobSpecPayload, *, stage: str, parents: list[str]) -> JobRunResult:
        cached = self._cache.get(job_spec.idempotency_key)
        if cached is not None:
            self._log_event(job_spec.job_id, "cache_hit", status="success", detail=cached.job_result_ref)
            if self._artifact_reader:
                try:
                    payload = self._artifact_reader(cached.job_result_ref)
                    job_result = JobResultPayload.model_validate(payload)
                    output_refs = [output.artifact_ref for output in job_result.outputs]
                    diff_ref = job_result.evaluator.diff_report_ref if job_result.evaluator else None
                    return JobRunResult(
                        job_result_id=cached.job_result_ref,
                        job_result=job_result,
                        output_refs=output_refs,
                        cache_hit=True,
                        diff_report_id=diff_ref,
                        apply_record_id=None,
                    )
                except Exception:
                    pass
            return JobRunResult(
                job_result_id=cached.job_result_ref,
                job_result=JobResultPayload(
                    schema_version="2.1.0",
                    job_id=job_spec.job_id,
                    status="cache_hit",
                    attempts=[],
                    outputs=[],
                    evaluator=None,
                    attestation_ref=None,
                    attestation=None,
                ),
                output_refs=[],
                cache_hit=True,
            )

        attempts: list[JobAttempt] = []
        output_refs: list[str] = []
        status = "failed"
        detail: str | None = None
        attempt = 0
        budget = job_spec.budget
        max_attempts = self._max_retries + 1
        if budget and budget.max_calls is not None:
            max_attempts = min(max_attempts, int(budget.max_calls))
        start_time = time.monotonic()
        attestation_ref: str | None = None
        side_effect_apply_id: str | None = None
        pre_snapshot_ref: str | None = None
        post_snapshot_ref: str | None = None
        drift_report_ref: str | None = None
        input_refs = self._resolve_input_refs(job_spec.inputs)

        if max_attempts <= 0:
            attempts.append(JobAttempt(attempt_id=f"attempt-{job_spec.job_id}-1", status="failed", error_class="budget_exceeded"))
            self._log_event(job_spec.job_id, "completed", status="failed", detail="budget_exceeded")
            job_result = JobResultPayload(
                schema_version=self._schema_version,
                job_id=job_spec.job_id,
                status="failed",
                attempts=attempts,
                outputs=[],
                evaluator=None,
                attestation_ref=None,
                attestation=None,
            )
            job_result_id = self._write_artifact(
                "job_result",
                f"job_result_{job_spec.job_id}",
                job_result.model_dump(),
                parents,
                stage,
                "job_runner",
            )
            self._cache.put(job_spec.idempotency_key, job_result_id, {"status": "failed"})
            return JobRunResult(
                job_result_id=job_result_id,
                job_result=job_result,
                output_refs=[],
                cache_hit=False,
                diff_report_id=None,
                apply_record_id=None,
            )

        while attempt < max_attempts:
            attempt += 1
            attempt_id = f"attempt-{job_spec.job_id}-{attempt}"
            attempt_start = time.monotonic()
            self._log_event(job_spec.job_id, "attempt_started", status="running", detail=attempt_id)
            mock_status = None
            if job_spec.mock_attempts:
                mock_status = job_spec.mock_attempts[min(attempt - 1, len(job_spec.mock_attempts) - 1)]
            if mock_status == "fail":
                latency_ms = (time.monotonic() - attempt_start) * 1000.0
                attempts.append(
                    JobAttempt(attempt_id=attempt_id, status="failed", error_class="mock_failure", latency_ms=latency_ms)
                )
                self._log_event(job_spec.job_id, "attempt_completed", status="failed", detail=attempt_id)
                if budget and budget.max_runtime_sec is not None and (time.monotonic() - start_time) > budget.max_runtime_sec:
                    detail = "budget_runtime_exceeded"
                    break
                continue

            if job_spec.side_effects != "none":
                intent = require_change_intent(job_spec, self._change_intents)
                pre_snapshot = capture_snapshot(Path(self._repo_root), f"snapshot_pre_{job_spec.job_id}", "local", [])
                pre_snapshot_ref = self._write_artifact(
                    "world_state_snapshot",
                    f"world_state_snapshot_pre_{job_spec.job_id}",
                    pre_snapshot.model_dump(),
                    parents,
                    stage,
                    "job_runner",
                )

            if job_spec.job_type == "tool_probe":
                attestation_ref = self._write_attestation(
                    stage=stage,
                    profile_id=job_spec.profile_id,
                    tool_id=job_spec.tool_id,
                    inputs=input_refs,
                )
                probe = probe_tool(job_spec.tool_id or "tool", "1.0.0", attestation_ref)
                probe_payload = ToolProbeResultsPayload(schema_version="2.1.0", probes=[probe])
                probe_id = self._write_artifact(
                    "tool_probe_results",
                    f"tool_probe_results_{job_spec.job_id}",
                    probe_payload.model_dump(),
                    parents,
                    stage,
                    "job_runner",
                )
                output_refs.append(probe_id)

            if job_spec.side_effects != "none":
                post_snapshot = capture_snapshot(Path(self._repo_root), f"snapshot_post_{job_spec.job_id}", "local", [])
                post_snapshot_ref = self._write_artifact(
                    "world_state_snapshot",
                    f"world_state_snapshot_post_{job_spec.job_id}",
                    post_snapshot.model_dump(),
                    parents,
                    stage,
                    "job_runner",
                )
                drift = diff_snapshots(pre_snapshot, post_snapshot, f"drift_{job_spec.job_id}")
                drift_report_ref = self._write_artifact(
                    "drift_report",
                    f"drift_report_{job_spec.job_id}",
                    drift.model_dump(),
                    parents,
                    stage,
                    "job_runner",
                )
                apply_record = ApplyRecordPayload(
                    schema_version="2.1.0",
                    intent_id=intent.intent_id,
                    applied_at=datetime.now(UTC),
                    status="applied",
                    pre_snapshot_ref=pre_snapshot_ref,
                    post_snapshot_ref=post_snapshot_ref,
                    drift_report_ref=drift_report_ref,
                    rollback_status=None,
                    evidence=[],
                )
                side_effect_apply_id = self._write_artifact(
                    "apply_record",
                    f"apply_record_{job_spec.job_id}",
                    apply_record.model_dump(),
                    parents,
                    stage,
                    "job_runner",
                )

            latency_ms = (time.monotonic() - attempt_start) * 1000.0
            attempts.append(JobAttempt(attempt_id=attempt_id, status="success", latency_ms=latency_ms))
            status = "success"
            self._log_event(job_spec.job_id, "attempt_completed", status="success", detail=attempt_id)
            if budget and budget.max_runtime_sec is not None and (time.monotonic() - start_time) > budget.max_runtime_sec:
                status = "failed"
                detail = "budget_runtime_exceeded"
            break

        self._log_event(job_spec.job_id, "completed", status=status, detail=detail)

        if status == "success" and attestation_ref is None:
            attestation_ref = self._write_attestation(
                stage=stage,
                profile_id=job_spec.profile_id,
                tool_id=job_spec.tool_id,
                inputs=input_refs,
            )

        evaluator: JobEvaluator | None = None
        diff_report_id: str | None = None
        if self._expectation_registry and job_spec.expected_expectation_ids:
            artifacts = self._load_artifacts_for_expectations(job_spec.expected_expectation_ids)
            output = job_spec.mock_output if job_spec.mock_output is not None else {"outputs": output_refs}
            ok, diffs, failing = evaluate_expectations(
                self._expectation_registry,
                job_spec.expected_expectation_ids,
                output,
                artifacts,
            )
            if ok:
                evaluator = JobEvaluator(verdict="pass", diff_report_ref=None)
            else:
                summary = f"Expectation {failing or 'unknown'} failed"
                report = build_diff_report(failing or "unknown", self._run_id, summary, diffs)
                diff_report_id = self._write_artifact(
                    "diff_report",
                    f"diff_report_{job_spec.job_id}",
                    report.model_dump(),
                    parents,
                    stage,
                    "job_runner",
                )
                evaluator = JobEvaluator(verdict="fail", diff_report_ref=diff_report_id)
                status = "failed"

        outputs: list[JobOutputRef] = []
        for ref in output_refs:
            digest = None
            if self._artifact_reader:
                try:
                    payload = self._artifact_reader(ref)
                    digest = deterministic_json_hash(payload)
                except Exception:
                    digest = None
            outputs.append(JobOutputRef(artifact_ref=ref, hash=digest))

        job_result = JobResultPayload(
            schema_version=self._schema_version,
            job_id=job_spec.job_id,
            status=status,
            attempts=attempts,
            outputs=outputs,
            evaluator=evaluator,
            attestation_ref=attestation_ref,
            attestation=None,
        )
        job_result_id = self._write_artifact(
            "job_result",
            f"job_result_{job_spec.job_id}",
            job_result.model_dump(),
            parents,
            stage,
            "job_runner",
        )
        self._cache.put(job_spec.idempotency_key, job_result_id, {"status": status})
        return JobRunResult(
            job_result_id=job_result_id,
            job_result=job_result,
            output_refs=output_refs,
            cache_hit=False,
            diff_report_id=diff_report_id,
            apply_record_id=side_effect_apply_id,
        )

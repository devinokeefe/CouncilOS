from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from council_os.agents.schemas import (
    SCHEMA_VERSION,
    ArtifactEnvelope,
    FailureModeFindingsPayload,
    PlanCandidatePayload,
    ProjectCapsulePayload,
    TriageFindingsPayload,
    ValidatorReportPayload,
)
from council_os.artifacts.store import ArtifactStore
from council_os.orchestrator.checkpoints import CheckpointState, latest_checkpoint, write_checkpoint
from council_os.orchestrator.hq_pipeline import CandidateState, HQPipeline
from council_os.orchestrator.stages import plan_candidate, project_capsule


class DummyHQPipeline(HQPipeline):
    def __init__(self, *args, fail_arch_merge: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_arch_merge = fail_arch_merge
        self.calls: Counter[str] = Counter()

    def _schema_dir(self) -> Path:
        return self.run_root / "schemas"

    def _validate_credentials(self) -> None:
        return None

    def _record(self, stage_id: str) -> None:
        self.calls[stage_id] += 1

    def _env(
        self,
        stage_id: str,
        artifact_type: str,
        artifact_id: str,
        payload: dict[str, object] | None = None,
    ) -> ArtifactEnvelope:
        env = ArtifactEnvelope(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            source_run_id=self.run_id,
            parents=[],
            payload=payload or {},
        )
        self._write_artifact(self._store(), env, stage=stage_id)
        return env

    def _stage_capsule(self, brief: str, artifact_refs: dict[str, str]) -> ArtifactEnvelope:
        self._record("intake.capsule_normalize")
        return self._env("intake.capsule_normalize", "project_capsule", "capsule_v1")

    def _stage_capsule_drafts(
        self, brief: str, artifact_refs: dict[str, str]
    ) -> dict[str, ArtifactEnvelope]:
        self._record("intake.capsule_normalize")
        env_a = self._env("intake.capsule_normalize", "project_capsule", "capsule_draft_a_v1")
        env_b = self._env("intake.capsule_normalize", "project_capsule", "capsule_draft_b_v1")
        return {"a": env_a, "b": env_b}

    def _stage_capsule_reconcile(
        self,
        brief: str,
        draft_a: dict[str, object],
        draft_b: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("intake.capsule_reconcile")
        return self._env("intake.capsule_reconcile", "project_capsule", "capsule_canonical_v1")

    def _stage_clarification(self, capsule: dict[str, object], artifact_refs: dict[str, str]) -> ArtifactEnvelope:
        self._record("clarify.clarification_gate")
        return self._env("clarify.clarification_gate", "clarification_plan", "clarification_plan_v1")

    def _stage_pods(self, capsule: dict[str, object], artifact_refs: dict[str, str]) -> dict[str, str]:
        self._record("pods")
        outputs: dict[str, str] = {}
        for stage_id, artifact_type, prefix in [
            ("pods.requirements_draft", "requirements_draft", "requirements_draft"),
            ("pods.architecture_options_draft", "architecture_options_draft", "architecture_draft"),
            ("pods.qa_templates_draft", "qa_templates_draft", "qa_templates_draft"),
            ("pods.risk_gov_draft", "risk_gov_draft", "risk_gov_draft"),
        ]:
            stage = self.stage_map[stage_id]
            count = sum(1 for call in stage.calls if call.enabled)
            for idx in range(count):
                artifact_id = f"{prefix}_{idx + 1}_v1"
                self._env(stage_id, artifact_type, artifact_id)
                outputs[f"{prefix}_{idx + 1}"] = artifact_id
        return outputs

    def _stage_requirements_merge(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("merge.requirements_merge")
        return self._env("merge.requirements_merge", "requirements_merge", "requirements_merge_v1")

    def _stage_requirements_canonical(
        self,
        merge_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("canonicalize.requirements")
        return self._env("canonicalize.requirements", "requirements_canonical", "requirements_canonical_v1")

    def _stage_arch_merge(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("merge.architecture_merge")
        if self.fail_arch_merge:
            raise RuntimeError("boom")
        return self._env("merge.architecture_merge", "architecture_merge", "architecture_merge_v1")

    def _stage_arch_canonical(
        self,
        merge_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("canonicalize.architecture")
        return self._env("canonicalize.architecture", "architecture_canonical", "architecture_canonical_v1")

    def _stage_risk_merge(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        requirements: dict[str, object],
        architecture: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("merge.risk_gov_merge")
        return self._env("merge.risk_gov_merge", "risk_gov_merge", "risk_gov_merge_v1")

    def _stage_risk_canonical(
        self,
        merge_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("canonicalize.risk_gov")
        return self._env("canonicalize.risk_gov", "risk_gov_canonical", "risk_gov_canonical_v1")

    def _stage_qa_strategy(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("merge.qa_strategy")
        return self._env("merge.qa_strategy", "qa_strategy_canonical", "qa_strategy_canonical_v1")

    def _stage_acceptance_generate(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        architecture: dict[str, object],
        qa_strategy: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("acceptance.generate")
        return self._env("acceptance.generate", "acceptance_draft", "acceptance_draft_v1")

    def _stage_acceptance_canonical(
        self,
        acceptance_payload: dict[str, object],
        requirements_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("acceptance.canonicalize")
        return self._env("acceptance.canonicalize", "acceptance_canonical", "acceptance_canonical_v1")

    def _stage_alignment(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("audit.alignment")
        return self._env("audit.alignment", "alignment_warnings", "alignment_warnings_v1")

    def _stage_branch_build(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        alignment: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("branch.build")
        payload = {
            "branches": [
                {
                    "branch_id": "B1",
                    "selected_arch_option_id": "O1",
                    "milestone_strategy": {"style": "thin_slice", "notes": "n"},
                    "acceptance_strategy": {"notes": "n"},
                    "governance_posture": {"tooling_level": "none", "hitl_strictness": "low"},
                    "risk_posture": {"notes": "n"},
                    "rationale": "r",
                }
            ]
        }
        return self._env("branch.build", "branch_set", "branch_set_v1", payload)

    def _stage_candidate_flow(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        branch_set: dict[str, object],
        artifact_refs: dict[str, str],
        existing_candidates: dict[str, CandidateState] | None = None,
        existing_judge_context: dict[str, object] | None = None,
        expected_candidate_ids: list[str] | None = None,
        rerun_candidates: set[str] | None = None,
    ) -> tuple[dict[str, CandidateState], dict[str, object]]:
        self._record("synthesis.candidate_delta")
        candidate_id = "B1A"
        if existing_candidates and candidate_id in existing_candidates:
            candidate_id = "B1B"
        plan_ref = "plan_candidate_B1A_v1"
        if candidate_id == "B1B":
            plan_ref = "plan_candidate_B1B_v1"
        delta_ref = f"candidate_delta_{candidate_id}_v1"
        validator_ref = f"validator_{candidate_id}_v1"
        triage_ref = f"triage_{candidate_id}_v1"
        failure_ref = f"failure_modes_{candidate_id}_v1"
        self._env("synthesis.candidate_delta", "candidate_delta", delta_ref)
        self._env("assemble.plan_candidate", "plan_candidate", plan_ref)
        self._env(
            "validate.plan_candidate",
            "validator_report",
            validator_ref,
            {"candidate_ref": plan_ref, "overall_status": "PASS", "checks": []},
        )
        self._env(
            "triage.critics",
            "triage_report",
            triage_ref,
            {"candidate_ref": plan_ref, "critic_id": "MERGED", "verdict": "approve", "defects": []},
        )
        self._env(
            "analysis.failure_modes",
            "failure_mode_findings",
            failure_ref,
            {"candidate_ref": plan_ref, "taxonomy": "CUSTOM_V1", "critical_findings": [], "closure_pass": True},
        )
        state = CandidateState(
            candidate_id=candidate_id,
            branch_id="B1",
            synthesizer_id="S1",
            author_model="mock",
            author_family="mock",
            delta_ref=delta_ref,
            plan_ref=plan_ref,
            payload=None,  # type: ignore[arg-type]
            validator_ref=validator_ref,
            triage_ref=triage_ref,
            failure_mode_ref=failure_ref,
            validator_report=None,
            triage_report=None,
            failure_mode=None,
            validator_status="PASS",
            triage_blocked=False,
            failure_mode_pass=True,
        )
        summaries = [{"candidate_id": candidate_id}]
        if existing_candidates:
            summaries.extend({"candidate_id": cid} for cid in existing_candidates.keys())
        judge_context = existing_judge_context or {"summaries": summaries}
        merged = dict(existing_candidates or {})
        merged[candidate_id] = state
        return merged, judge_context

    def _stage_judge_tournament(
        self,
        candidates: dict[str, CandidateState],
        judge_context: dict[str, object],
        artifact_refs: dict[str, str],
        *,
        allow_fallback: bool = False,
    ) -> tuple[str, dict[str, object]]:
        self._record("judge.arbitrator")
        return "B1A", {"pairwise_refs": [], "pairwise_results": [], "arbitrator_ref": None}

    def _stage_freeze(
        self,
        winner_state: CandidateState,
        judge_artifacts: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("freeze.freeze")
        freeze_env = self._env("freeze.freeze", "freeze_record", "freeze_v1")
        frozen_id = f"frozen_{winner_state.candidate_id}_vFinal"
        self._env("freeze.freeze", "plan_frozen", frozen_id)
        artifact_refs["frozen_plan"] = frozen_id
        return freeze_env

    def _stage_decision_record(
        self,
        winner_state: CandidateState,
        candidates: dict[str, CandidateState],
        judge_artifacts: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        self._record("freeze.decision_record")
        return self._env("freeze.decision_record", "decision_record", "decision_record_v1")

    def _stage_renderer(self, winner_state: CandidateState, artifact_refs: dict[str, str]) -> ArtifactEnvelope | None:
        self._record("freeze.renderer")
        return self._env("freeze.renderer", "markdown_render", "markdown_render_v1", {"markdown": "ok"})


def _write_candidate_artifacts(store: ArtifactStore, run_id: UUID, candidate_id: str) -> dict[str, object]:
    capsule_env = project_capsule(run_id, "brief")
    capsule_payload = ProjectCapsulePayload.model_validate(capsule_env.payload)
    base_env = plan_candidate(
        run_id,
        capsule_payload,
        branch_ref="branch_set_v1",
        branch_id="B1",
        synthesizer_id="S1",
        draft_refs=[],
    )
    base_payload = PlanCandidatePayload.model_validate(base_env.payload)
    plan_payload = PlanCandidatePayload(
        candidate_id=candidate_id,
        branch_id="B1",
        synthesizer_id="S1",
        inputs=base_payload.inputs,
        plan_package=base_payload.plan_package,
    )
    plan_ref = f"plan_candidate_{candidate_id}_v1"
    delta_ref = f"candidate_delta_{candidate_id}_v1"
    validator_ref = f"validator_{candidate_id}_v1"
    triage_ref = f"triage_{candidate_id}_v1"
    failure_ref = f"failure_modes_{candidate_id}_v1"

    store.write_artifact(
        ArtifactEnvelope(
            artifact_type="candidate_delta",
            artifact_id=delta_ref,
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=[],
            payload={"candidate_id": candidate_id},
        )
    )
    store.write_artifact(
        ArtifactEnvelope(
            artifact_type="plan_candidate",
            artifact_id=plan_ref,
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=[],
            payload=plan_payload.model_dump(by_alias=True),
        )
    )
    validator_payload = ValidatorReportPayload(candidate_ref=plan_ref, overall_status="PASS", checks=[])
    store.write_artifact(
        ArtifactEnvelope(
            artifact_type="validator_report",
            artifact_id=validator_ref,
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=[],
            payload=validator_payload.model_dump(by_alias=True),
        )
    )
    triage_payload = TriageFindingsPayload(candidate_ref=plan_ref, critic_id="MERGED", verdict="approve", defects=[])
    store.write_artifact(
        ArtifactEnvelope(
            artifact_type="triage_report",
            artifact_id=triage_ref,
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=[],
            payload=triage_payload.model_dump(by_alias=True),
        )
    )
    failure_payload = FailureModeFindingsPayload(
        candidate_ref=plan_ref,
        taxonomy="CUSTOM_V1",
        critical_findings=[],
        closure_pass=True,
    )
    store.write_artifact(
        ArtifactEnvelope(
            artifact_type="failure_mode_findings",
            artifact_id=failure_ref,
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=[],
            payload=failure_payload.model_dump(by_alias=True),
        )
    )
    return {
        "candidate_id": candidate_id,
        "branch_id": "B1",
        "synthesizer_id": "S1",
        "author_model": "mock",
        "author_family": "mock",
        "delta_ref": delta_ref,
        "plan_ref": plan_ref,
        "validator_ref": validator_ref,
        "triage_ref": triage_ref,
        "failure_mode_ref": failure_ref,
    }


def test_hq_resume_from_checkpoint_skips_completed_stages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("council_os.artifacts.store.validate_artifact_payload", lambda _env: None)

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "config" / "council_os_hq.yml"
    config_raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(config_raw)

    brief_path = tmp_path / "brief.md"
    brief_path.write_text("brief", encoding="utf-8")

    run_id = uuid4()
    run_root = tmp_path / str(run_id)

    pipeline1 = DummyHQPipeline(
        storage_root=tmp_path,
        config_path=config_path,
        config_raw=config_raw,
        config=config,
        run_id=run_id,
        run_root=run_root,
        fail_arch_merge=True,
    )
    with pytest.raises(RuntimeError, match="boom"):
        pipeline1.run(brief_path)

    checkpoint = latest_checkpoint(run_root)
    assert checkpoint.stage_name == "canonicalize.requirements"

    pipeline2 = DummyHQPipeline(
        storage_root=tmp_path,
        config_path=config_path,
        config_raw=config_raw,
        config=config,
        run_id=run_id,
        run_root=run_root,
        fail_arch_merge=False,
    )
    result = pipeline2.resume()

    assert result.frozen_artifact_id == "frozen_B1A_vFinal"
    assert pipeline2.calls["intake.capsule_normalize"] == 0
    assert pipeline2.calls["clarify.clarification_gate"] == 0
    assert pipeline2.calls["pods"] == 0
    assert pipeline2.calls["merge.architecture_merge"] == 1
    assert (run_root / "artifacts" / "plan_frozen" / "frozen_B1A_vFinal.json").exists()


def test_hq_resume_mid_candidate_flow_continues_missing_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("council_os.artifacts.store.validate_artifact_payload", lambda _env: None)

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "config" / "council_os_hq.yml"
    config_raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(config_raw)
    config["workflow"]["enable_candidate_c_google"] = True

    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "config.snapshot.yml").write_text(config_raw, encoding="utf-8")
    (run_root / "brief.snapshot.md").write_text("brief", encoding="utf-8")

    store = ArtifactStore(tmp_path, run_id)
    candidate_meta = [
        _write_candidate_artifacts(store, run_id, "B1A"),
        _write_candidate_artifacts(store, run_id, "B1B"),
    ]

    pipeline = DummyHQPipeline(
        storage_root=tmp_path,
        config_path=config_path,
        config_raw=config_raw,
        config=config,
        run_id=run_id,
        run_root=run_root,
    )
    stage_idx = pipeline._stage_index_value("synthesis.candidate_delta")
    write_checkpoint(
        run_root,
        run_id,
            CheckpointState(
                stage_name="synthesis.candidate_delta",
                stage_index=stage_idx,
                event_cursor="ev1",
                artifact_refs={},
                routing_state={
                    "candidates": candidate_meta,
                    "expected_candidates": ["B1A", "B1B", "B1C"],
                    "candidates_complete": False,
                },
            ),
        )

    result = pipeline.resume()
    assert pipeline.calls["synthesis.candidate_delta"] == 1
    assert (run_root / "artifacts" / "plan_candidate" / "plan_candidate_B1B_v1.json").exists()
    assert result.frozen_artifact_id == "frozen_B1A_vFinal"


def test_hq_resume_after_judge_skips_judge_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("council_os.artifacts.store.validate_artifact_payload", lambda _env: None)

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "config" / "council_os_hq.yml"
    config_raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(config_raw)

    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "config.snapshot.yml").write_text(config_raw, encoding="utf-8")
    (run_root / "brief.snapshot.md").write_text("brief", encoding="utf-8")

    store = ArtifactStore(tmp_path, run_id)
    candidate_meta = [_write_candidate_artifacts(store, run_id, "B1A")]

    pipeline = DummyHQPipeline(
        storage_root=tmp_path,
        config_path=config_path,
        config_raw=config_raw,
        config=config,
        run_id=run_id,
        run_root=run_root,
    )
    stage_idx = pipeline._stage_index_value("judge.arbitrator")
    write_checkpoint(
        run_root,
        run_id,
        CheckpointState(
            stage_name="judge.arbitrator",
            stage_index=stage_idx,
            event_cursor="ev2",
            artifact_refs={},
            routing_state={
                "candidates": candidate_meta,
                "expected_candidates": ["B1A", "B1B"],
                "candidates_complete": True,
                "winner_id": "B1A",
                "judge_artifacts": {"pairwise_refs": [], "pairwise_results": [], "arbitrator_ref": None},
            },
        ),
    )

    result = pipeline.resume()
    assert pipeline.calls["judge.arbitrator"] == 0
    assert result.frozen_artifact_id == "frozen_B1A_vFinal"


def test_hq_judge_fallback_after_max_cycles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("council_os.artifacts.store.validate_artifact_payload", lambda _env: None)

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "config" / "council_os_hq.yml"
    config_raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(config_raw)
    config["runtime"]["orchestrator"]["max_full_cycles_without_pass"] = 2

    brief_path = tmp_path / "brief.md"
    brief_path.write_text("brief", encoding="utf-8")

    class DummyFailingHQPipeline(DummyHQPipeline):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.cycle_count = 0
            self.judge_fallback_flags: list[bool] = []

        def _stage_candidate_flow(
            self,
            capsule: dict[str, object],
            requirements: dict[str, object],
            acceptance: dict[str, object],
            architecture: dict[str, object],
            risk_gov: dict[str, object],
            branch_set: dict[str, object],
            artifact_refs: dict[str, str],
            existing_candidates: dict[str, CandidateState] | None = None,
            existing_judge_context: dict[str, object] | None = None,
            expected_candidate_ids: list[str] | None = None,
            rerun_candidates: set[str] | None = None,
        ) -> tuple[dict[str, CandidateState], dict[str, object]]:
            self._record("synthesis.candidate_delta")
            self.cycle_count += 1
            candidate_id = "B1A"
            version = self.cycle_count
            plan_ref = f"plan_candidate_{candidate_id}_v{version}"
            delta_ref = f"candidate_delta_{candidate_id}_v{version}"
            validator_ref = f"validator_{candidate_id}_v{version}"
            triage_ref = f"triage_{candidate_id}_v{version}"
            failure_ref = f"failure_modes_{candidate_id}_v{version}"

            self._env("synthesis.candidate_delta", "candidate_delta", delta_ref)
            self._env("assemble.plan_candidate", "plan_candidate", plan_ref)
            self._env(
                "validate.plan_candidate",
                "validator_report",
                validator_ref,
                {"candidate_ref": plan_ref, "overall_status": "FAIL_STRUCTURAL", "checks": []},
            )
            self._env(
                "triage.critics",
                "triage_report",
                triage_ref,
                {"candidate_ref": plan_ref, "critic_id": "MERGED", "verdict": "reject", "defects": []},
            )
            self._env(
                "analysis.failure_modes",
                "failure_mode_findings",
                failure_ref,
                {
                    "candidate_ref": plan_ref,
                    "taxonomy": "CUSTOM_V1",
                    "critical_findings": [],
                    "closure_pass": False,
                },
            )

            state = CandidateState(
                candidate_id=candidate_id,
                branch_id="B1",
                synthesizer_id="S1",
                author_model="mock",
                author_family="mock",
                delta_ref=delta_ref,
                plan_ref=plan_ref,
                payload=None,  # type: ignore[arg-type]
                validator_ref=validator_ref,
                triage_ref=triage_ref,
                failure_mode_ref=failure_ref,
                validator_report=None,
                triage_report=None,
                failure_mode=None,
                validator_status="FAIL_STRUCTURAL",
                triage_blocked=True,
                failure_mode_pass=False,
            )
            merged = dict(existing_candidates or {})
            merged[candidate_id] = state
            summaries = [{"candidate_id": candidate_id}]
            judge_context = existing_judge_context or {"summaries": summaries}
            return merged, judge_context

        def _stage_judge_tournament(
            self,
            candidates: dict[str, CandidateState],
            judge_context: dict[str, object],
            artifact_refs: dict[str, str],
            *,
            allow_fallback: bool = False,
        ) -> tuple[str, dict[str, object]]:
            self._record("judge.arbitrator")
            self.judge_fallback_flags.append(allow_fallback)
            return "B1A", {"pairwise_refs": [], "pairwise_results": [], "arbitrator_ref": None}

    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    pipeline = DummyFailingHQPipeline(
        storage_root=tmp_path,
        config_path=config_path,
        config_raw=config_raw,
        config=config,
        run_id=run_id,
        run_root=run_root,
    )

    result = pipeline.run(brief_path)
    assert pipeline.cycle_count == 2
    assert pipeline.judge_fallback_flags == [True]
    assert result.frozen_artifact_id == "frozen_B1A_vFinal"

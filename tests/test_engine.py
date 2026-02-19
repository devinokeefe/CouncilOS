from __future__ import annotations

from pathlib import Path

from council_os.orchestrator.engine import STAGES, Engine


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yml"
    config.write_text(
        """
roles:
  requirements_pod:
    model_provider: mock
    model_name: mock-v1
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: p1
    tools_allowed: []
  synthesizer_a:
    model_provider: mock_a
    model_name: mock-a-v1
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: sa
    tools_allowed: []
  synthesizer_b:
    model_provider: mock_b
    model_name: mock-b-v1
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: sb
    tools_allowed: []
  judge_1:
    model_provider: mock_j
    model_name: mock-j-v1
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: j1
    tools_allowed: []
branching_policy: {branch_count: 1, keep_count: 1, contrarian_rule: false}
ensemble_policy: {synthesizers_per_branch: 1, triage_critics: 2, failure_mode_analysts: 1, judge_panel_size: 3}
iteration_caps: {max_candidate_repairs: 1, max_pod_reruns: 1, max_branch_rebuilds: 1, max_full_cycles_without_pass: 1}
schemas_version: "1.0.0"
validator_suite_version: "1.0.0"
selection_protocol_version: "1.0.0"
tools_enabled: false
storage_root: "ignored"
stage_machine_version: "1.0.0"
tool_policy:
  stage_policies:
    - stage: PLANNING
      allowlisted_tools: []
      restrictions: []
      hitl_triggers: ["network_access", "write_outside_workspace", "shell_exec", "secrets_access"]
hitl_policy:
  when_to_interrupt: ["blocking_unknowns", "high_severity_decision", "tool_approval_required"]
  approval_roles: ["user", "human_reviewer"]
""".strip(),
        encoding="utf-8",
    )
    return config


def test_engine_run_resume_list_show(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("Build planning system", encoding="utf-8")
    config = _config(tmp_path)

    engine = Engine(storage_root=tmp_path / "runs")
    result = engine.run(brief, config)
    assert result.frozen_artifact_id == "frozen_B1-SA_vFinal"

    resumed = engine.resume(str(result.run_id))
    assert resumed.frozen_artifact_id == "frozen_B1-SA_vFinal"

    artifacts = engine.list_artifacts(str(result.run_id))
    assert "freeze_v1" in artifacts
    assert "run_metrics_v1" in artifacts

    payload = engine.show_artifact(str(result.run_id), "freeze_v1")
    assert payload["artifact_type"] == "freeze_record"

    metrics = engine.show_artifact(str(result.run_id), "run_metrics_v1")
    assert metrics["artifact_type"] == "run_metrics"
    assert float(metrics["payload"]["total_duration_sec"]) >= 0.0
    assert set(metrics["payload"]["stage_durations_sec"].keys()) == set(STAGES)

    checkpoints = list((tmp_path / "runs" / str(result.run_id) / "checkpoints").glob("cp-*.json"))
    assert len(checkpoints) == len(STAGES)

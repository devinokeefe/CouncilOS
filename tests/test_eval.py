from __future__ import annotations

from pathlib import Path

from council_os.eval.harness import run_eval
from council_os.orchestrator.engine import Engine


def test_eval_harness(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    engine = Engine(storage_root=runs)

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

    golden = tmp_path / "golden"
    golden.mkdir(parents=True)
    (golden / "b1.md").write_text("Brief 1", encoding="utf-8")
    (golden / "b2.md").write_text("Brief 2", encoding="utf-8")

    metrics = run_eval(engine, config, golden)
    assert metrics.total_briefs == 2
    assert metrics.successful_runs == 2
    assert metrics.pass_rate == 1.0
    assert metrics.invariant_passed_runs == 2
    assert metrics.invariant_pass_rate == 1.0
    assert metrics.avg_stage_duration_sec >= 0.0
    assert metrics.avg_events_per_run > 0.0
    assert metrics.avg_judge_agreement >= 0.0
    assert metrics.judge_agreement_drift >= 0.0
    assert metrics.blocker_spike_runs >= 0
    assert metrics.blocker_spike_rate >= 0.0

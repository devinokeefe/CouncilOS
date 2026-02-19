from __future__ import annotations

from pathlib import Path

import pytest

from council_os.audit.event_log import read_events
from council_os.orchestrator.engine import Engine


def _config(path: Path) -> None:
    path.write_text(
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
ensemble_policy: {synthesizers_per_branch: 0, triage_critics: 2, failure_mode_analysts: 1, judge_panel_size: 3}
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


def test_resume_replays_incomplete_run_with_snapshots(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config)
    brief.write_text("Build planning system", encoding="utf-8")

    engine = Engine(storage_root=storage_root)
    with pytest.raises(RuntimeError, match="HITL interrupt required"):
        engine.run(brief, config)

    run_dirs = list(storage_root.glob("*"))
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    assert (run_dirs[0] / "config.snapshot.yml").exists()
    assert (run_dirs[0] / "brief.snapshot.md").exists()

    with pytest.raises(RuntimeError, match="HITL interrupt required"):
        engine.resume(run_id)


def test_resume_continues_from_checkpoint_stage_boundary(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config)
    brief.write_text("Build planning system", encoding="utf-8")

    engine = Engine(storage_root=storage_root)
    with pytest.raises(RuntimeError, match="HITL interrupt required"):
        engine.run(brief, config)

    run_dirs = list(storage_root.glob("*"))
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    events_before = list(read_events(run_dirs[0]))
    intake_before = [e for e in events_before if e.type == "stage_transition" and e.stage == "intake"]
    assert len(intake_before) == 1

    with pytest.raises(RuntimeError, match="HITL interrupt required"):
        engine.resume(run_id)

    events_after = list(read_events(run_dirs[0]))
    intake_after = [e for e in events_after if e.type == "stage_transition" and e.stage == "intake"]
    assert len(intake_after) == 1

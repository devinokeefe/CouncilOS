from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from council_os.cli import app


def _write_config(path: Path, storage_root: Path) -> None:
    path.write_text(
        f"""
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
branching_policy: {{branch_count: 1, keep_count: 1, contrarian_rule: false}}
ensemble_policy: {{synthesizers_per_branch: 1, triage_critics: 2, failure_mode_analysts: 1, judge_panel_size: 3}}
iteration_caps: {{max_candidate_repairs: 1, max_pod_reruns: 1, max_branch_rebuilds: 1, max_full_cycles_without_pass: 1}}
schemas_version: "1.0.0"
validator_suite_version: "1.0.0"
selection_protocol_version: "1.0.0"
tools_enabled: false
storage_root: "{storage_root.as_posix()}"
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


def test_cli_run_and_diff(tmp_path: Path) -> None:
    runner = CliRunner()
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    storage_root = tmp_path / "runs"
    _write_config(config, storage_root)
    brief.write_text("Build planning system", encoding="utf-8")

    run_result = runner.invoke(app, ["run", "--config", str(config), "--brief", str(brief)])
    assert run_result.exit_code == 0
    run_payload = json.loads(run_result.stdout)
    run_id = run_payload["run_id"]

    diff_result = runner.invoke(
        app,
        [
            "diff",
            "--run-id",
            run_id,
            "--a",
            "freeze_v1",
            "--b",
            "freeze_v1",
            "--storage-root",
            str(storage_root),
        ],
    )
    assert diff_result.exit_code == 0
    diff_payload = json.loads(diff_result.stdout)
    assert diff_payload["equal"] is True

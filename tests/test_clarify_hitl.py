from __future__ import annotations

from pathlib import Path

import pytest

import council_os.orchestrator.engine as engine_module
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


def test_clarify_blocking_unknowns_interrupt_and_resume_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config)
    brief.write_text("Build planning system", encoding="utf-8")

    original = engine_module.project_capsule

    def blocking_capsule(run_id, brief_text):  # type: ignore[no-untyped-def]
        env = original(run_id, brief_text)
        env.payload["open_questions"][0]["blocking"] = True
        return env

    monkeypatch.setattr(engine_module, "project_capsule", blocking_capsule)

    engine = Engine(storage_root=storage_root)
    with pytest.raises(RuntimeError, match="blocking unknowns"):
        engine.run(brief, config)

    run_dirs = list(storage_root.glob("*"))
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    resp = engine.interrupt_response(run_id, {"approved": True, "note": "continue"})
    assert bool(resp["accepted"])

    resumed = engine.resume(run_id)
    assert resumed.frozen_artifact_id.startswith("frozen_")

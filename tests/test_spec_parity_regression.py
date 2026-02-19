from __future__ import annotations

import json
from pathlib import Path

from council_os.orchestrator.engine import Engine


def _config(path: Path, storage_root: Path) -> None:
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
ensemble_policy: {{synthesizers_per_branch: 1, triage_critics: 2, failure_mode_analysts: 2, judge_panel_size: 3}}
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


def test_spec_parity_regression(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config, storage_root)
    brief.write_text("Build planning system", encoding="utf-8")

    engine = Engine(storage_root=storage_root)
    result = engine.run(brief, config)
    run_root = storage_root / str(result.run_id)

    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest["schema_versions"], dict)
    assert "artifacts" in manifest["schema_versions"]
    for role_name, entry in manifest["model_portfolio"].items():
        assert role_name
        assert set(entry.keys()) == {"provider", "model", "temperature", "max_tokens", "prompt_version_hash"}

    allowed_stages = {
        "intake",
        "clarify",
        "draft_pods",
        "branch",
        "synthesize",
        "validate",
        "triage",
        "failure_mode",
        "judge",
        "freeze",
    }
    allowed_types = {
        "stage_transition",
        "llm_request",
        "llm_response",
        "artifact_write",
        "validator_result",
        "triage_result",
        "failure_mode_result",
        "judge_pairwise",
        "decision",
        "checkpoint",
        "tool_call",
        "tool_result",
        "error",
    }
    stages = set()
    types = set()
    for line in (run_root / "events.jsonl").read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        stages.add(ev["stage"])
        types.add(ev["type"])
    assert stages.issubset(allowed_stages)
    assert types.issubset(allowed_types)

    triage_artifacts = list((run_root / "artifacts" / "triage_findings").glob("*.json"))
    failure_mode_artifacts = list((run_root / "artifacts" / "failure_mode_findings").glob("*.json"))
    assert len(triage_artifacts) == 3
    assert len(failure_mode_artifacts) == 3

from __future__ import annotations

from pathlib import Path

from council_os.audit.event_log import read_events
from council_os.orchestrator.engine import Engine


def _config(path: Path, storage_root: Path) -> None:
    path.write_text(
        f"""
roles:
  requirements_pod:
    model_provider: openai
    model_name: gpt-mock
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: p1
    tools_allowed: []
  synthesizer_a:
    model_provider: openai
    model_name: gpt-mock-a
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: sa
    tools_allowed: []
  synthesizer_b:
    model_provider: anthropic
    model_name: claude-mock-b
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: sb
    tools_allowed: []
  judge_1:
    model_provider: mock_j
    model_name: judge-mock
    temperature: 0.0
    max_tokens: 100
    prompt_version_hash: j1
    tools_allowed: []
branching_policy: {{branch_count: 2, keep_count: 2, contrarian_rule: false}}
ensemble_policy: {{synthesizers_per_branch: 2, triage_critics: 2, failure_mode_analysts: 2, judge_panel_size: 3}}
iteration_caps: {{max_candidate_repairs: 2, max_pod_reruns: 1, max_branch_rebuilds: 1, max_full_cycles_without_pass: 1}}
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


def test_runtime_audit_events_include_required_fields(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config, storage_root)
    brief.write_text("Build planning system", encoding="utf-8")

    engine = Engine(storage_root=storage_root)
    result = engine.run(brief, config)
    events = list(read_events(storage_root / str(result.run_id)))

    llm_requests = [e for e in events if e.type == "llm_request"]
    llm_responses = [e for e in events if e.type == "llm_response"]

    assert llm_requests
    assert llm_responses
    assert "prompt_hash" in llm_requests[0].payload
    assert "request" in llm_requests[0].payload
    assert "raw_output_hash" in llm_responses[0].payload
    assert "latency_ms" in llm_responses[0].payload
    assert "parse_valid" in llm_responses[0].payload

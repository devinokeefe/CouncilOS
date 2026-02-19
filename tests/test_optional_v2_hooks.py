from __future__ import annotations

from pathlib import Path

from council_os.orchestrator.engine import Engine


def _config(path: Path, storage_root: Path, store_full_prompts: bool) -> None:
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
store_full_prompts: {str(store_full_prompts).lower()}
storage_root: "{storage_root.as_posix()}"
stage_machine_version: "1.0.0"
tracing:
  enabled: false
  provider: "noop"
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


def test_optional_markdown_render_and_prompt_policy(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config, storage_root, store_full_prompts=True)
    brief.write_text("Build planning system", encoding="utf-8")

    engine = Engine(storage_root=storage_root)
    result = engine.run(brief, config)
    run_root = storage_root / str(result.run_id)

    markdown = run_root / "frozen_plan.md"
    assert markdown.exists()
    text = markdown.read_text(encoding="utf-8")
    assert "# Frozen Plan" in text

    events = engine.events(str(result.run_id))
    requests = [e for e in events if e["type"] == "llm_request"]
    assert requests
    request_payload = requests[0]["payload"]
    assert "request" in request_payload
    assert "messages" in request_payload["request"]

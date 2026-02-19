from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from council_os.service import create_app


def _write_config(path: Path, storage_root: Path) -> None:
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


def test_service_endpoints(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    app = create_app(storage_root)
    client = TestClient(app)

    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _write_config(config, storage_root)
    brief.write_text("Build planning system", encoding="utf-8")

    run_resp = client.post("/runs", json={"config_path": str(config), "brief_path": str(brief)})
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

    resume_resp = client.post(f"/runs/{run_id}/resume")
    assert resume_resp.status_code == 200

    artifacts_resp = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts_resp.status_code == 200
    assert "frozen" in " ".join(artifacts_resp.json()["artifacts"])

    events_resp = client.get(f"/runs/{run_id}/events")
    assert events_resp.status_code == 200
    events_payload = events_resp.json()["events"]
    assert len(events_payload) > 0

    first_event_id = events_payload[0]["event_id"]
    cursor_resp = client.get(f"/runs/{run_id}/events", params={"since_event_id": first_event_id})
    assert cursor_resp.status_code == 200
    assert len(cursor_resp.json()["events"]) <= len(events_payload)

    status_resp = client.get(f"/runs/{run_id}/status")
    assert status_resp.status_code == 200
    assert bool(status_resp.json()["is_complete"])

    interrupt_resp = client.post(
        f"/runs/{run_id}/interrupt_response",
        json={"response": {"approved": True, "note": "continue"}},
    )
    assert interrupt_resp.status_code == 200
    assert bool(interrupt_resp.json()["accepted"])

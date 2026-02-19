from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from council_os.audit.event_log import read_events
from council_os.orchestrator.checkpoints import latest_checkpoint
from council_os.orchestrator.engine import Engine
from council_os.orchestrator.policies import parse_tool_policy
from council_os.orchestrator.tools import IdempotencyLedger, compute_idempotency_key


def _config(path: Path, storage_root: Path, tools_enabled: bool) -> None:
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
tools_enabled: {str(tools_enabled).lower()}
storage_root: "{storage_root.as_posix()}"
stage_machine_version: "1.0.0"
tool_policy:
  stage_policies:
    - stage: PLANNING
      allowlisted_tools: ["mock.counter"]
      restrictions: []
      hitl_triggers: ["shell_exec"]
hitl_policy:
  when_to_interrupt: ["blocking_unknowns", "high_severity_decision", "tool_approval_required"]
  approval_roles: ["user", "human_reviewer"]
""".strip(),
        encoding="utf-8",
    )


def test_idempotency_ledger_skips_repeat_side_effect(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    engine = Engine(storage_root=tmp_path)
    engine._tool_policy = parse_tool_policy(  # noqa: SLF001
        {
            "stage_policies": [
                {
                    "stage": "draft_pods",
                    "allowlisted_tools": ["mock.counter"],
                    "restrictions": [],
                    "hitl_triggers": [],
                }
            ]
        }
    )

    ledger = IdempotencyLedger(run_root)
    run_id = uuid4()
    first = engine._execute_tool(  # noqa: SLF001
        run_id,
        run_root,
        ledger,
        "draft_pods",
        "mock.counter",
        {"target_resource": "x", "trigger": "none"},
        approved=True,
    )
    second = engine._execute_tool(  # noqa: SLF001
        run_id,
        run_root,
        ledger,
        "draft_pods",
        "mock.counter",
        {"target_resource": "x", "trigger": "none"},
        approved=True,
    )

    assert first["count"] == 1
    assert second["count"] == 1
    events = list(read_events(run_root))
    assert any(e.type == "tool_result" and bool(e.payload.get("skipped")) for e in events)
    assert any(
        e.type == "decision" and e.payload.get("decision") == "tool_approval" and e.payload.get("approved") is True
        for e in events
    )


def test_compute_idempotency_key_matches_spec_formula() -> None:
    run_id = uuid4()
    stage = "draft_pods"
    tool_name = "mock.counter"
    normalized_args = '{"target_resource":"x","trigger":"none"}'
    target_resource = "x"
    expected = hashlib.sha256(f"{run_id}{stage}{tool_name}{normalized_args}{target_resource}".encode()).hexdigest()
    assert compute_idempotency_key(run_id, stage, tool_name, normalized_args, target_resource) == expected


def test_engine_tools_enabled_writes_ledger_and_checkpoint_ref(tmp_path: Path) -> None:
    storage_root = tmp_path / "runs"
    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    _config(config, storage_root, tools_enabled=True)
    brief.write_text("Build planning system", encoding="utf-8")

    engine = Engine(storage_root=storage_root)
    result = engine.run(brief, config)

    run_root = storage_root / str(result.run_id)
    assert (run_root / "tool_ledger.json").exists()

    cp = latest_checkpoint(run_root)
    assert cp.tool_side_effect_ledger_ref == "tool_ledger.json"

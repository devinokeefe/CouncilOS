from __future__ import annotations

from pathlib import Path

import pytest

from council_os.agents.schemas import EvidencePointer, JudgePairwisePayload
from council_os.orchestrator import engine as engine_module
from council_os.orchestrator.engine import Engine
from council_os.orchestrator.selection import TournamentResult


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
    return config


def test_interrupt_when_no_acceptable_candidate_after_cycle_cap(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("Build planning system", encoding="utf-8")
    config = _config(tmp_path)

    engine = Engine(storage_root=tmp_path / "runs")
    with pytest.raises(RuntimeError, match="HITL interrupt required"):
        engine.run(brief, config)


def test_high_impact_tie_requires_hitl(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    brief = tmp_path / "brief.md"
    brief.write_text("Build planning system", encoding="utf-8")
    config = tmp_path / "config_high_impact.yml"
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
iteration_caps: {max_candidate_repairs: 1, max_pod_reruns: 0, max_branch_rebuilds: 0, max_full_cycles_without_pass: 1}
schemas_version: "1.0.0"
validator_suite_version: "1.0.0"
selection_protocol_version: "1.0.0"
tools_enabled: false
storage_root: "ignored"
stage_machine_version: "1.0.0"
high_impact: true
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

    def fake_tournament(run_id: str, candidate_ids: list[str], judge_panel_size: int = 3) -> TournamentResult:
        _ = run_id, candidate_ids, judge_panel_size
        pairwise = JudgePairwisePayload(
            comparison={"a": "B1-SA", "b": "B1-SA"},
            judge_id="J1",
            judge_model="mock/mock-v1",
            winner="a",
            scores={
                "feasibility": 8,
                "testability": 8,
                "governance": 8,
                "architecture": 8,
                "clarity": 8,
            },
            evidence=[
                EvidencePointer(
                    candidate_id="B1-SA",
                    artifact_ref="candidate",
                    artifact_id="DEC1",
                    json_pointer="/plan_package/decision_log/0",
                )
            ],
            notes="forced tie memo path",
        )
        return TournamentResult(winner="B1-SA", pairwise_results=[pairwise], arbitrator_memo="Forced tie memo")

    monkeypatch.setattr(engine_module, "run_tournament", fake_tournament)

    engine = Engine(storage_root=tmp_path / "runs")
    with pytest.raises(RuntimeError, match="high-impact tie arbitration"):
        engine.run(brief, config)

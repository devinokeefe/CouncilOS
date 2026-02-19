from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from council_os.agents.schemas import ProjectCapsulePayload
from council_os.orchestrator import engine as engine_module
from council_os.orchestrator.engine import Engine
from council_os.orchestrator.stages import plan_candidate
from council_os.orchestrator.triage import critic_findings, merge_findings, triage_repair_loop


def _capsule() -> ProjectCapsulePayload:
    return ProjectCapsulePayload(
        brief="brief",
        problem_statement="problem",
        goals=["g"],
        non_goals=["n"],
        constraints=[{"id": "CNS1", "type": "tech", "text": "t"}],
        assumptions=[
            {
                "id": "A1",
                "text": "a",
                "impact": "low",
                "confidence": "high",
                "needs_confirmation": False,
            }
        ],
        open_questions=[{"id": "Q1", "text": "q", "impact": "low", "blocking": False}],
        success_metrics=["s"],
        stakeholders=[],
        glossary=[],
    )


def test_triage_merge_and_repair_loop_clears_blocker() -> None:
    env = plan_candidate(
        uuid4(),
        _capsule(),
        "branch_B1_v1",
        "B1",
        "SA",
        ["requirements_v1"],
    )
    payload = env.payload
    payload["plan_package"]["acceptance_tests"] = []

    from council_os.agents.schemas import PlanCandidatePayload

    candidate = PlanCandidatePayload.model_validate(payload)
    a = critic_findings("candidate_B1-SA_v1", candidate, "A")
    b = critic_findings("candidate_B1-SA_v1", candidate, "B")
    merged = merge_findings(a, b)
    assert any(d.label == "blocker" for d in merged.defects)

    repaired, final_findings = triage_repair_loop("candidate_B1-SA_v1", candidate, max_repairs=2)
    assert not any(d.label == "blocker" for d in final_findings.defects)
    assert len(repaired.plan_package.acceptance_tests) >= 1
    assert all(re.match(r"^AT\d+$", at.id) for at in repaired.plan_package.acceptance_tests)
    assert len({at.id for at in repaired.plan_package.acceptance_tests}) == len(repaired.plan_package.acceptance_tests)


def test_engine_revalidates_after_triage_repairs(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def broken_plan_candidate(*args, **kwargs):  # type: ignore[no-untyped-def]
        env = plan_candidate(*args, **kwargs)
        payload = env.model_dump(by_alias=True)
        payload["payload"]["plan_package"]["acceptance_tests"] = []
        from council_os.agents.schemas import ArtifactEnvelope

        return ArtifactEnvelope.model_validate(payload)

    monkeypatch.setattr(engine_module, "plan_candidate", broken_plan_candidate)

    config = tmp_path / "config.yml"
    brief = tmp_path / "brief.md"
    storage_root = tmp_path / "runs"
    config.write_text(
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
iteration_caps: {{max_candidate_repairs: 2, max_pod_reruns: 0, max_branch_rebuilds: 0, max_full_cycles_without_pass: 1}}
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
    brief.write_text("Build planning system", encoding="utf-8")

    result = Engine(storage_root=storage_root).run(brief, config)
    assert result.frozen_artifact_id

    run_root = storage_root / str(result.run_id) / "artifacts" / "validator_report"
    assert any(path.name.endswith("_triage_v1.json") for path in run_root.glob("*.json"))

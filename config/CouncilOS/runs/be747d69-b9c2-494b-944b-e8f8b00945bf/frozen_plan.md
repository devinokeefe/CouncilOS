# Frozen Plan

## Meta
- plan_id: `db747cef-bb87-45c6-a3c0-19c7f4155876`
- version: `vFinal`
- created_at: `2026-02-19T23:06:15.951486+00:00`
- source_run_id: `be747d69-b9c2-494b-944b-e8f8b00945bf`
- schema_version: `1.0.0`
- candidate_id: `B1-SA`
- branch_id: `B1`
- plan_status: `FROZEN`

## Project Capsule
- problem_statement: Build a planning system that turns vague briefs into frozen plans.
- brief:
  # Brief 01
  Build a high-confidence planning workflow for project variant 01.
### Goals
- Produce deterministic frozen plans
- Preserve strong auditability
### Non-Goals
- Autonomous code execution in v1
### Constraints
- CNS1 (tech): Python 3.12
### Assumptions
- A1 (impact: medium, confidence: high, needs_confirmation: False): Users provide trusted prompts
### Open Questions
- Q1 (impact: medium, blocking: False): Need tools layer in v2?
### Success Metrics
- At least one candidate frozen
- Checkpoint resume works

## Requirements
- R1 [MUST]: Freeze plan — rationale: Primary outcome
- R2 [SHOULD]: Support resume — rationale: Operational resilience

## Acceptance Tests
- AT1 [system] -> R1: procedure: Run council pipeline; pass_criteria: plan_frozen exists

## Architecture Options
### Option O1
- summary: Deterministic orchestrator with immutable artifacts
- components:
  - C1: Orchestrator — Stage machine
  - C2: ArtifactStore — Immutable writes
- interfaces:
  - IF1: C1 -> C2 — write/read artifacts
- tradeoffs:
  - pros: Determinism
  - cons: Extra boilerplate
- risks: K1

## Chosen Architecture
- option_id: O1
- rationale: Balances determinism and practical implementation
- high_level_dataflow: brief -> drafts -> candidate -> validate -> freeze
- key_design_decisions: DEC1

## Milestones
### M1: Phase 1
- deliverables:
  - DL1: End-to-end frozen plan pipeline
- exit_criteria:
  - CLI run command produces frozen plan
- depends_on: (none)

## Risk Register
- K1 [high]: Coverage regressions mitigation: Hard validator checks (owner: orchestrator, status: planned)

## Governance
### Failure Modes
- FM1 [high] (CUSTOM_V1 / coverage): MUST requirements may be untested; closure: mitigated — Coverage validator enforces mapping (signoff: system)
### Tool Policy
- PLANNING: allowlisted_tools=[]; restrictions=[]; hitl_triggers=['network_access', 'write_outside_workspace', 'shell_exec', 'secrets_access']
### HITL Policy
- when_to_interrupt: blocking_unknowns, high_severity_decision, tool_approval_required
- approval_roles: user, human_reviewer

## Decision Log
### DEC1
- question: Which architecture should v1 use?
- choice: Deterministic orchestrator + immutable store
- rationale: Improves auditability and replay
- alternatives_considered: Chat-log driven planner
- what_would_change_this_decision: Need lower implementation overhead
- evidence_pointers:
  - branch_B1_v1 DEC1 /plan_package/architecture/chosen (Selected architecture rationale)

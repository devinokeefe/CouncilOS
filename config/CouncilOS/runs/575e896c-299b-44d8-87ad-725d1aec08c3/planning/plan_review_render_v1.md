# Plan Review Draft

## Requirements
- R1: Freeze plan
- R2: Support resume

## Acceptance Tests
- AT1: Run council pipeline (pass: plan_frozen exists)

## Assumptions
- A1: Users provide trusted prompts (impact: medium)

## Open Questions
- Q1: Need tools layer in v2?

## Architecture Options
- O1 (selected): Deterministic orchestrator with immutable artifacts

## Governance Highlights
- Tool policy stages: PLANNING
- Failure modes tracked: 1

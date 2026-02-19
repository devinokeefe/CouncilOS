# Council OS (Phase 1)

Phase 1 implementation of the Council OS planning pipeline.

## Quickstart

```bash
uv sync --extra dev
uv run council run --config CouncilOS/config/default.yml --brief CouncilOS/eval/golden_briefs/brief_01.md
```

For real LLM generation (instead of mock artifacts), set `OPENROUTER_API_KEY` and run:

```bash
uv run council run --config CouncilOS/config/llm_openai_anthropic.yml --brief CouncilOS/eval/golden_briefs/brief_01.md
```

For a larger multi-model council, set `OPENROUTER_API_KEY`:

```bash
uv run council run --config CouncilOS/config/council_os_hq.yml --brief CouncilOS/eval/golden_briefs/brief_01.md
```

Optional OpenRouter headers can be set via `OPENROUTER_HTTP_REFERER` and `OPENROUTER_APP_TITLE`.

For mocked CI record/replay, set `mock_record_replay_file` in config (for example `CouncilOS/runs/mock_cassette.json`).

## Commands

- `council run --config <path> --brief <path>`
- `council resume --run-id <id> [--checkpoint-id <id>]`
- `council fork --checkpoint-id <id> --new-config <path>`
- `council show --run-id <id> --artifact <artifact_id> [--render]`
- `council list --run-id <id> --artifacts`
- `council diff --run-id <id> --a <artifact_id> --b <artifact_id>`
- `council eval --config <path> --golden <path>`

## Feedback Gates

Optional planning checkpoints:

- `--feedback-clarify` enables the Clarify Intent Gate.
- `--feedback-plan-review` enables the Plan Review Gate and approval gating.
- `--feedback-provider {auto,cli,file}` selects the input provider.
- `--response-file <path>` provides a response artifact for offline resume.
- `--max-plan-review-rounds <N>` controls the review loop limit.

Offline flow:

- Run with feedback enabled; the process pauses and emits `planning/pending_action.json`.
- Resume with `council resume --resume-run <run_id> --response-file <path>`.

## Implementation v2

### Execution Profiles

Define a profile catalog and pass it via `v2_inputs.execution_profile_catalog` in the implementation config.

Minimal example:

```json
{
  "schema_version": "2.0.0",
  "catalog_id": "profiles_default",
  "profiles": [
    {
      "profile_id": "legacy_v1",
      "profile_version": "1.0.0",
      "runtime": { "lang": "python", "version": "3.12" },
      "base_image": { "name": "org/runtime", "digest": "sha256:..." },
      "deps": { "required": [], "allowed": [], "forbidden": [] },
      "network": { "default": "deny", "allowlist_domains": [], "allowlist_ports": [443] },
      "package_manager": { "pip_install": "deny", "conda_install": "deny" }
    }
  ]
}
```

### Expectations + Evidence

Provide `expectation_registry` and `evidence_index` (auto-generated if omitted). Expectations must exist before remote ops and are enforced at Freeze.

Evidence index entries map expectation IDs to evidence pointers:

```json
{
  "schema_version": "2.0.0",
  "evidence": [
    {
      "expectation_id": "EXP-PLAN-R1",
      "status": "pass",
      "evidence_pointers": [
        { "artifact_ref": "test_results_v1", "json_pointer": "/" }
      ]
    }
  ]
}
```

### Remote Ops Manifests

Remote ops are driven by `remote_ops_manifest`. Each op references expectation IDs and includes an idempotency key.

```json
{
  "schema_version": "2.0.0",
  "ops": [
    {
      "op_id": "ROP-001",
      "profile_id": "legacy_v1",
      "tool_id": "tool1",
      "idempotency_key": "run/task/rop",
      "expected": ["EXP-PLAN-R1"]
    }
  ]
}
```

### Diff Reports

On expectation mismatch, a diff report is emitted as a canonical artifact and copied to
`workspace/canonical/diff_reports/<expectation_id>/<run_id>.json`.

### Migration Utility

Use `CouncilOS/tools/migrate_v1_to_v2.py` to convert v1 `repo_context` and `work_plan` artifacts to v2 outputs.

## Optional Service API

`council_os.service:app` exposes:

- `POST /runs`
- `POST /runs/{id}/resume`
- `POST /runs/{id}/interrupt_response`
- `GET /runs/{id}/artifacts`
- `GET /runs/{id}/events`
- `GET /runs/{id}/status`

`GET /runs/{id}/events` accepts optional query param `since_event_id=<uuid>` for cursor reads.

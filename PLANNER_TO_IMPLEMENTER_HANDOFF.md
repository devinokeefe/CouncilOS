This plan assumes: no content safety scanning of user inputs. We do keep schema/ID validation and hash integrity checks because the system can’t function deterministically without them.

1) Goal and invariants
Goal

Make it impossible for implementation to start from the wrong plan, wrong approval, wrong clarifications, wrong repo commit, or wrong config — while keeping your existing contract that implementation consumes only the frozen plan for semantics.

Hard invariants

I1 Semantic authority: Implementation treats plan_package_final.json as the only semantic source of truth. Everything else is provenance/context.

I2 Hash binding: If plan review was enabled, a plan cannot be frozen/handed off unless an approval artifact exists and its approved hash matches the plan content hash.

I3 Deterministic bundle: Handoff uses one canonical planning_handoff_bundle.json that self-enumerates required artifacts + hashes and has a deterministic handoff_digest.

I4 Replayability: If artifacts exist (final plan, bundle, ack), do not re-generate or re-prompt. Resume must be deterministic.

I5 Repo pinning: Implementation must not proceed if the local repo state does not match the pinned snapshot (commit + dirty flag), unless an explicit override flag is used (and recorded).

2) Artifacts (canonical JSON unless stated)
Planning produces

plan_package_final.json (existing)

Must have plan_status: "FROZEN".

config_snapshot.json (new)

Fully resolved runtime config used for the run (defaults + CLI flags applied).

plan_freeze_record.json (new)

The “seal” tying together: plan content hash, approval (if required), clarifications (if enabled), config snapshot, and planning run id.

human_feedback_bundle.json (new, small)

Condensed, stable summary + refs:

clarifications (resolved values + evidence pointers)

plan review rounds (refs to feedback + diff summary)

approval ref

Do not embed large transcripts; keep it link-heavy.

planning_handoff_bundle.json (new or extend existing)

The single authoritative boundary object.

Contains:

required artifact list with hashes

repo snapshot

plan hash

approval required flag + approval hash

handoff_digest = hash(canonical JSON of the bundle)

(Optional but useful) handoff_record.json

A simple “ready_for_implementation” marker. (You can skip this if you feel it’s redundant with bundle + freeze record.)

Implementation produces

handoff_ack.json (new)

Written before intake begins; marks the official start and pins what was accepted.

handoff_rejection.json (new, on failure)

Structured reasons + expected vs actual.

3) Canonical hashing (required utility)

Implement one shared library function used by both planning + implementation:

3.1 Canonical JSON bytes

UTF-8

sorted keys

stable separators (no whitespace)

stable list order (you control ordering; don’t sort lists unless explicitly defined)

3.2 Two hashes

artifact_hash = sha256(canonical JSON bytes of the artifact)

plan_content_hash = sha256(canonical JSON bytes of the plan excluding volatile metadata)

Volatile fields to exclude from plan hash

timestamps (created_at, frozen_at, etc.)

run ids

status fields if they change between draft/final (e.g., plan_status)

anything non-semantic that may be injected during freeze

Implementation detail: implement a normalize_plan_for_hash(plan_obj) that deep-copies then deletes known volatile paths before hashing.

This is the main fix to avoid “approval breaks because freeze added a timestamp”.

4) Minimal schemas (implement exactly)
4.1 plan_freeze_record.json
{
  "schema_version": "2.1.0",
  "plan_id": "…",
  "planning_run_id": "…",
  "frozen_at": "…",
  "plan_package_final_ref": {"path": "planning/plan_package_final.json", "sha256": "…"},
  "plan_content_hash": "sha256:…",
  "interactive_flags": {
    "clarify_intent_enabled": true,
    "plan_review_enabled": true
  },
  "clarification_resolutions_ref": {"path": "planning/clarification_resolutions.json", "sha256": "…"},
  "plan_approval_ref": {"path": "planning/plan_approval.json", "sha256": "…"},
  "approved_plan_hash": "sha256:…",
  "config_snapshot_ref": {"path": "planning/config_snapshot.json", "sha256": "…"},
  "human_feedback_bundle_ref": {"path": "planning/human_feedback_bundle.json", "sha256": "…"},
  "notes": "Frozen after review round N"
}
4.2 planning_handoff_bundle.json (self-manifesting)
{
  "schema_version": "2.1.0",
  "handoff_bundle_id": "HB-…",
  "planning_run_id": "…",
  "implementation_run_id": null,

  "final_plan": {"path": "planning/plan_package_final.json", "sha256": "…", "plan_content_hash": "sha256:…"},
  "freeze_record": {"path": "planning/plan_freeze_record.json", "sha256": "…"},

  "approval": {
    "required": true,
    "path": "planning/plan_approval.json",
    "sha256": "…",
    "approved_plan_hash": "sha256:…"
  },

  "clarifications": {
    "required": true,
    "path": "planning/clarification_resolutions.json",
    "sha256": "…"
  },

  "human_feedback_bundle": {"path": "planning/human_feedback_bundle.json", "sha256": "…"},
  "config_snapshot": {"path": "planning/config_snapshot.json", "sha256": "…"},

  "repo_snapshot": {
    "commit_sha": "abc123…",
    "branch": "main",
    "dirty": false
  },

  "required_artifacts": [
    {"path": "planning/plan_package_final.json", "sha256": "…"},
    {"path": "planning/plan_freeze_record.json", "sha256": "…"},
    {"path": "planning/config_snapshot.json", "sha256": "…"},
    {"path": "repo_context.json", "sha256": "…"},
    {"path": "workspace_context.json", "sha256": "…"}
  ],

  "optional_artifacts": [
    {"path": "planning/human_feedback_bundle.json", "sha256": "…"}
  ],

  "handoff_digest": "sha256:…"
}
How to compute handoff_digest

Set "handoff_digest": "" temporarily

canonicalize the bundle JSON

sha256 that canonical bytes

write it back as "handoff_digest"

Implementation side recomputes the same and must match.

4.3 handoff_ack.json
{
  "schema_version": "2.1.0",
  "implementation_run_id": "…",
  "accepted_at": "…",
  "accepted_handoff_digest": "sha256:…",
  "accepted_plan_content_hash": "sha256:…",
  "accepted_repo_commit": "abc123…",
  "accepted_config_sha256": "sha256:…",
  "implementation_engine_version": "…"
}
5) Planning-side procedure (finalize + handoff)

Implement a single entrypoint: finalize_and_write_handoff(run_ctx)

Step A — Gather inputs

latest draft: plan_package_draft_vN.json

if review enabled: plan_approval.json

if clarify enabled: clarification_resolutions.json

repo_context.json + repo snapshot (commit, dirty, branch)

workspace_context.json

resolved config_snapshot.json

Step B — Finalization gate checks (hard fail)

Approval gate (only if review enabled):

compute draft_plan_content_hash

require plan_approval.approved == true

require plan_approval.approved_plan_hash == draft_plan_content_hash

Clarification gate (only if clarify enabled):

require clarification_resolutions.json exists and schema-valid

No safety scanning; only parse/schema/IDs.

Step C — Freeze plan deterministically

Write plan_package_final.json as a copy of the approved draft (or latest draft if review disabled)

Set plan_status = "FROZEN"

Do not run any LLMs here

Compute plan_content_hash using normalize_plan_for_hash().

Step D — Build human_feedback_bundle.json deterministically

Include:

clarifications: resolved values + evidence pointers to clarification_resolutions

review rounds: refs to plan_review_feedback_vN.json + plan_diff_summary_vN.json (don’t inline large text)

approval ref

Hash it.

Step E — Write plan_freeze_record.json

Tie together:

plan_content_hash

approval (required?) + approved_plan_hash

clarifications (required?) + hash

config_snapshot hash

human_feedback_bundle hash

Step F — Write planning_handoff_bundle.json

Populate required/optional artifacts + hashes + repo_snapshot

Compute and set handoff_digest

Step G — Emit planning events

Append JSONL events (names illustrative):

PLAN_FINALIZATION_STARTED

PLAN_FROZEN (include plan_content_hash)

HANDOFF_BUNDLE_WRITTEN (include handoff_digest)

HANDOFF_READY

Idempotency

If plan_package_final.json + planning_handoff_bundle.json already exist:

verify hashes match expected

if match: no-op

if mismatch: fail unless --force (and record that)

6) Implementation-side acceptance protocol (before intake)

Add accept_handoff() executed before any Intake work.

Step A — Load + validate bundle

schema validate planning_handoff_bundle.json

recompute handoff_digest and require equality

verify all required_artifacts exist and their sha256 match

Step B — Validate plan + freeze invariants

load plan_package_final.json

require plan_status == "FROZEN"

compute plan_content_hash and require match with:

bundle final_plan.plan_content_hash

freeze record plan_content_hash

Step C — Validate approval/clarifications if required

if bundle approval.required:

load approval artifact

require approved == true

require approved_plan_hash == plan_content_hash

if bundle clarifications.required:

load clarifications and schema validate

Step D — Pin repo snapshot

compare current repo:

HEAD commit == bundle repo_snapshot.commit_sha

dirty == bundle repo_snapshot.dirty

if mismatch: write handoff_rejection.json and stop

optional override flag allowed (record override artifact/event)

Step E — Copy-on-import core artifacts (recommended)

Copy into implementation run workspace:

inputs/planning/plan_package_final.json

inputs/planning/plan_freeze_record.json

inputs/planning/config_snapshot.json

inputs/planning/human_feedback_bundle.json (if present)

inputs/planning/clarification_resolutions.json (if required)

inputs/planning/plan_approval.json (if required)
Each copy records provenance: {source_path, source_sha256}.

Step F — Write handoff_ack.json + emit event

Append event HANDOFF_ACCEPTED with accepted hashes/digest

Then proceed into your existing intake → work_planning → … stages unchanged.

Idempotency

If handoff_ack.json exists:

recompute digest/hashes; if identical, resume

if different, fail hard (prevents switching inputs mid-run)

7) Code integration points (match your repo layout)
Planning orchestrator

AGI/council_os/orchestrator/hq_pipeline.py

add finalize_and_write_handoff() after plan approval loop completes

New module folder (recommended):

AGI/council_os/orchestrator/handoff/

hashing.py (canonical json + plan normalization)

schemas.py (pydantic/dataclass schemas)

finalize.py (freeze + records)

bundle.py (handoff bundle writer + digest)

repo_snapshot.py (read repo commit/dirty/branch deterministically)

Implementation engine

AGI/council_os/implementation/engine.py

add CLI option --from-handoff <path>

call accept_handoff(from_handoff_path) before Intake stage

New module folder:

AGI/council_os/implementation/handoff/

accept.py (bundle validation + ack)

importer.py (copy-on-import with provenance)

errors.py (structured rejection reasons)

8) Test plan (must-have)
Unit tests

Canonical JSON hashing stable across runs

normalize_plan_for_hash() excludes volatile fields correctly

bundle handoff_digest recomputation matches

approval gate rejects mismatch

required_artifacts hash mismatch rejects

Integration tests

Review enabled:

approve → freeze → bundle → accept → ack → intake begins

Review enabled but approval missing:

finalize fails; no bundle

Repo snapshot mismatch:

accept rejects before intake

Restart behavior:

ack exists + same digest → resume

ack exists + different digest → refuse

9) What I deliberately discarded

A separate, standalone “bundle_manifest.json” file: redundant if the bundle is self-manifesting.

Anything resembling safety/content scanning of user inputs (including secrets scanning).

Overly complex “choose smallest passing patch among many candidates” logic at handoff time (not needed here).

If you want to make this even easier for a junior engineer: implement hashing + schemas first, then planning finalize, then implementation accept, then tests. That order avoids thrash.
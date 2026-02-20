1) What CouncilOS vNext is (one sentence)

CouncilOS vNext turns a vague brief into a versioned frozen plan and then executes it via persisted execution contracts (WorkGraph + VerificationPlan), completing milestones only when required expectations are PASS with linked evidence, and evolving the plan only via ChangeRequests → new frozen plan versions with lineage.

2) Non‑negotiable invariants (print these in the repo)

These are the rules that eliminate silent drift and “resume drift”:

Single semantic authority: plan_package_final_vN.json is the only file that defines intent, scope, constraints, and acceptance requirements.

Freeze is hash‑bound to a specific draft: Freeze must reference selected_draft_ref (never “latest draft”) and approvals (if enabled) must match that exact plan hash.

Deterministic hashing everywhere: all integrity checks use one hash_spec.json + one canonicalization implementation shared by planning and implementation.

Handoff is deterministic and verifiable: implementation must verify the manifest digest + all required artifact hashes + repo baseline tree hash (and env snapshot if required).

Plan → execution contracts are compiled and persisted: work_graph.json, verification_plan.json, execution_plan.json are deterministic outputs; resume must reuse them if inputs match.

Done is evidence‑gated: milestone completion requires evidence_index shows all MUST expectations PASS, with resolvable evidence refs, and decision_record.decision == PASS.

No deviation without change control: if execution needs to violate plan constraints/expectations, it must emit change_request_vN.json and stop (or continue only on explicitly unaffected tasks if configured).

3) Unified authority model (the “3 roots + receipt”)

This resolves “which file wins?” forever.

Authority layer	Artifact	What it means	Who writes it
Semantic authority	plan_package_final_vN.json	Truth about intent/requirements/scope/constraints/checks	Planning
Seal/binding authority	plan_freeze_record.json	Binds plan hash + selected draft + approvals/clarifications + snapshot refs + hash spec	Planning
Transport/index authority	handoff_manifest.json	Lists required artifacts with hashes + schema versions + repo acquisition spec + env requirements	Planning
Acceptance receipt	handoff_ack.json	Records exactly what impl accepted/acquired (manifest digest, plan hash, repo tree hash, env fingerprint)	Implementation

Everything else (diff summaries, human summaries, renderings) is derived and must reference source hashes.

4) Canonical hashing (Phase 0 foundation)
4.1 hash_spec.json (required artifact)

This is the contract that makes hashes stable.

Minimum fields:

schema_version: "hash_spec.v1"

hash_alg: "sha256"

canonical_json: "RFC8785" or "JCS-like-v1" (pick one and implement it consistently)

excluded_fields_by_pointer: JSON Pointers excluded from plan content hash (ex: timestamps/run IDs/status)

hash_spec_version: integer or semver (ex: "1")

4.2 Required digests

Plan content hash:
plan_content_hash = hash(canonicalize(plan_package_final_vN.json minus excluded pointers))

Manifest digest:
handoff_digest = hash(canonicalize(handoff_manifest.json with handoff_digest field empty))

4.3 Excluded pointers (start with these)

Exclude volatile runtime fields from plan hashing:

/meta/plan_status

/meta/created_at

/meta/source_run_id

/meta/run_id

/meta/last_updated

any /meta/*timestamp*

any /meta/*nonce*

Rule of thumb: exclude anything that changes on re-run without changing semantic plan meaning.

4.4 One shared reference implementation

Create a single library/module used by both planning and implementation:

canonicalize_json(obj) -> bytes

hash_bytes(bytes) -> "sha256:<hex>"

hash_json(obj, excluded_pointers=[]) -> digest

This prevents “planning hash != implementation hash” failures.

5) Filesystem-first storage layout (vNext baseline)

Keep it simple, append-only, easy to debug:

runs/<run_id>/
  planning/
    artifacts/
      hash_spec.json
      preplan_triage.json
      clarification_questions.json
      clarification_resolutions.json
      plan_package_draft_v1.json
      plan_edits_v1.json
      plan_package_final_v1.json
      plan_freeze_record.json
      handoff_manifest.json
      config_snapshot.json
      repo_snapshot.json
      env_snapshot.json   (phase-gated; optional in kernel)
      human_feedback_bundle.json (derived)
      plan_diff_summary_v1.json  (derived)
    events.jsonl

  implementation/
    inputs/planning/  (copy-on-import minimal authoritative set)
      ...
    artifacts/
      work_graph.json
      execution_plan.json
      expectation_registry.json
      verification_plan.json
      evidence_index.json
      decision_record.json
      change_request_v1.json
      discrepancy_report.json
      ...
    jobs/
      specs/JOB-001.json
      run_records/JOB-001.jsonl
      results/JOB-001.json
    patchsets/
      TASK-010_v1.diff
      patch_manifest_TASK-010_v1.json
      apply_result_TASK-010_v1.json
    events.jsonl

“Copy-on-import” rule: copy the authoritative roots + referenced required inputs into implementation/inputs/planning/ so the impl run is self-contained and replayable.

6) End-to-end state machines (make transitions explicit)
6.1 Planning state machine

PLANNING_INTAKE → (PLANNING_TRIAGE) → (PLANNING_CLARIFY) → PLANNING_DRAFTING → (PLANNING_REVIEWING) → PLAN_FROZEN → HANDOFF_READY

6.2 Handoff state machine

HANDOFF_READY → HANDOFF_ACCEPTED | HANDOFF_REJECTED

6.3 Implementation per-milestone state machine

EXECUTION_PLANNED → EXECUTING → (BLOCKED_NEEDS_CHANGE ↔ planning) → MILESTONE_VERIFIED → RELEASE_FROZEN

7) Planning Orchestrator changes (Idea → Frozen Plan vN)
7.1 Add Pre-plan triage (cheap, high ROI)

Artifact: preplan_triage.json with:

ambiguities + default assumptions

candidate approaches

key decisions (and what they affect)

rough complexity band

recommended defaults

7.2 Clarify loop (optional but structured)

Artifacts:

clarification_questions.json: each has question_id, default_resolution, decision_impact

clarification_resolutions.json: each has question_id, resolution, impact_assessment

7.3 Draft plans must include stable IDs

In every plan_package_draft_vN.json and final:

Requirements: REQ-###

Work items: WI-### (map to REQs)

Checks/expectations: CHK-### (or EXP-###, pick one convention)

Milestones: MS-###

7.4 Review loop uses typed edits (canonical mutation mechanism)

Artifacts per review round:

plan_review_feedback_vN.json (normalized feedback items referencing IDs)

plan_edits_vN.json (typed ops referencing stable IDs)

plan_package_draft_v(N+1).json

plan_diff_summary_vN.json (derived deterministic)

7.5 Plan QC gates (minimum lint)

Produce:

plan_lint_report.json

traceability_matrix.json (REQ ↔ WI ↔ CHK coverage)

Minimum lint rules:

every milestone references ≥1 required check

every WI maps to ≥1 REQ

every CHK has an oracle mapping (oracle_ref) or resolvable entrypoint

scope + constraints exist (in_scope_paths, out_of_scope_paths, caps)

7.6 Freeze protocol (fix the “freeze latest” trap)

Freeze input must be explicit: selected_draft_ref.

Freeze produces:

plan_package_final_vN.json (copy of selected draft + meta.plan_status = "FROZEN")

plan_freeze_record.json (binding seal)

handoff_manifest.json (transport/index)

repo_snapshot.json (baseline pin)

config_snapshot.json

hash_spec.json (referenced + copied)

If review enabled:

plan_approval.json.approved_plan_hash must equal the hash of the selected draft (not “latest”)

8) Handoff protocol (Planning → Implementation)
8.1 handoff_manifest.json (tightened replacement for planning_handoff_bundle.json)

This is the only “bundle index” the implementation needs.

Must include:

hash_spec_version

artifacts[]: { ref, digest, schema_version, role: REQUIRED|OPTIONAL }

pointers: { plan_ref, freeze_record_ref, hash_spec_ref }

repo_acquisition_spec (embedded or referenced)

optional env_requirements_ref

handoff_digest (computed with this field empty)

8.2 Repo acquisition spec (mandatory for determinism)

Either embedded in manifest or separate repo_acquisition_spec.json:

Minimum:

repo identifier (URL or repo_id)

pinned git_commit

required git_tree_hash (strongly recommended to make required)

submodules policy (if used)

fetch/checkout rules (depth, remotes)

8.3 Handoff acceptance (implementation) — exact checks

Implementation must:

recompute handoff_digest and match

verify every REQUIRED artifact exists and digest matches

acquire repo baseline per spec (clone/fetch/checkout)

verify git_tree_hash

verify plan is FROZEN and plan_content_hash matches:

manifest pointer

freeze record

approval hash (if enabled)

write handoff_ack.json

If anything fails: write handoff_rejection.json with a typed reason code:

MISSING_ARTIFACT

HASH_MISMATCH

REPO_ACQUIRE_FAILED

TREE_HASH_MISMATCH

ENV_MISMATCH (if required)

SCHEMA_UNSUPPORTED

8.4 Overrides (optional but must be typed)

Replace boolean allow_repo_override with explicit override artifact:

handoff_override.json with:

override_type (ex: FAST_FORWARD_ALLOWED, TREE_HASH_MATCHES_AFTER_PATCH, BASELINE_PATCH_APPLIED)

expected vs actual baseline

rationale

new baseline snapshot ref

8.5 Idempotency rules

If handoff_ack.json exists:

same handoff_digest → resume

different handoff_digest → fail hard (prevents “accepted one plan then silently switched”)

9) Implementation Engine kernel (Execute frozen plan vN)
9.1 Explicit deterministic compilation step (must exist)

Inputs (hashed):

plan_package_final_vN.json (plan hash)

repo_snapshot.json

config_snapshot.json

env_snapshot.json (phase gated; optional in kernel but schema-ready)
Outputs (persisted):

work_graph.json

verification_plan.json

execution_plan.json (ordered runlist)

expectation_registry.json (can be standalone or embedded into verification plan)

Resume rule: if compiled artifacts exist and compiler_inputs_hashes match, reuse them exactly.

9.2 WorkGraph (durable operational DAG)

work_graph.json must contain:

meta.source_plan_hash

meta.compiler_version

meta.compiler_inputs_hashes[]

tasks[] each with:

task_id (stable)

source_plan_ids[] (REQ/WI/MS/CHK)

depends_on[]

target_paths[]

acceptance_check_ids[]

inputs[] / expected_outputs[]

budgets (optional caps/timeouts)

9.3 Jobs are first-class (JobSpec → JobRunRecord → JobResult)

Any long-running or verification-relevant work must be a Job.

Artifacts:

jobs/specs/JOB-###.json

jobs/run_records/JOB-###.jsonl (append-only attempts)

jobs/results/JOB-###.json

Minimum JobSpec:

job_id, job_type

task_id linkage

idempotency_key

inputs[] (artifact refs)

env_profile_ref or env_snapshot_hash

budgets (timeouts/retries)

expectation_ids[] satisfied by this job

Minimum JobResult:

job_id, status, attempt summary

outputs[] (artifact refs)

logs_refs[], metrics_refs[]

satisfies_expectations[]

9.4 Transactional patch apply (mandatory)

Artifacts per task patch:

patchsets/<TASK-ID>_vN.diff

patch_manifest_<TASK-ID>_vN.json (files touched, stats, linked plan IDs)

apply_result_<TASK-ID>_vN.json with:

pre_apply_tree_hash

post_apply_tree_hash (only if success)

per-file outcomes

integration status

Invariant: apply happens in isolated worktree/branch; failed apply must not corrupt baseline.

10) Replace “judge vibes” with the Verification Contract

This is the core upgrade across all reviewer plans.

10.1 Expectation Registry

expectation_registry.json defines each check/expectation:

expectation_id (CHK/EXP)

severity: MUST|SHOULD

oracle: { type: command|artifact_exists|metric_threshold|manual_review, ... }

required_env_class: LOCAL|CI|STAGING|CLUSTER|PROD

evidence_type: usually job_result

10.2 Verification Plan (compiled)

verification_plan.json defines:

which expectations apply for this milestone

what jobs/checks to run to satisfy them

optional baseline/regression policy (phase 2)

10.3 Evidence Index (live closure state)

evidence_index.json maps:

expectation → PENDING|PASS|FAIL + evidence_refs[]

10.4 Decision Record (rubric-based)

decision_record.json includes:

decision: PASS|FAIL|NEEDS_REPLAN|NEEDS_REVIEW

rubric version

per-expectation results and evidence pointers

10.5 Milestone freeze gate

Milestone is verifiable only when:

all MUST expectations are PASS with evidence refs

decision_record.decision == PASS

evidence refs resolve to real artifacts

11) Change control (how execution adapts to reality)
11.1 When implementation must emit a ChangeRequest

If any of these occur:

a MUST check is infeasible as specified

repo baseline differs materially / missing entrypoints

scope or budgets must expand

tool limitations prevent compliance

risk found requires changed constraints

11.2 change_request_vN.json (implementation → planning)

Must include:

source_plan_hash

typed reason enum: NEW_INFO|INFEASIBLE|DEP_CONFLICT|SCOPE_CHANGE|TOOL_LIMITATION|RISK_FOUND

blocked task/job/check IDs

evidence refs (job results/logs)

proposed_plan_edits_ref (typed edits, not prose)

impact summary (milestones/checks affected)

11.3 Planning response

Planning produces:

plan_package_final_v(N+1).json with:

meta.parent_plan_hash

meta.plan_version = N+1

meta.amendment_id

plan_migration_record.json (what changed + compatibility notes)

11.4 Continuation rules

Implementation must stop executing blocked tasks under vN once a blocking ChangeRequest is issued (unless explicitly configured to continue on unaffected tasks).

12) Resume and recovery modes (practical, but governed)

Implement explicit modes (even if only one is enabled initially):

RESUME_EXACT: strict replay; reuse compiled artifacts and existing job specs/results; no recompile.

REPAIR_WITHIN_PLAN: allow new job attempts or patch attempts within the same plan IDs, recording attempt lineage.

REPLAN_REQUIRED: emit ChangeRequest.

When mismatch is detected (inputs changed but you’re resuming):

write discrepancy_report.json

require ChangeRequest or explicit “recompile attempt” artifact (optional feature)

13) Events & observability (schema discipline, not “three events”)

Write events append-only to events.jsonl with:

schema_version

event_name

timestamp

correlation IDs: run_id, plan_hash, plus milestone_id/task_id/job_id when relevant

payload (structured)

Minimum event names:

planning: PLAN_DRAFTED, PLAN_REVIEWED, PLAN_FROZEN, HANDOFF_READY

handoff: HANDOFF_ACCEPTED, HANDOFF_REJECTED

implementation: EXECUTION_PLANNED, JOB_STARTED, JOB_FINISHED, PATCH_APPLIED, MILESTONE_VERIFIED, RELEASE_FROZEN, CHANGE_REQUESTED

14) Concrete implementation backlog for a junior engineer (do in this order)

This is the “one coherent plan” turned into implementable tickets.

Phase 0 — Correctness traps + foundation (must ship first)

Add hash_spec.json + shared hashing library

Implement canonical JSON + SHA-256 digest

Add unit tests: identical object → identical digest across runs

Add “excluded pointers” support for plan hashing

Fix freeze trap: freeze binds to selected_draft_ref

Update freeze code to require explicit selected draft path/hash

If approval exists, enforce approved_plan_hash == selected_draft_hash

Add integration test: “latest draft changes after selection” does not change frozen output

Introduce handoff_manifest.json + digest

Implement manifest writer + digest computation

Update planning to write it alongside existing planning_handoff_bundle.json (compat)

Add acceptance tests for digest determinism

Implement structured handoff acceptance

Implement verifier: required artifacts exist + digests match + plan is frozen

Implement handoff_ack.json / handoff_rejection.json

Enforce idempotency rules (same digest resumes; different fails)

Phase 1 — Kernel (minimum “very large project” capability)

Repo acquisition protocol + baseline verification

Implement repo fetch/checkout from repo_acquisition_spec

Verify git_tree_hash

Record repo_snapshot in handoff_ack

Deterministic compilers

Implement compiler framework:

compile_work_graph(plan, repo_ctx, config, env) -> work_graph.json

compile_verification_plan(...) -> verification_plan.json (+ expectation_registry.json)

compile_execution_plan(work_graph) -> execution_plan.json

Persist compiler inputs hashes; reuse on resume if identical

Job substrate

Implement JobSpec writer, JobRunRecord append-only logger, JobResult writer

Add idempotency_key logic and retry policy (minimal)

Convert existing validation steps to Jobs (even if executed locally)

Evidence-gated milestone completion

Implement evidence_index.json updater

Implement decision_record.json generator using a rubric (start simple: MUST all pass)

Gate milestone freeze on evidence + decision

Transactional patch apply

Apply diffs in isolated worktree/branch

Record pre/post tree hash + apply_result

Fail safely without corrupting baseline

ChangeRequest + plan lineage

Implement change_request_v1.json emitter when blocked

Implement planning support for producing v(N+1) plans with parent_plan_hash

Add plan_migration_record.json

Phase 2 — Scaling & parallelism (only when needed)

env_snapshot binding everywhere

scope/budget guardrails enforcement

baseline/regression reporting

multi-repo acquisition + manifests

surface leases + merge trains

Phase 3 — Optional

research profile artifacts as jobs/evidence

SQLite artifact graph index

CAS/Merkle/DB upgrades

15) Minimal schema skeletons (copy into /schemas/)

Below are “minimum viable fields.” Your implementation can extend them, but must not omit these.

hash_spec.json
{
  "schema_version": "hash_spec.v1",
  "hash_spec_version": "1",
  "hash_alg": "sha256",
  "canonical_json": "JCS-like-v1",
  "excluded_fields_by_pointer": [
    "/meta/plan_status",
    "/meta/created_at",
    "/meta/source_run_id",
    "/meta/run_id",
    "/meta/last_updated"
  ]
}
plan_package_final_vN.json
{
  "schema_version": "plan_package.vNext",
  "meta": {
    "plan_status": "FROZEN",
    "plan_version": 1,
    "parent_plan_hash": null,
    "amendment_id": null,
    "hash_spec_version": "1"
  },
  "problem": {
    "statement": "...",
    "intent_summary": "...",
    "non_goals": ["..."]
  },
  "scope": {
    "in_scope_paths": ["..."],
    "out_of_scope_paths": ["..."]
  },
  "constraints": {
    "caps": { "max_files_changed": 50, "max_loc_changed": 2000, "max_dep_changes": 2 }
  },
  "requirements": [{ "id": "REQ-001", "text": "..." }],
  "work_items": [{ "id": "WI-001", "title": "...", "maps_to_requirements": ["REQ-001"] }],
  "checks": [
    {
      "id": "CHK-UNIT",
      "severity": "MUST",
      "required_env_class": "LOCAL",
      "oracle": { "type": "command", "cmd": "pytest -q" }
    }
  ],
  "milestones": [{ "id": "MS-001", "required_check_ids": ["CHK-UNIT"] }]
}
plan_freeze_record.json
{
  "schema_version": "plan_freeze_record.v1",
  "hash_spec_version": "1",
  "selected_draft_ref": "artifacts/plan_package_draft_v3.json",
  "selected_draft_hash": "sha256:...",
  "plan_content_hash": "sha256:...",
  "refs": {
    "config_snapshot": { "ref": "artifacts/config_snapshot.json", "hash": "sha256:..." },
    "clarifications": null,
    "approval": null,
    "repo_snapshot": { "ref": "artifacts/repo_snapshot.json", "hash": "sha256:..." },
    "env_snapshot": null,
    "hash_spec": { "ref": "artifacts/hash_spec.json", "hash": "sha256:..." }
  }
}
handoff_manifest.json
{
  "schema_version": "handoff_manifest.v1",
  "hash_spec_version": "1",
  "pointers": {
    "plan_ref": "artifacts/plan_package_final_v1.json",
    "freeze_record_ref": "artifacts/plan_freeze_record.json",
    "hash_spec_ref": "artifacts/hash_spec.json"
  },
  "repo_acquisition_spec": {
    "repo_url": "git@...",
    "git_commit": "abc123...",
    "git_tree_hash": "deadbeef...",
    "submodules": "RECURSIVE"
  },
  "artifacts": [
    { "ref": "artifacts/plan_package_final_v1.json", "digest": "sha256:...", "schema_version": "plan_package.vNext", "role": "REQUIRED" },
    { "ref": "artifacts/plan_freeze_record.json", "digest": "sha256:...", "schema_version": "plan_freeze_record.v1", "role": "REQUIRED" }
  ],
  "handoff_digest": "sha256:..."
}
handoff_ack.json
{
  "schema_version": "handoff_ack.v1",
  "accepted_handoff_digest": "sha256:...",
  "accepted_plan_content_hash": "sha256:...",
  "acquired_repo": { "git_commit": "abc123...", "git_tree_hash": "deadbeef..." },
  "env_fingerprint": null,
  "imported_artifacts": [{ "ref": "...", "digest": "sha256:..." }]
}
handoff_rejection.json
{
  "schema_version": "handoff_rejection.v1",
  "reason_code": "HASH_MISMATCH",
  "expected": { "ref": "artifacts/plan_package_final_v1.json", "digest": "sha256:..." },
  "actual": { "digest": "sha256:..." },
  "remediation_hints": ["Re-freeze plan or regenerate manifest with correct hashes."]
}
work_graph.json
{
  "schema_version": "work_graph.v1",
  "meta": {
    "source_plan_hash": "sha256:...",
    "compiler_version": "work_compiler.v1",
    "compiler_inputs_hashes": ["sha256:plan", "sha256:repo", "sha256:config"]
  },
  "tasks": [
    {
      "task_id": "TASK-010",
      "title": "Implement WI-001",
      "source_plan_ids": ["WI-001", "REQ-001", "CHK-UNIT", "MS-001"],
      "depends_on": [],
      "target_paths": ["src/**", "tests/**"],
      "acceptance_check_ids": ["CHK-UNIT"],
      "budgets": { "max_files_changed": 20, "max_loc_changed": 800 }
    }
  ]
}
verification_plan.json + evidence_index.json + decision_record.json
{
  "schema_version": "verification_plan.v1",
  "meta": { "plan_hash": "sha256:...", "compiler_version": "verify_compiler.v1", "hash_spec_version": "1" },
  "milestone_id": "MS-001",
  "required_expectations": ["CHK-UNIT"],
  "jobs_to_run": ["JOB-041"]
}
{
  "schema_version": "evidence_index.v1",
  "meta": { "plan_hash": "sha256:...", "milestone_id": "MS-001" },
  "expectations": [
    { "expectation_id": "CHK-UNIT", "status": "PASS", "evidence_refs": [{ "type": "job_result", "ref": "jobs/results/JOB-041.json" }] }
  ]
}
{
  "schema_version": "decision_record.v1",
  "meta": { "plan_hash": "sha256:...", "milestone_id": "MS-001", "rubric_version": "1" },
  "decision": "PASS",
  "per_expectation": [{ "expectation_id": "CHK-UNIT", "status": "PASS", "evidence_refs": ["jobs/results/JOB-041.json"] }]
}
job_spec.json + job_result.json
{
  "schema_version": "job_spec.v1",
  "job_id": "JOB-041",
  "job_type": "validate",
  "task_id": "TASK-010",
  "idempotency_key": "plan:sha256.../task:TASK-010/job:validate/attempt:1",
  "inputs": [{ "type": "repo", "ref": "repo@deadbeef" }],
  "budgets": { "max_runtime_sec": 1800, "max_retries": 2 },
  "expectation_ids": ["CHK-UNIT"]
}
{
  "schema_version": "job_result.v1",
  "job_id": "JOB-041",
  "status": "SUCCEEDED",
  "outputs": [{ "type": "report", "ref": "artifacts/reports/pytest.xml" }],
  "logs_refs": ["artifacts/logs/JOB-041.log"],
  "satisfies_expectations": ["CHK-UNIT"]
}
change_request_v1.json
{
  "schema_version": "change_request.v1",
  "meta": { "impl_run_id": "impl_123", "source_plan_hash": "sha256:..." },
  "reason": { "code": "INFEASIBLE", "summary": "CHK-UNIT cannot run; pytest missing in env." },
  "blocked_entities": { "task_ids": ["TASK-010"], "job_ids": ["JOB-041"], "check_ids": ["CHK-UNIT"] },
  "evidence_refs": [{ "type": "job_result", "ref": "jobs/results/JOB-041.json" }],
  "proposed_plan_edits_ref": "artifacts/plan_edits_v7.json",
  "impact": { "milestones_affected": ["MS-001"], "checks_affected": ["CHK-UNIT"] }
}
16) Compatibility with your current CouncilOS artifacts

To avoid breaking everything at once:

Keep writing current:

plan_package_draft_vN.json

plan_package_final.json (but transition to plan_package_final_vN.json)

planning_handoff_bundle.json (legacy)

Add vNext artifacts in parallel:

handoff_manifest.json (new canonical transport/index)

handoff_ack.json / handoff_rejection.json

hash_spec.json

compiled execution artifacts + job substrate + evidence/decision artifacts

Then, once stable, deprecate planning_handoff_bundle.json by making it a derived view or a thin wrapper around handoff_manifest.json.

17) One-page “Definition of Done” for CouncilOS vNext kernel

A build is “vNext kernel complete” when:

hash_spec.json exists and is used for all hashes/digests.

Freeze binds to selected_draft_ref and approval hash matches it (if review enabled).

handoff_manifest.json includes required artifacts + repo acquisition spec + deterministic digest.

Implementation acceptance verifies artifact hashes + repo git_tree_hash, then writes handoff_ack.json.

Implementation compiles and persists work_graph.json, verification_plan.json, execution_plan.json deterministically and reuses them on resume.

Validations are Jobs with job_spec, job_run_record, job_result.

Milestone completion is evidence-gated with evidence_index.json + decision_record.json.

Patch apply is transactional with pre/post tree hashes in apply_result.json.

Blockers emit change_request_vN.json, and planning produces amended frozen plan v(N+1) with lineage.
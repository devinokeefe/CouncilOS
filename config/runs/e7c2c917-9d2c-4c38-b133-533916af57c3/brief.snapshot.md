Produce a frozen Plan Package (plus decision record + handoff bundle) to build a new CouncilOS subsystem: an “Implementation Council” that consumes a frozen Planning Council output and produces a working implementation (repo changes + tests + build/release artifacts).

CORE COUNCILOS INVARIANTS (non-negotiable)

Separation of powers: orchestrator (code) is sole authority over routing, budgets, iteration limits, termination, checkpoint/resume, tool permissions, selection logic, validator enforcement.

LLM roles are stochastic workers that output JSON-only artifacts matching per-stage schemas (no prose).

Durable audit spine: immutable run_manifest, append-only event_log, versioned artifact store; checkpoint every stage boundary.

Single source of truth: downstream consumers use ONLY canonical artifacts (not chat logs).

No silent scope expansion: Implementation Council may not change semantics of R#/AT#/O#/K# from input plan. Any deviation requires an explicit ChangeRequest artifact + decision record + policy-triggered escalation (HITL or reroute upstream).

SHARED WORKSPACE / LONG-HORIZON CONTEXT (required)
Design a shared, tool-accessible workspace so agents can persist progress across context-window limits and coordinate without relying on chat history. The design must:

Treat the shared workspace as an extension of the artifact system (versioned, auditable, checkpointed), not an informal scratchpad.

Support long-running work: agents must be able to “resume” from workspace artifacts deterministically (no reliance on ephemeral memory).

Separate canonical vs non-canonical content:

Canonical: typed/versioned JSON artifacts used by validators and downstream councils.

Non-canonical: optional working notes/summaries (still stored, versioned, and attributable, but explicitly non-authoritative).

Include a deterministic “Context Pack” mechanism:

Orchestrator compiles per-stage/per-agent context packs from the workspace (bounded size, explicit selection rules).

Agents must cite workspace artifacts via evidence pointers (below), not via freeform recollection.

Include workspace governance:

Permissions by stage (read/write) for workspace paths/namespaces.

Guardrails to prevent sensitive data persistence (secrets redaction + blocking).

Retention/compaction rules (e.g., periodic summarization artifacts) that preserve auditability (keep raw logs; summaries are additive).

Require explicit “state handoff” artifacts for each stage boundary, so execution can be paused/resumed without losing intent:

e.g., stage_handoff.json with current objectives, completed tasks, pending blockers, next actions, and pointers into work_plan / patchsets / test results.

EVIDENCE + TRACEABILITY (keep ONE standard)

Evidence pointers MUST use RFC 6901 JSON Pointer objects referencing versioned JSON artifacts:
{ artifact_ref, entity_id|null, json_pointer, note }.

Do NOT introduce a separate “code pointer standard” that bypasses JSON Pointer.

Instead, represent repo state, code changes, AND workspace state as JSON artifacts (e.g., repo_snapshot.json, patchset.json, code_trace_index.json, workspace_index.json) so evidence can be cited via JSON Pointer into those artifacts.

REPAIR MODEL (patch/diff-only)

JSON artifacts: repair via RFC 6902 JSON Patch arrays only.

Code changes: repair via diff-only artifacts chosen by you (e.g., unified diff embedded in JSON patchset objects, or structured file-op diffs), with deterministic orchestrator application + revalidation.

Workspace artifacts:

Canonical JSON workspace artifacts: repaired only via JSON Patch.

Non-canonical notes/summaries: append-only or patch-only per your design, but must remain auditable (no silent overwrites).

Orchestrator must select smallest passing repair (by deterministic rules) and must enforce forbidden-path rules + op caps.

No “rewrite the repo” loops: large refactors must be decomposed into bounded patchsets + staged validation.

TASK
Design the end-to-end Implementation Council architecture and workflow as a CouncilOS-grade system.

REQUIRED INPUT CONTRACT

Canonical input: plan_package_final.json (only), plus associated structured handoff bundle (decision_record, validator_reports, failure_mode_findings).

Define a required repo_context.json schema (or equivalent) for existing codebases: repo locator/path, base commit SHA, build/test entrypoints, toolchain constraints, dependency/network policy, protected paths, secrets handling constraints.

Define a required workspace_context.json schema describing the shared workspace backend and constraints (e.g., file store location, size limits, allowed formats, retention policy, namespaces, access control).

REQUIRED OUTPUT: “Implementation Freeze Bundle” (canonical contract)
Define the full freeze bundle with typed/versioned artifacts (JSON). Minimum:

implementation_run_manifest.json (models, prompts, schemas, toolchain versions, base/head commit, policy knobs, code hash)

implementation_event_log.jsonl (append-only: prompts/responses/tool calls/results/decisions/checkpoints)

work_plan.json (task graph with canonical IDs; tasks trace to R#/AT#/components/milestones)

patchsets/ (as JSON artifacts that embed diffs; deterministic apply order)

repo_snapshot.json (base_commit, head_commit, changed_files index, optionally per-file hashes)

test_results.json (structured execution results; maps to AT#)

quality_reports.json (lint/typecheck/security/deps/license checks as applicable)

trace_report.json (dense trace: R#→tasks→patchsets/commits→files/modules→tests→AT# results)

review_findings.json (taxonomy-mapped findings + closure table; evidence pointers)

release_bundle.json (artifact manifest: build outputs, package metadata, container digests if applicable)

decision_record.json (why key implementation decisions were made; what would change them)

Shared-workspace outputs (must be specified and included in freeze bundle):

workspace_index.json (canonical index of all workspace items produced, with types, versions, provenance, and pointers)

context_packs/ (JSON artifacts produced by orchestrator that capture the bounded context given to each role at each stage)

stage_handoffs/ (JSON artifacts capturing resumable state at each checkpoint boundary)

Optional non-authoritative renders (e.g., handoff_notes.md) clearly marked and indexed in workspace_index.json

COUNCIL STRUCTURE (roles)
Specify roles and per-role constraints, including:

Leader agents (planning/execution oversight; cannot override orchestrator)

Implementer agents (emit patchset artifacts + task updates + workspace-resident progress artifacts)

Integrator agent(s) (merge/resolve conflicts via patch-only protocol)

Reviewer agents (NON-WRITING: findings-only with evidence pointers)

Patch agents (patch-only outputs for fixes)

QA/Release/DevOps agents (as needed)

Security/Compliance reviewers (as needed)
Ensure writer/reviewer/judge separation and multi-vendor diversity.
Include explicit “Scribe/Summarizer” role(s) if you use them:

They may produce non-canonical summaries and context packs, but must not change canonical artifacts.

WORKFLOW (Generate → Validate → Judge → Freeze)
Design a staged state machine/graph optimized for real software delivery and long-horizon context:

Intake (load Plan Package + repo_context + workspace_context; initialize workspace_index)

Work breakdown + canonical task graph (IDs assigned deterministically in code)

Parallel implementation pods (code/test/docs/devops) producing patchsets + structured progress artifacts in workspace

Deterministic integration (patch queue; conflict handling + reroute/rebase stages)

Tool-run validation loops (build/test/lint/etc) with bounded iteration

Review (findings-only) → patch generation → deterministic patch selection

Regression revalidation

Stage handoff artifact emission at every checkpoint boundary (resumable state)

Freeze bundle + checkpoint index + finalized workspace_index + context_packs archive

TOOLS + SAFETY BY BOUNDARY
Implementation will use tools. Provide:

Tool permission matrix per stage (repo read/write, command execution, network, package managers, artifact IO, workspace read/write)

Secrets policy + redaction strategy (never store secrets in logs or workspace; define detection + blocking)

HITL triggers for high-risk operations (destructive commands, credential changes, deploys, network expansion)

Deterministic enforcement rules in orchestrator.

DETERMINISTIC VALIDATORS (closure gates)
Define code-owned validators that gate freezing, at minimum:

build/compile succeeds (if applicable)

lint/format/typecheck gates (if applicable)

tests + AT# execution criteria (every MUST R# has ≥1 AT# passing trace)

integrity (refs resolve, IDs unique, patchsets apply cleanly, forbidden paths untouched)

workspace integrity (workspace_index complete; context packs bounded; stage_handoffs present for each checkpoint; no forbidden data persisted)

risk closure (High/Critical risks mitigated or explicitly accepted with signoff)

reproducibility/trace completeness checks

FLAKY/NONDETERMINISTIC FAILURE POLICY
Explicitly design:

retry budgets and deterministic retry rules

quarantining or stabilization requirements

escalation paths (patch vs reroute vs HITL vs ChangeRequest)

CHANGE CONTROL
Define ChangeRequest artifact schema + deterministic rules for:

when it is allowed

who can propose (agents) vs who can approve (orchestrator policy/HITL)

reroute to Planning Council when plan semantics must change

MODEL PORTFOLIO
Specify default multi-vendor model routing:

writers vs reviewers vs judges vs patchers (keep judges independent from writers)

parameter gotchas and enforcement notes (as in CouncilOS HQ defaults)

OUTPUT FORMAT
Output only the Planning Council’s Implementation-Council Plan Package in JSON (plus its schemas/contract artifacts as JSON). No prose. Proceed.
# Implementation Council — Final Plan Package (Candidate B2B, Branch B2)

## Meta
- **plan_id:** 57c9e177-5056-4c63-adcb-900b2f6984dc
- **version:** vFinal
- **created_at:** 2026-02-15T21:19:50.353157Z
- **source_run_id:** e5e8f9e4-14bb-4c6f-9781-032fcf85734c
- **schema_version:** 1.0.0
- **candidate_id:** B2B
- **branch_id:** B2
- **plan_status:** FROZEN

---

## Project Capsule
### Brief
Produce a frozen Plan Package (plus decision record + handoff bundle) to build a new CouncilOS subsystem: an “Implementation Council” that consumes a frozen Planning Council output (plan_package_final.json) and produces a working implementation (repo changes + tests + build/release artifacts) as a canonical “Implementation Freeze Bundle” with a governed shared workspace for long-horizon execution.

### Problem Statement
CouncilOS needs a CouncilOS-grade Implementation Council architecture and workflow that can deterministically turn a frozen plan into audited, reproducible repository changes and release artifacts, while preserving separation of powers, preventing silent scope expansion, maintaining an immutable audit spine, and enabling long-running work via a versioned shared workspace with deterministic context packs and resumable stage handoffs.

### Goals
1. Define the end-to-end Implementation Council architecture, roles, and staged workflow (Generate → Validate → Judge → Freeze) suitable for real software delivery and long-horizon context.
2. Define the canonical input contract: accept plan_package_final.json only (plus associated structured handoff bundle) and require repo_context.json and workspace_context.json schemas.
3. Define the canonical output contract: an “Implementation Freeze Bundle” with typed/versioned JSON artifacts (run manifest, event log, work plan, patchsets, repo snapshots, test/quality/trace/review/release/decision artifacts) plus required shared-workspace outputs (workspace_index, context_packs, stage_handoffs).
4. Design a shared, tool-accessible workspace as an extension of the artifact system: versioned, auditable, checkpointed; with canonical vs non-canonical separation; deterministic orchestrator-compiled context packs; and explicit stage handoff artifacts for pause/resume.
5. Standardize evidence/traceability using one evidence pointer standard: RFC 6901 JSON Pointer objects into versioned JSON artifacts; represent repo/code/workspace state as JSON artifacts to support evidence pointers.
6. Specify repair/iteration model: JSON artifacts repaired via RFC 6902 JSON Patch only; code changes via deterministic diff-only patchset artifacts with bounded apply/validate loops; orchestrator selects smallest passing repair via deterministic rules.
7. Specify tool permission matrix per stage, secrets redaction/blocking strategy, HITL triggers, and deterministic enforcement rules owned by the orchestrator.
8. Define deterministic, code-owned validators (closure gates) required to freeze (build/test/lint/typecheck, integrity, workspace integrity, risk closure, reproducibility, trace completeness).
9. Define flaky/nondeterministic failure policy: deterministic retry rules/budgets, quarantine/stabilization, and escalation paths (patch vs reroute vs HITL vs ChangeRequest).
10. Define change control: ChangeRequest artifact schema and deterministic rules for proposing/approving changes; reroute upstream when plan semantics must change.
11. Specify default multi-vendor model portfolio/routing for writers vs reviewers vs judges vs patchers, preserving writer/reviewer/judge separation and independence.

### Non-Goals
- Implementing the actual subsystem code in this stage; this capsule is for producing the Implementation Council plan package and contracts, not executing repo changes.
- Changing the semantics of upstream plan entities (R#/AT#/O#/K#) without an explicit ChangeRequest and required escalation.
- Using chat logs as a source of truth; downstream consumption must use only canonical artifacts.
- Introducing alternate evidence standards (e.g., code pointers) that bypass JSON Pointer into versioned JSON artifacts.
- Allowing non-auditable workspace behavior (silent overwrites, unversioned scratchpads, or reliance on agent ephemeral memory).
- Modifying existing CouncilOS orchestrator code or Planning Council subsystems (beyond defining interfaces/contracts they must satisfy).
- Designing or implementing storage infrastructure/backends (only interface contracts, governance rules, and constraints).
- Defining general-purpose CI/CD pipelines unrelated to CouncilOS multi-agent workflows.

### Constraints
- **CNS1 (policy):** Separation of powers is non-negotiable: orchestrator (code) is sole authority over routing, budgets, iteration limits, termination, checkpoint/resume, tool permissions, selection logic, and validator enforcement; agents cannot override orchestrator decisions.
- **CNS2 (policy):** LLM roles are stochastic workers that must output JSON-only artifacts matching per-stage schemas; no prose in canonical outputs.
- **CNS3 (tech):** Durable audit spine required: immutable run_manifest, append-only event_log, versioned artifact store; checkpoint at every stage boundary.
- **CNS4 (policy):** Single source of truth: downstream councils/validators consume only canonical artifacts (not chat logs).
- **CNS5 (policy):** No silent scope expansion: Implementation Council may not change semantics of R#/AT#/O#/K# from input plan; deviations require ChangeRequest artifact + decision record + policy-triggered escalation (HITL or reroute upstream).
- **CNS6 (tech):** Evidence pointers must use a single standard: RFC 6901 JSON Pointer objects referencing versioned JSON artifacts {artifact_ref, entity_id|null, json_pointer, note}; repo/code/workspace state must be representable as JSON artifacts for citation.
- **CNS7 (tech):** Repair model constraints: JSON artifacts repaired via RFC 6902 JSON Patch arrays only; code changes via diff-only patchset artifacts with deterministic application and revalidation; orchestrator enforces forbidden-path rules and operation caps.
- **CNS8 (tech):** Shared workspace must be tool-accessible, versioned, auditable, checkpointed; must separate canonical vs non-canonical content; must support deterministic orchestrator-built bounded context packs; and must require explicit stage_handoff artifacts at each checkpoint.
- **CNS9 (compliance):** Secrets policy: never store secrets in logs or workspace; require detection, redaction, and blocking guardrails; enforce by orchestrator policy and validators.
- **CNS10 (tech):** Freeze bundle must include, at minimum, the specified canonical artifacts: implementation_run_manifest.json, implementation_event_log.jsonl, work_plan.json, patchsets (diff-embedded JSON), repo_snapshot.json, test_results.json, quality_reports.json, trace_report.json, review_findings.json, release_bundle.json, decision_record.json, plus workspace_index.json, context_packs/, stage_handoffs/.
- **CNS12 (policy):** Writer/reviewer/judge separation and multi-vendor diversity required in council structure; reviewers are findings-only and do not emit writing/patch artifacts.
- **CNS13 (policy):** No silent scope expansion in code changes: avoid “rewrite the repo” loops; large refactors must be decomposed into bounded patchsets with staged validation and deterministic integration.

### Assumptions
- **A1:** A CouncilOS orchestrator implementation exists (or will be provided) that can enforce stage schemas, tool permissions, deterministic routing/selection, checkpoints, and validator gates as described. (impact: high; confidence: medium; needs_confirmation: true)
- **A2:** A versioned artifact store is available that can immutably store run manifests, append-only event logs, and versioned JSON artifacts referenced by artifact_ref, and can support JSON Pointer addressing for evidence. (impact: high; confidence: medium; needs_confirmation: true)
- **A3:** The implementation environment will allow deterministic application of diff-only patchsets to a checked-out repository state and can produce repo_snapshot.json with base/head commits and changed-files indices. (impact: high; confidence: medium; needs_confirmation: true)
- **A4:** Validator execution (build/test/lint/typecheck/security/license as applicable) can be invoked via declared entrypoints in repo_context.json and can emit structured results to JSON artifacts. (impact: high; confidence: medium; needs_confirmation: true)
- **A5:** A multi-vendor model portfolio is available to support separation between writers, reviewers, judges, and patchers, with routing controlled by orchestrator policy. (impact: medium; confidence: low; needs_confirmation: true)
- **A6:** Workspace backend can support namespacing, read/write access control by stage/role, retention/compaction policies, and append-only handling for non-canonical notes while remaining auditable. (impact: high; confidence: low; needs_confirmation: true)
- **A7:** Planning Council output (plan_package_final.json) is well-formed, frozen, and contains complete R#/AT#/O#/K# specifications sufficient for downstream traceability and gating. (impact: high; confidence: medium; needs_confirmation: true)
- **A8:** Tool execution can be sandboxed with granular permission controls per stage (filesystem/repo, command execution, network, package managers, artifact IO) consistent with orchestrator policy enforcement. (impact: high; confidence: low; needs_confirmation: true)
- **A9:** Test execution results can be deterministically mapped back to AT# acceptance test IDs from planning artifacts (via naming/metadata conventions captured in schemas). (impact: high; confidence: medium; needs_confirmation: true)
- **A10:** Diff-based code change artifacts can be applied deterministically across merge conflicts and rebase scenarios using orchestrator-owned integration logic and bounded reroute/escalation paths when conflicts exceed limits. (impact: high; confidence: low; needs_confirmation: true)
- **A11:** Context pack size bounds and deterministic selection rules can be defined that balance completeness with model context window constraints for the chosen model portfolio. (impact: medium; confidence: low; needs_confirmation: true)
- **A12:** Flaky test/tool nondeterminism can be detected and handled using deterministic retry/quarantine policies with acceptable error rates for the targeted repos. (impact: medium; confidence: low; needs_confirmation: true)

### Open Questions
- **Q1 (blocking):** What are the target repository ecosystems to support first (languages, build systems, CI runners), and which validations are mandatory vs optional by default?
- **Q2 (blocking):** What concrete workspace backend(s) are permitted (e.g., filesystem path, object store, database), and what are the hard constraints (size limits, latency, retention, encryption, tenancy)?
- **Q3 (blocking):** What is the canonical definition/format of artifact_ref (naming scheme, versioning scheme, addressing) used by evidence pointers and workspace_index entries?
- **Q4 (blocking):** What are the organization’s forbidden paths/protected paths patterns for repositories, and what constitutes a high-risk operation requiring HITL (e.g., destructive commands, deploys, credential changes, network expansion)?
- **Q5 (blocking):** What is the default model routing policy (which vendors/models for writer/reviewer/judge/patch roles), and what parameters must be fixed for determinism/reproducibility (temperature, max tokens, tool-call settings)?
- **Q6:** What is the required ChangeRequest approval authority and escalation policy (pure orchestrator policy vs mandatory HITL for certain classes), and where should reroute upstream land (Planning Council stage identifiers)?
- **Q7 (blocking):** What specific diff format should be used for code change artifacts in patchsets (e.g., unified diff vs structured file-op diffs), and what normalization rules are required for deterministic application?
- **Q8 (blocking):** How should the orchestrator deterministically select the “smallest passing repair” when multiple candidate patch artifacts are available (ordering, tie-breakers, scoring, op caps)?
- **Q9 (blocking):** How should conflict resolution work when parallel implementation pods produce overlapping/competing patchsets (merge strategy, reroute/rebase stages, escalation thresholds)?
- **Q10 (blocking):** What are the retry budget and backoff strategy requirements for flaky tests and nondeterministic tool failures, and how are retries recorded in the audit spine?
- **Q11 (blocking):** How should dependencies requiring network access and package-manager operations be handled under tool/network policies (offline mirrors, allowlists, lockfile rules, reproducibility constraints)?
- **Q12 (blocking):** What are the specific criteria for when a ChangeRequest must be escalated (HITL and/or reroute upstream) vs handled fully within Implementation Council without changing plan semantics?
- **Q13 (blocking):** How should the system handle incremental implementation when the full work plan cannot be completed within token/time budgets while preserving determinism and traceability (partial freezes vs checkpoints only)?
- **Q14 (blocking):** What is the required schema for repo_context.json protected paths/forbidden paths and how exactly are they enforced during patch application and tool execution?
- **Q15 (blocking):** How should context packs be bounded and selected when workspace state grows beyond context window limits (selection rules, prioritization, summarization artifacts, and validation of bounds)?
- **Q16:** What is the maximum workspace size and retention period for non-canonical artifacts before compaction/summarization is required, and what auditability guarantees must compaction preserve?
- **Q17:** What specific security/compliance checks are required in quality_reports.json (SAST/DAST, dependency/license, secrets scans, SBOM), and what tooling/standards must be supported?

### Success Metrics
- All required input/output contracts are fully specified as typed/versioned JSON schemas (repo_context.json, workspace_context.json, ChangeRequest schema, and full Implementation Freeze Bundle artifact list), and pass schema validation in orchestrator tests (target: 100% schema-valid artifacts).
- Deterministic workflow definition exists as a staged state machine/graph with explicit checkpoint boundaries and required stage_handoffs; validators confirm a stage_handoff artifact is present for every checkpoint (target: 100% coverage).
- Evidence/traceability standard is singular and enforced: all findings/decisions/trace links reference artifacts via RFC 6901 JSON Pointer objects; validator detects zero non-compliant evidence references (target: 0 violations).
- Repair model is enforceable: all canonical JSON updates are expressible as RFC 6902 JSON Patch; all code changes are diff-only patchsets with deterministic apply order; validator confirms patchsets apply cleanly on base_commit with no forbidden-path writes (target: 100% clean apply; 0 forbidden writes).
- Workspace governance is enforceable: workspace_index.json accounts for all workspace items with types/versions/provenance; context packs are bounded and deterministically assembled; validator confirms bounds and completeness (target: 100% indexed items; 0 out-of-bounds packs).
- Security policy is implemented in design: secrets redaction/blocking rules specified and validated; event log/workspace scans find no secrets persisted (target: 0 detected secrets).
- Freeze gates are complete: specified deterministic validators cover build/test/lint/typecheck (as applicable), integrity, workspace integrity, risk closure, reproducibility, and trace completeness; a freeze decision is allowed only when all gates pass (target: 0 freezes with failing gates in test scenarios).
- Council structure and role separation are explicit and testable (target: required roles present with enforced write/review/judge separation and multi-vendor routing).
- Workflow coverage meets minimum stage expectations (target: intake through freeze includes parallel implementation, deterministic integration, validation loops, review+patch cycles, regression revalidation, and checkpoint handoffs at each boundary).
- Validator suite coverage meets minimum gate expectations (target: build/compile, lint/format/typecheck, tests+AT# trace, integrity, workspace integrity, risk closure, reproducibility/trace completeness all implemented and gating).

### Stakeholders
- CouncilOS Orchestrator (code-owned policy/validation authority)
- Planning Council (producer of plan_package_final.json and semantics of R#/AT#/O#/K#)
- Implementation Council roles: leader/implementer/integrator/reviewer/judge/patch/QA-release/security/compliance/scribe
- Platform/DevOps (tooling, build/release environments, artifact storage)
- Security/Compliance (secrets policy, license/dependency/security checks, HITL triggers)
- Artifact schema and validation framework maintainers
- Workspace backend infrastructure team
- Multi-vendor LLM integration team
- End users requiring deterministic software implementation workflows

### Glossary
- **Implementation Council:** A CouncilOS subsystem that consumes a frozen planning output (plan_package_final.json) and produces audited, reproducible repository changes and release artifacts under orchestrator-controlled policies and validators.
- **Implementation Freeze Bundle:** The canonical, typed/versioned set of JSON artifacts emitted at freeze, including run manifest, event log, work plan, patchsets, repo snapshot, test/quality/trace/review/release artifacts, decision record, and required workspace artifacts (workspace_index, context_packs, stage_handoffs).
- **Shared Workspace:** A tool-accessible, versioned and auditable storage area treated as an extension of the artifact system, containing canonical JSON artifacts and explicitly non-canonical notes/summaries, governed by permissions, retention, and checkpoint/resume handoffs.
- **Evidence Pointer:** An RFC 6901 JSON Pointer object {artifact_ref, entity_id|null, json_pointer, note} referencing a versioned JSON artifact location; used as the sole standard for traceability across decisions, findings, and state representations.
- **Stage Handoff:** A required JSON artifact emitted at each checkpoint boundary capturing resumable state: objectives, completed work, pending blockers, next actions, and pointers into canonical artifacts (work plan, patchsets, validation outputs).
- **ChangeRequest:** A canonical artifact proposed when plan semantics would need to change; requires explicit decisioning and policy-triggered escalation (HITL and/or reroute upstream) before any semantic deviation is allowed.
- **Orchestrator:** Deterministic code component with sole authority over routing, budgets, iteration limits, termination, checkpoint/resume, tool permissions, selection logic, and validator enforcement.
- **Context Pack:** A bounded, deterministically-compiled set of workspace artifacts provided to a role at a stage, selected by explicit rules and cited via evidence pointers.
- **Patchset:** A JSON artifact embedding code diffs with deterministic apply order for repo modifications; used as the sole mechanism for code repair/changes in the workflow.
- **Validator:** A code-owned deterministic gate that checks artifact integrity/completeness and quality criteria to allow stage transitions or freeze.

---

## Executive Summary
Candidate B2B implements the Implementation Council using architecture option O2 (task-parallel fan-out/fan-in with deterministic integration) delivered via a hybrid milestone strategy. Phase 1 establishes the foundation—Global Orchestrator (C11), Intake & DAG Work Planner (C12), Workspace Manager with namespaced partitions (C22), and a single-lane execution path—to validate intake, workspace governance, and the audit spine against AT1–AT3, AT4 (single-lane subset), AT5, AT8, AT9, AT19, and AT20. Phase 2 adds the Task Lane Orchestrator (C13) and per-lane Writer/Validator/Reviewer/Patcher pods (C14–C17) running two parallel lanes on a test repository, gating on AT12–AT14 and AT22. Phase 3 introduces the Integration Merge Engine (C18), Integration Validator/Reviewer/Patcher (C19–C21), and a deterministic merge-ordering algorithm that resolves alignment warning AL006, gating on AT15, AT10, and AT16. Phase 4 completes the system with Judge Pod (C23) and Freeze Assembler (C24), gating on AT5–AT7, AT17–AT18, AT21, and AT23, followed by full AT-suite validation at final Freeze. This candidate is recommended when wall-clock throughput is a priority and the target repository has low inter-module coupling. The primary elevated risk is K12 (merge conflicts from parallel lanes), mitigated by Phase 3's deterministic merge ordering and explicit conflict escalation. High HITL strictness is retained throughout due to parallel complexity and open questions Q7–Q9.

---

## Requirements (R#)
- **R1 (MUST):** The Implementation Council subsystem SHALL accept exactly one frozen plan_package_final.json (plus its associated structured handoff bundle) as its sole canonical planning input, and SHALL reject chat logs or any non-canonical/unapproved upstream artifacts as authoritative sources of plan semantics.
  - Rationale: Enforces single source of truth and prevents downstream semantic drift driven by non-canonical inputs.
- **R2 (MUST):** The Implementation Council subsystem SHALL define and require a typed, versioned repo_context.json schema; orchestrator validators SHALL fail the run if repo_context.json is missing or schema-invalid, and repo_context.json SHALL be required before any code-generation stage begins and SHALL declare at minimum: repository root, language/build-system entrypoints, validator commands/entrypoints, protected/forbidden path patterns, and branch/commit metadata.
  - Rationale: Standardizes repository execution, validates prerequisites deterministically, and enables enforcement of path protections and validator invocation.
- **R3 (MUST):** The Implementation Council subsystem SHALL define and require a typed, versioned workspace_context.json schema; orchestrator validators SHALL fail the run if workspace_context.json is missing or schema-invalid, and workspace_context.json SHALL be required before any workspace-writing stage begins and SHALL declare at minimum: workspace root, namespace/layout rules, canonical vs non-canonical partition rules, retention/compaction policies, and access-control declarations.
  - Rationale: Establishes enforceable workspace governance prerequisites for long-horizon execution, checkpointing, and deterministic context delivery.
- **R4 (MUST):** The Implementation Council workflow SHALL be defined as a deterministic staged state machine with at minimum the stages: Intake → Generate → Validate → Review → Judge → Freeze, and SHALL include an explicit checkpoint boundary at every stage transition.
  - Rationale: Defines the required deterministic end-to-end staged workflow and ensures resumability and gating structure at each transition.
- **R5 (MUST):** At every checkpoint boundary (i.e., each stage transition), the system SHALL emit a stage_handoff JSON artifact conforming to a typed/versioned schema, and orchestrator validators SHALL fail the run if any required stage_handoff is absent.
  - Rationale: Enforces pause/resume via explicit, machine-verifiable handoffs at every checkpoint boundary.
- **R6 (MUST):** Each stage_handoff artifact SHALL capture resumable state sufficient for cold-start resume, including: stage objectives, completed work references, pending blockers, next actions, and evidence pointers into canonical artifacts (including work_plan, patchsets, and validation outputs).
  - Rationale: Ensures stage handoffs are operationally sufficient for deterministic resume without relying on transient agent memory or chat.
- **R7 (MUST):** At Freeze, the system SHALL produce an Implementation Freeze Bundle that includes, at minimum, the following typed/versioned artifacts and directories: implementation_run_manifest.json, implementation_event_log.jsonl, work_plan.json, patchsets (diff-embedded JSON), repo_snapshot.json, test_results.json, quality_reports.json, trace_report.json, review_findings.json, release_bundle.json, decision_record.json, workspace_index.json, context_packs/, and stage_handoffs/.
  - Rationale: Defines the minimum canonical freeze deliverables required for audited downstream consumption and reproducible implementation.
- **R8 (MUST):** The audit spine SHALL include an implementation_run_manifest that is immutable once created and an implementation_event_log that is append-only; the artifact store SHALL version every artifact, and the orchestrator SHALL checkpoint canonical artifacts at every stage boundary.
  - Rationale: Provides durable, immutable run-level accountability and replayable state transitions with versioned artifacts and mandatory checkpoints.
- **R9 (MUST):** All LLM agent outputs in canonical stages SHALL be JSON-only artifacts conforming to per-stage schemas; the orchestrator SHALL reject any artifact containing prose/markdown/unstructured text or failing schema validation.
  - Rationale: Ensures deterministic machine-consumable artifacts suitable for validator enforcement and downstream councils.
- **R10 (MUST):** All evidence pointers, traceability links, findings references, and decision citations in canonical artifacts SHALL use a single standard: RFC 6901 JSON Pointer objects with the structure {artifact_ref, entity_id|null, json_pointer, note} referencing versioned JSON artifacts; the orchestrator SHALL fail the run if any non-compliant or alternative evidence format is present.
  - Rationale: Enforces a single, validator-checkable traceability mechanism across the system.
- **R11 (MUST):** Repository, code, and workspace state that must be cited for traceability SHALL be represented as versioned JSON artifacts addressable via RFC 6901 JSON Pointers, and the system SHALL forbid evidence standards that bypass JSON-artifact citation.
  - Rationale: Makes traceability uniformly machine-verifiable by ensuring citeable state is always reachable via the evidence pointer standard.
- **R12 (MUST):** Repairs to canonical JSON artifacts SHALL be expressed exclusively as RFC 6902 JSON Patch arrays, and orchestrator validators SHALL reject any canonical artifact modification not expressed as JSON Patch.
  - Rationale: Constrains artifact mutation to a deterministic, auditable, and validator-checkable repair mechanism.
- **R13 (MUST):** All code changes produced by the Implementation Council SHALL be emitted exclusively as diff-only patchset JSON artifacts with a deterministic apply order; the orchestrator SHALL enforce bounded apply/validate loops and SHALL fail the run if patchsets include non-diff code mutation mechanisms.
  - Rationale: Prevents non-auditable edits and supports reproducible application and validation cycles controlled by orchestrator policy.
- **R14 (MUST):** During patchset application, the orchestrator SHALL enforce forbidden/protected-path rules and operation caps: no patchset may write to paths declared forbidden/protected in repo_context.json, and total patch operations per repair cycle SHALL be capped by orchestrator policy.
  - Rationale: Ensures policy-enforced safety boundaries and bounded change application under deterministic orchestrator control.
- **R15 (MUST):** When multiple candidate repair patches pass validation, the orchestrator SHALL select the smallest passing repair using deterministic rules, with tie-breakers in order: (1) fewest operations, (2) fewest bytes changed, (3) first-generated tiebreaker.
  - Rationale: Defines deterministic selection logic to prevent non-reproducible or subjective choice among multiple passing repairs.
- **R16 (MUST):** The Implementation Council SHALL NOT modify the semantics of upstream R#/AT#/O#/K# entities from the frozen input plan; any required semantic deviation SHALL be expressed as a ChangeRequest artifact and SHALL require a decision record and policy-triggered escalation (HITL and/or reroute upstream) before the deviation is applied.
  - Rationale: Prevents silent scope expansion and preserves upstream governance by requiring explicit change control for semantic deviations.
- **R17 (MUST):** The shared workspace SHALL be tool-accessible, versioned, auditable, and checkpointed; it SHALL enforce a strict canonical vs non-canonical partition where canonical artifacts are immutable after freeze and non-canonical notes are append-only with provenance metadata; and the orchestrator SHALL build bounded context packs deterministically for each stage/role invocation using explicit selection rules, with context pack contents cited via evidence pointers.
  - Rationale: Enables governed long-horizon execution with auditable workspace state, deterministic context delivery, and clear canonical/non-canonical behavior.
- **R18 (MUST):** workspace_index.json SHALL enumerate all workspace items with type, version, and provenance, and orchestrator validators SHALL fail the run if any workspace item is not indexed or if any context_pack exceeds configured bounds.
  - Rationale: Provides complete, enforceable workspace accounting and bounded context-pack delivery for deterministic consumption.
- **R19 (MUST):** The orchestrator SHALL define a tool permission matrix per stage specifying allowed/denied capabilities (including filesystem read/write scopes, command execution, network access, package managers, and artifact I/O) and SHALL enforce it deterministically; tool invocations outside declared permissions SHALL be blocked and recorded in the audit log.
  - Rationale: Enables deterministic enforcement of execution constraints and produces auditable records of denied/violating tool actions.
- **R20 (MUST):** The system SHALL implement and enforce a secrets policy such that secrets are never stored in event logs, workspace artifacts, or any canonical/non-canonical artifacts; the orchestrator SHALL enforce detection, redaction, and blocking guardrails validated before any artifact is persisted, and validators SHALL fail any run where secrets are detected in canonical artifacts, event logs, or workspace outputs.
  - Rationale: Meets compliance constraints by preventing persistence of sensitive data and making enforcement validator-checkable.
- **R21 (MUST):** Freeze SHALL be permitted only when all deterministic, code-owned closure gates pass, including: build/test/lint/typecheck (as applicable per repo_context), artifact integrity, workspace integrity, risk closure, reproducibility, and trace completeness validators.
  - Rationale: Prevents freezes with unmet quality/integrity/traceability conditions and makes release decisions validator-gated.
- **R22 (MUST):** The council structure SHALL enforce writer/reviewer/judge separation and multi-vendor diversity: writers produce code/artifact outputs, reviewers produce findings-only artifacts (no writing or patches), and judges produce accept/reject decisions; no single role may perform more than one of these functions within the same stage; orchestrator routing SHALL enforce default multi-vendor separation across writer/reviewer/judge roles.
  - Rationale: Prevents conflicts of interest and preserves independent review and judgment via enforceable role separation and routing diversity.
- **R23 (MUST):** Flaky and nondeterministic failures SHALL be handled using deterministic retry rules with bounded budgets (including max retries per validator per stage); failures exceeding retry budgets SHALL be quarantined and escalated via a deterministic path (patch → reroute → HITL → ChangeRequest), and all retries and outcomes SHALL be recorded in the implementation_event_log.
  - Rationale: Establishes deterministic handling for nondeterminism while preserving auditability and bounded iteration behavior.
- **R24 (SHOULD):** Large code refactors SHOULD be decomposed into bounded patchsets with staged validation and deterministic integration order; the orchestrator SHOULD reject any single patchset exceeding a configurable operation/size cap and require decomposition.
  - Rationale: Reduces risk of “rewrite the repo” loops and improves deterministic validation/integration by bounding change units.
- **R25 (SHOULD):** test_results.json SHOULD include a deterministic mapping from each test outcome back to the corresponding AT# acceptance test ID from the planning artifacts to enable automated trace completeness validation.
  - Rationale: Supports validator-checkable end-to-end traceability from plan acceptance tests to implementation validation results.

---

## Acceptance Tests (AT#)
- **AT1 → [R1] (system)**
  - Procedure: Attempt to initiate an Implementation Council run with inputs: (a) a valid plan_package_final.json plus its structured handoff bundle; (b) a chat log reference as the only planning input; (c) an unapproved non-canonical artifact as planning input. Record orchestrator accept/reject results.
  - Pass criteria: Run (a) is accepted; runs (b) and (c) are rejected with structured errors indicating non-canonical planning inputs are not authoritative.
- **AT2 → [R2] (system)**
  - Procedure: Start a run without repo_context.json; then start a run with repo_context.json that violates schema; then start a run with schema-valid repo_context.json containing required fields (repo root, build entrypoints, validator entrypoints, protected/forbidden paths, branch/commit metadata).
  - Pass criteria: Runs missing or schema-invalid repo_context.json are rejected before any code-generation stage; run with schema-valid repo_context.json is accepted and records validation success.
- **AT3 → [R3] (system)**
  - Procedure: Start a run without workspace_context.json; then start a run with schema-invalid workspace_context.json; then start a run with schema-valid workspace_context.json containing required fields (workspace root, namespace/layout rules, canonical/non-canonical partition rules, retention/compaction, access-control).
  - Pass criteria: Runs missing or schema-invalid workspace_context.json are rejected before any workspace-writing stage; run with schema-valid workspace_context.json is accepted and records validation success.
- **AT4 → [R4] (system)**
  - Procedure: Execute a run through stages and extract the recorded stage sequence and checkpoint boundaries from implementation_event_log.jsonl and stage_handoff artifacts.
  - Pass criteria: Stage sequence includes at minimum Intake → Generate → Validate → Review → Judge → Freeze and a checkpoint boundary is recorded at every stage transition.
- **AT5 → [R5] (system)**
  - Procedure: Complete a run with all stage transitions and list stage_handoff artifacts present in stage_handoffs/ and workspace_index.json.
  - Pass criteria: A stage_handoff artifact exists for every stage transition; validator fails the run if any required stage_handoff is missing.
- **AT6 → [R6] (system)**
  - Procedure: Inspect each stage_handoff artifact for required fields: objectives, completed work references, pending blockers, next actions, and evidence pointers into work_plan, patchsets, and validation outputs.
  - Pass criteria: Every stage_handoff contains all required fields and valid evidence pointers to the referenced canonical artifacts.
- **AT7 → [R7] (system)**
  - Procedure: At Freeze, enumerate artifacts in the Implementation Freeze Bundle and verify presence of all required items and directories.
  - Pass criteria: Freeze bundle contains all minimum artifacts: implementation_run_manifest.json, implementation_event_log.jsonl, work_plan.json, patchsets (diff-embedded JSON), repo_snapshot.json, test_results.json, quality_reports.json, trace_report.json, review_findings.json, release_bundle.json, decision_record.json, workspace_index.json, context_packs/, stage_handoffs/.
- **AT8 → [R8] (system)**
  - Procedure: Attempt to modify implementation_run_manifest.json after creation and attempt to delete/overwrite past entries in implementation_event_log.jsonl; check artifact store versions and event_log ordering.
  - Pass criteria: Run manifest is immutable (modification rejected or versioned as new artifact without altering original); event log is append-only with monotonic ordering; artifacts are versioned and checkpoints recorded at each stage boundary.
- **AT9 → [R9] (system)**
  - Procedure: Submit a canonical stage artifact containing prose/markdown; submit another artifact that violates schema; then submit a valid JSON-only artifact conforming to schema.
  - Pass criteria: Artifacts with prose/markdown or schema violations are rejected; JSON-only, schema-valid artifact is accepted.
- **AT10 → [R10] (system)**
  - Procedure: Scan all evidence pointers in canonical artifacts (trace_report, review_findings, decision_record, quality_reports, stage_handoffs) for structure and JSON Pointer format.
  - Pass criteria: All evidence pointers use {artifact_ref, entity_id|null, json_pointer, note} with json_pointer starting with '/' and resolving to a valid path; any non-compliant evidence format causes validation failure.
- **AT11 → [R11] (system)**
  - Procedure: Attempt to cite repository/code/workspace state using a non-JSON artifact reference (e.g., file path or code pointer) in a canonical artifact; then cite the same state using a versioned JSON artifact with RFC 6901 pointer.
  - Pass criteria: Non-JSON artifact evidence is rejected; JSON artifact citation via RFC 6901 pointer is accepted.
- **AT12 → [R12] (system)**
  - Procedure: Attempt to modify a canonical JSON artifact via direct replacement (non-patch); then perform the same modification using an RFC 6902 JSON Patch array.
  - Pass criteria: Direct modification is rejected; JSON Patch modification is accepted and recorded.
- **AT13 → [R13] (system)**
  - Procedure: Submit code changes as (a) a diff-only patchset JSON artifact with deterministic apply order; (b) a non-diff mechanism (e.g., direct file writes or prose instructions).
  - Pass criteria: Diff-only patchset artifact is accepted and applied under bounded apply/validate loops; non-diff mechanism is rejected.
- **AT14 → [R14] (system)**
  - Procedure: Create a patchset that attempts to modify a forbidden/protected path from repo_context.json and exceeds operation caps; apply patchset under orchestrator enforcement.
  - Pass criteria: Patchset writes to forbidden/protected paths are blocked; operation caps are enforced and exceedance causes failure; blocks are recorded in the event log.
- **AT15 → [R15] (system)**
  - Procedure: Provide multiple candidate repair patchsets that all pass validation and differ in operation count and bytes changed; record orchestrator selection.
  - Pass criteria: Orchestrator selects the smallest passing repair using tie-breakers: fewest operations, then fewest bytes changed, then first-generated.
- **AT16 → [R16] (system)**
  - Procedure: Propose a semantic change to an upstream R#/AT#/O#/K# entity without ChangeRequest; then propose the same change with a ChangeRequest and decision record plus required escalation.
  - Pass criteria: Unapproved semantic change is rejected; ChangeRequest with decision record and escalation approval is required before applying the deviation.
- **AT17 → [R17] (system)**
  - Procedure: Inspect workspace for canonical vs non-canonical partitions and context pack assembly rules; verify canonical immutability after freeze and append-only behavior for non-canonical notes; verify context packs are deterministically built with evidence pointers.
  - Pass criteria: Workspace enforces canonical vs non-canonical partition rules; canonical artifacts are immutable after freeze; non-canonical notes are append-only with provenance; context packs are deterministically built and cite contents via evidence pointers.
- **AT18 → [R18] (system)**
  - Procedure: Enumerate all workspace items and compare against workspace_index.json entries; verify context pack sizes against configured bounds.
  - Pass criteria: All workspace items are indexed with type, version, provenance; any unindexed item or out-of-bounds context pack causes validation failure.
- **AT19 → [R19] (system)**
  - Procedure: Attempt tool invocations outside the declared permission matrix (filesystem write outside scope, network access, disallowed command execution) and within allowed permissions; inspect implementation_event_log.jsonl for records.
  - Pass criteria: Unauthorized tool invocations are blocked and recorded; authorized invocations succeed; enforcement is deterministic and logged.
- **AT20 → [R20] (system)**
  - Procedure: Inject known secret patterns into a candidate artifact and event log content, then run secrets detection/redaction guardrails prior to persistence.
  - Pass criteria: Secrets are detected and blocked/redacted; validators fail the run if any secret is persisted in event logs, workspace artifacts, or canonical artifacts.
- **AT21 → [R21] (system)**
  - Procedure: Attempt to Freeze when one or more closure gates (build/test/lint/typecheck, artifact integrity, workspace integrity, risk closure, reproducibility, trace completeness) are failing; then attempt Freeze when all gates pass.
  - Pass criteria: Freeze is denied when any gate fails; Freeze is permitted only when all required deterministic gates pass.
- **AT22 → [R22] (system)**
  - Procedure: Run a workflow with distinct writer, reviewer, and judge roles using multi-vendor routing; inspect produced artifacts to ensure reviewers emit findings-only and do not produce patches; verify orchestrator routing records.
  - Pass criteria: Writer/reviewer/judge roles are distinct, reviewers emit findings-only artifacts, judges emit decisions; multi-vendor routing is recorded and no role performs more than one function within the same stage.
- **AT23 → [R23] (system)**
  - Procedure: Induce flaky validator failures that require retries; observe retry counts and outcomes in implementation_event_log.jsonl; exceed retry budget and check escalation path.
  - Pass criteria: Retries follow deterministic policy with bounded budgets and are recorded; when budget is exceeded, failures are quarantined and escalated via the defined deterministic path with records in event log and decision artifacts.

---

## Architecture

### Option O1
**Summary:** Sequential staged pipeline with explicit council role pods (Writer/Reviewer/Patcher/Judge) routed by an orchestrator-owned linear state machine. State lives in a governed workspace/artifact store; deterministic context packs are compiled per stage boundary; repair loops are bounded and enforced by validators and orchestrator policy.

**Components**
- **C1 — Orchestrator Core (Linear State Machine)**
  - Enforce strict stage ordering and closure gates (Intake → Plan → Generate → Validate → Review → Patch → Judge → Freeze).
  - Own routing, budgets, retry/iteration caps, and termination/checkpoint logic (CNS1).
  - Compile deterministic context packs for each stage from workspace_index and declared selection rules (CNS8).
  - Enforce tool permission matrix per stage/role and forbidden-path / operation-cap rules (CNS7).
  - Trigger escalation via ChangeRequest and/or HITL when policy thresholds are exceeded (CNS5).
- **C2 — Workspace Gateway / Manager**
  - Provide the sole API for workspace and artifact IO, abstracting underlying storage.
  - Maintain workspace_index.json with types/versions/provenance; separate canonical vs non-canonical content (CNS8).
  - Support checkpoint/restore and emit/verify stage_handoff artifacts at boundaries (CNS3).
  - Validate artifact writes against expected schemas; block/redact secrets per policy (CNS9).
- **C3 — Intake Gate**
  - Validate plan_package_final.json, repo_context.json, workspace_context.json against schemas.
  - Initialize run_manifest and append-only event_log (CNS3).
  - Emit structured rejection errors for malformed/incomplete inputs.
  - Emit stage_handoff for Intake→Plan boundary.
- **C4 — Work Plan Compiler**
  - Decompose plan entities (R#/AT#/O#/K#) into ordered implementation tasks.
  - Produce work_plan.json with dependency graph, bounded patchset slots, and AT# trace mapping.
  - Enforce no semantic deviation from frozen plan entities (CNS5).
- **C5 — Writer Pod**
  - Consume orchestrator-compiled context pack for a task/task-batch.
  - Produce diff-only patchset JSON artifacts with deterministic apply order (CNS7).
  - Obey tool permissions and forbidden-path rules; output JSON-only artifacts (CNS2).
- **C6 — Validator Engine**
  - Run deterministic validators via repo_context.json entrypoints (build/test/lint/typecheck/etc.).
  - Emit structured test_results.json and quality_reports.json with AT# trace mapping.
  - Detect secrets in artifacts/workspace; enforce policy blocking (CNS9).
  - Produce pass/fail gate results consumed by orchestrator.
- **C7 — Reviewer Pod (Findings-Only)**
  - Review patchsets and validation outputs read-only.
  - Produce review_findings.json with RFC 6901 evidence pointers into versioned artifacts (CNS6).
  - Flag scope expansion, risks, and quality issues; never emit code/patch artifacts (CNS12).
- **C8 — Patcher Pod**
  - Produce RFC 6902 JSON Patch arrays for JSON artifact repairs (CNS7).
  - Produce replacement diff-only patchsets for code repairs (CNS7).
  - Operate within orchestrator-enforced iteration/op caps; provide candidates for deterministic smallest-passing selection.
- **C9 — Judge Pod**
  - Independently evaluate patchsets, validator outputs, and review findings.
  - Produce trace_report.json verifying evidence completeness (CNS6).
  - Emit freeze/no-freeze recommendation and risk-closure assessment against K# entries.
- **C10 — Freeze Assembler**
  - Collect and validate the required Implementation Freeze Bundle artifacts (CNS10).
  - Run final integrity/reproducibility/workspace-integrity checks before freeze (CNS3/CNS8).
  - Emit decision_record.json and release_bundle.json and a bundle manifest.

**Interfaces**
- **IF1:** C1 → C3 — Provide plan_package_final.json + repo_context.json + workspace_context.json; receive intake_result.json or structured schema/policy rejection plus stage_handoff.
- **IF2:** C3 → C4 — Provide validated inputs + intake stage_handoff; receive work_plan.json with task graph and AT# trace mapping.
- **IF3:** C1 → C2 — Checkpoint/restore and artifact IO operations; return updated workspace_index.json and versioned artifact_refs; append mutations to event_log.
- **IF4:** C1 → C5 — Dispatch task slice + context_pack_ref; receive patchset artifact_refs (diff-only JSON) for deterministic apply.
- **IF5:** C1 → C6 — Invoke validators on applied patchsets using repo_context.json entrypoints; receive test_results.json + quality_reports.json + gate status.
- **IF6:** C1 → C7 — Provide read-only context pack (patchsets + validation + work_plan slice); receive review_findings.json with RFC 6901 evidence pointers.
- **IF7:** C1 → C8 — Provide failures/findings + constraints/op caps; receive candidate RFC 6902 JSON Patches and/or replacement patchsets for bounded repair.
- **IF8:** C1 → C9 — Provide full run context pack (work_plan, patchsets, validation, findings, workspace index); receive trace_report.json and freeze recommendation.
- **IF9:** C9 → C10 — On freeze-eligible recommendation, assemble CNS10 bundle and run final validators; return freeze bundle manifest or structured missing/failed-gate report.

**Tradeoffs**
- Pros:
  - Simplest reproducible execution model: one active stage and a linear audit trail.
  - Clear separation of powers between orchestrator policy enforcement and role pods (writer/reviewer/judge).
  - Easier checkpoint/resume because stage boundaries are unambiguous and there is no parallel in-flight work.
  - Modular decomposition supports independent testing of stage components while keeping overall flow sequential.
- Cons:
  - Slowest wall-clock time for repos with many independent tasks due to lack of parallelism.
  - Single failing task can block pipeline progress even when other tasks are independent.
  - Context packs may grow across stages unless bounded/filtered aggressively.

**Risks**
- Long-horizon implementations may exceed token/time budgets due to serialized execution.
- Linear accumulation of artifacts can pressure context-window bounds if context-pack selection is not strict.
- Flaky validator behavior can exhaust retry budgets and stall the only active lane.

---

### Option O2
**Summary:** Task-parallel fan-out/fan-in architecture with deterministic per-lane execution and a governed integration sub-flow. Independent lanes run Generate→Validate→Review→Patch in parallel partitions; an Integration Merge/Validate/Review/Patch sequence produces a single integrated state for Judge→Freeze. Optionally modeled as a dedicated Integration Sub-Council for stronger separation and specialized integration review.

**Components**
- **C11 — Global Orchestrator / Council Coordinator (DAG + Budgets)**
  - Own top-level DAG workflow: Intake → Parallel Lanes → Integration → Judge → Freeze.
  - Allocate and enforce global/per-lane budgets, retry/iteration caps, and escalation policy (CNS1/CNS5).
  - Compile deterministic context packs for lanes and integration stages from partitioned workspace_index (CNS8).
  - Enforce multi-vendor role separation across writers/reviewers/judges (CNS12).
- **C12 — Intake & DAG Work Planner**
  - Validate plan_package_final.json, repo_context.json, workspace_context.json and initialize run_manifest/event_log/workspace_index.
  - Produce work_plan.json with dependency DAG, parallelism annotations, and lane partitioning hints.
  - Emit stage_handoff for Intake→Parallel boundary.
- **C13 — Task Lane Orchestrator**
  - Spawn and manage per-lane state machines (Generate→Validate→Review→Patch).
  - Emit per-lane stage_handoffs and completion signals.
  - Enforce lane-scoped tool permissions and file-path scoping for patchsets.
- **C14 — Writer Pool (Multi-Lane)**
  - Consume lane-scoped context pack and produce diff-only patchset artifacts scoped to lane file sets.
  - Output JSON-only artifacts per schema; obey forbidden-path and operation caps.
- **C15 — Per-Lane Validator**
  - Run scoped validators per lane; emit per-lane test_results.json and quality_reports.json with AT# mapping.
  - Detect secrets in lane artifacts/workspace partitions.
- **C16 — Per-Lane Reviewer (Findings-Only)**
  - Review lane patchsets and lane validation results read-only.
  - Produce per-lane review_findings.json with RFC 6901 evidence pointers; flag cross-lane dependency issues.
  - Enforce findings-only constraint (CNS12).
- **C17 — Per-Lane Patcher**
  - Produce lane-scoped repairs: RFC 6902 patches for JSON artifacts and replacement diff-only patchsets for code.
  - Operate within lane iteration/op caps; provide candidates for deterministic selection.
- **C18 — Integration Merge Engine**
  - Collect completed lane patchsets and apply in deterministic order to base repo state.
  - Detect merge conflicts/collisions and emit structured conflict reports for reroute/escalation.
  - Produce merged repo_snapshot.json and integrated patchset ordering record.
- **C19 — Integration Validator**
  - Run full-repo validators on merged state and emit integrated test_results.json and quality_reports.json.
  - Verify regressions and ensure cross-lane interactions are validated.
- **C20 — Integration Reviewer (Findings-Only)**
  - Perform holistic integration review focused on cross-module interactions and system-level quality.
  - Produce integration review_findings.json with RFC 6901 evidence pointers; findings-only (CNS12).
- **C21 — Integration Patcher**
  - Produce integration-level repairs for conflicts/regressions discovered at merge/validation time.
  - Operate within integration op caps and escalation thresholds.
- **C22 — Workspace Manager (Namespaced Partitions)**
  - Maintain per-lane workspace partitions plus a shared canonical integration partition (CNS8).
  - Maintain unified workspace_index.json spanning partitions with provenance and checkpoint metadata.
  - Support per-lane and global checkpoint/restore; enforce retention/compaction policies.
- **C23 — Judge Pod**
  - Evaluate integrated outputs (merged patchsets, integrated validation, lane + integration findings).
  - Produce trace_report.json across lanes and integration and freeze/no-freeze recommendation.
  - Assess risk closure against K# entries.
- **C24 — Freeze Assembler**
  - Collect CNS10-required artifacts from integrated state plus required lane artifacts as specified.
  - Run final integrity/reproducibility/workspace-integrity checks; emit decision_record.json and release_bundle.json.

**Interfaces**
- **IF10:** C11 → C12 — Provide input artifacts; receive validated inputs, work_plan.json with DAG/lane annotations, and stage_handoff for fan-out.
- **IF11:** C11 → C13 — Dispatch lane IDs + task slices + per-lane budgets; receive per-lane stage_handoffs and completion signals with lane artifact_refs.
- **IF12:** C13 → C14 — Provide lane context_pack_ref; receive lane patchset artifact_refs (diff-only JSON) scoped to lane file set.
- **IF13:** C13 → C15 — Invoke scoped validators for lane; receive lane test_results.json + quality_reports.json + gate status.
- **IF14:** C13 → C16 — Provide read-only lane context pack; receive lane review_findings.json with RFC 6901 evidence pointers.
- **IF15:** C13 → C17 — Provide lane failures/findings; receive lane-scoped repairs (RFC 6902 patches and/or replacement patchsets).
- **IF16:** C11 → C18 — Provide completed lane patchsets and deterministic merge order; receive merged repo_snapshot.json and conflict report (if any).
- **IF17:** C11 → C19 — Provide merged repo state; receive integrated test_results.json + quality_reports.json + gate status.
- **IF18:** C11 → C20 — Provide read-only integrated context pack; receive integration review_findings.json.
- **IF19:** C11 → C21 — Provide integration failures/conflicts; receive integration-level repair artifacts within op caps or escalation recommendation.
- **IF20:** C11 → C22 — Namespaced workspace operations (lane/integration partitions) + checkpoint/restore; receive unified workspace_index.json updates with provenance.
- **IF21:** C11 → C23 — Provide full integrated context pack including lane and integration artifacts; receive trace_report.json and freeze recommendation.
- **IF22:** C23 → C24 — On freeze-eligible recommendation, assemble CNS10 bundle and run final validators; return freeze bundle manifest or structured failure report.

**Tradeoffs**
- Pros:
  - Faster wall-clock time via parallel execution lanes for independent tasks.
  - Smaller, lane-scoped context packs reduce context-window pressure.
  - Integration-focused validation and review improves cross-module quality and catches coupling issues.
  - Isolation via workspace partitions reduces accidental cross-contamination across tasks.
- Cons:
  - Higher orchestration complexity (DAG scheduling, lane budgets, deterministic merge ordering, conflict handling).
  - Integration is a critical path and can become a bottleneck when conflicts/regressions are common.
  - More complex checkpoint/resume model (per-lane + global + integration checkpoints).
  - Higher total compute/token cost due to parallel role invocations.

**Risks**
- Merge conflicts between lanes may be frequent in tightly-coupled repos, driving escalation rates.
- Missed dependencies in task partitioning can cause late integration failures and rework.
- Reproducibility depends on merge ordering rules that must be independent of wall-clock completion timing.
- Workspace partition boundaries can be violated by side-effecting tools unless sandboxing and path scoping are strict.

---

### Option O3
**Summary:** Checkpoint-driven, sequential-by-default micro-cycle pipeline that processes work_plan tasks one at a time (Generate→Validate→Review→Patch), emitting a stage_handoff after each task. Supports deterministic partial-freeze bundles when budgets are reached, enabling long-horizon execution with resumable checkpoints.

**Components**
- **C25 — Orchestrator Core (Checkpoint-Oriented)**
  - Own task-by-task state machine and enforce per-task budgets/retry/iteration limits (CNS1).
  - Compile task-scoped context packs with bounded selection rules and sliding window over recent state (CNS8).
  - Decide continue vs partial-freeze based on deterministic budget thresholds; enforce escalation (CNS5).
  - Require and verify stage_handoff at every task boundary for resumability (CNS3).
- **C26 — Intake & Resume + Work Plan Compiler**
  - Validate inputs and initialize run_manifest/event_log/workspace_index, or resume from prior checkpoint.
  - Produce or update work_plan.json with completion status and deterministic next-task selection metadata.
  - Load prior stage_handoff for continuation and emit Intake→TaskCycle stage_handoff.
- **C27 — Task Cycle Engine**
  - Execute micro-cycles per task: Generate→Validate→Review→Patch until task gate passes or caps are hit.
  - Emit checkpoint and stage_handoff after each completed task.
  - Track cumulative budget consumption and signal orchestrator when thresholds approach.
- **C28 — Writer Pod**
  - Consume task-scoped context pack and produce diff-only patchset JSON for the current task (CNS7).
  - Enforce forbidden paths and JSON-only outputs (CNS2).
- **C29 — Task Validator (Incremental)**
  - Apply task patchset and run incremental validators (changed-files lint/tests as configured).
  - Emit incremental test_results.json and quality_reports.json with AT# mapping.
  - Detect secrets in artifacts/workspace (CNS9).
- **C30 — Task Reviewer (Findings-Only)**
  - Review task patchset and incremental validation outputs read-only.
  - Produce task review_findings.json with RFC 6901 evidence pointers (CNS6).
  - Findings-only enforcement (CNS12).
- **C31 — Task Patcher**
  - Produce task-scoped repairs: RFC 6902 JSON patches and/or replacement patchsets (CNS7).
  - Operate within task op caps; provide candidates for deterministic smallest-passing selection.
- **C32 — Regression Revalidation Gate (Cumulative)**
  - After each task completion, run full-repo validators on cumulative repo state as configured.
  - Detect regressions introduced by the latest task and emit cumulative validation overlays.
  - Trigger repair/rollback/escalation signals to orchestrator based on deterministic rules.
- **C33 — Workspace Manager (Append-Checkpoint Model)**
  - Maintain append-only checkpoint log per task completion; support deterministic restore to any checkpoint.
  - Maintain workspace_index.json with per-task provenance and completion status; separate canonical vs non-canonical (CNS8).
  - Enforce compaction policies for non-canonical content at checkpoint boundaries.
- **C34 — Judge Pod**
  - Evaluate cumulative state at full completion or partial-freeze boundary.
  - Produce trace_report.json for completed tasks and freeze/partial-freeze recommendation.
  - Assess risk closure for completed scope against K# entries.
- **C35 — Freeze / Partial-Freeze Assembler**
  - Assemble full or partial Implementation Freeze Bundle.
  - For partial freeze: include completion_status metadata and a resume_descriptor artifact to enable deterministic continuation.
  - Run integrity/reproducibility/workspace-integrity checks on completed scope (CNS3/CNS8).

**Interfaces**
- **IF23:** C25 → C26 — Provide inputs and (optionally) prior stage_handoff for resume; receive validated/resumed state plus work_plan.json with completion status.
- **IF24:** C25 → C27 — Dispatch next task (or small batch) with task-scoped budget; receive task completion signal, patchset refs, and stage_handoff.
- **IF25:** C27 → C28 — Provide task context_pack_ref; receive task patchset artifact_ref (diff-only JSON).
- **IF26:** C27 → C29 — Invoke incremental validators on task patchset; receive incremental test_results.json + quality_reports.json + gate status.
- **IF27:** C27 → C30 — Provide read-only task context; receive task review_findings.json with RFC 6901 evidence pointers.
- **IF28:** C27 → C31 — Provide task failures/findings; receive bounded task repair artifacts (RFC 6902 patches and/or replacement patchsets).
- **IF29:** C25 → C32 — After task completion, trigger cumulative regression revalidation; receive cumulative results or regression alert with evidence pointers.
- **IF30:** C25 → C33 — Append/checkpoint/restore operations per task; receive updated workspace_index.json with checkpoint IDs and provenance.
- **IF31:** C25 → C34 — At completion or partial-freeze boundary, provide cumulative context pack; receive trace_report.json and freeze/partial-freeze recommendation.
- **IF32:** C34 → C35 — Assemble full/partial freeze bundle; return bundle manifest plus resume_descriptor (for partial) or structured failure report.

**Tradeoffs**
- Pros:
  - Best fit for long-horizon work: fine-grained checkpoints minimize rework and enable deterministic resume.
  - Partial freeze preserves completed progress when budgets are exhausted.
  - Task-scoped context packs remain small and focused.
  - Early regression detection via frequent cumulative revalidation (when enabled).
- Cons:
  - Slower than parallel approaches for independent tasks.
  - Full regression revalidation after each task can be expensive for large repos with slow test suites.
  - Partial freeze adds complexity for downstream consumers who must handle incomplete bundles.
  - Resume correctness depends on stable environment/toolchain and faithful checkpoint restoration.

**Risks**
- Validation cost may pressure teams to weaken regression gates, reducing safety.
- Environment drift between runs can break deterministic replay/resume unless toolchains are pinned.
- Budget thresholds for partial-freeze decisions can be misestimated, freezing too early/late.
- Task ordering may hide implicit dependencies that cause late-task failures.

---

### Option O4
**Summary:** Monolithic Implementation Council component that encapsulates the full workflow (intake through freeze) behind a single external interface. The external orchestrator still enforces global policy, tool permissions, budgets, and audit spine, while the monolith manages internal stage logic and role separation.

**Components**
- **C36 — Orchestrator (External Policy Authority)**
  - Initiate runs and enforce system-wide policies, budgets, routing, iteration limits, and termination (CNS1).
  - Enforce tool permissions and secrets policy guardrails (CNS9).
  - Manage checkpointing boundaries and immutable audit spine requirements (CNS3).
- **C37 — Implementation Council (Monolithic)**
  - Consume plan_package_final.json and related required context artifacts as inputs.
  - Internally manage stages/roles (writer/reviewer/judge/patch/validate) and produce required artifacts (work plan, patchsets, validations, findings, decision record).
  - Write the final Implementation Freeze Bundle to the artifact store/workspace per CNS10.
  - Request tool/workspace operations via orchestrator-controlled interfaces (no direct policy override).
- **C38 — Artifact Store & Workspace (External)**
  - Provide durable, versioned storage for canonical artifacts and append-only logs (CNS3).
  - Host shared workspace with canonical vs non-canonical separation (CNS8).
  - Support addressing via artifact references and JSON Pointer evidence (CNS6).

**Interfaces**
- **IF33:** C36 → C37 — start_run(plan_package_final.json, repo_context.json, workspace_context.json) with orchestrator-enforced budgets/permissions; receive run outputs/status.
- **IF34:** C37 → C38 — read/write/query versioned artifacts and workspace items via artifact_ref; all writes validated and logged per policy.
- **IF35:** C36 → C37 — Policy enforcement channel: tool permission decisions, retry budgets, checkpoint/termination directives; monolith must comply.

**Tradeoffs**
- Pros:
  - Simplified external system boundary with a single main council component.
  - Potentially lower inter-component interface overhead inside the council boundary.
  - Encapsulates internal workflow evolution behind one interface.
- Cons:
  - Low modularity: harder to independently test/scale stages and enforce role separation internally.
  - Auditability can degrade if internal sub-stages are not surfaced as explicit artifacts/handoffs.
  - Risk of inadvertently weakening separation of powers and findings-only constraints without strict internal controls.

**Risks**
- Internal complexity may grow and undermine CNS12 separation (reviewers/judges becoming entangled with writing/patching).
- Hidden internal state transitions can make checkpoint/resume and traceability less explicit unless the monolith emits full stage_handoff artifacts.
- Monolith becomes a high-blast-radius component for failures and changes.

---

### Chosen Architecture
- **option_id:** O2

**Rationale**
O2 directly addresses O1's primary weakness—wall-clock time—by enabling parallel execution lanes for independent tasks while preserving all MUST requirements. The hybrid milestone strategy de-risks adoption: Phase 1 proves the single-lane path works (equivalent to O1 core), then Phases 2–3 incrementally layer parallelism and integration. O3's checkpoint-driven micro-cycle model was considered but rejected because it does not reduce wall-clock time for independent tasks and its partial-freeze complexity adds downstream consumer burden without proportional benefit for repos with cleanly partitionable modules. O4's monolithic boundary was rejected due to low modularity, difficulty enforcing role separation internally, and high blast-radius for failures. O2's namespaced workspace partitions (C22) provide stronger isolation than O1's single workspace, and the dedicated Integration Merge Engine (C18) with deterministic ordering directly addresses the merge-conflict risk (K12) and alignment warning AL006. The risk posture acknowledges K12 as the primary elevated risk and mitigates it with Phase 3's focused integration sub-flow and explicit conflict escalation thresholds.

**High-Level Dataflow**
plan_package_final.json + repo_context.json + workspace_context.json → C12 (Intake & DAG Work Planner): validates inputs, initializes run_manifest/event_log/workspace_index, produces work_plan.json with DAG/lane annotations → C11 (Global Orchestrator) fans out to C13 (Task Lane Orchestrators): each lane runs C14 (Writer) → C15 (Validator) → C16 (Reviewer) → C17 (Patcher) cycles within lane-scoped workspace partitions managed by C22 → lane completion signals flow back to C11 → C18 (Integration Merge Engine) applies lane patchsets in deterministic order to base repo state, emitting conflict reports if needed → C19 (Integration Validator) runs full-repo validation → C20 (Integration Reviewer) produces holistic findings → C21 (Integration Patcher) repairs integration issues → C23 (Judge Pod) evaluates integrated outputs and produces trace_report.json + freeze recommendation → C24 (Freeze Assembler) collects all required artifacts, runs final closure gates, and emits the Implementation Freeze Bundle.

**Key Design Decisions**
- Hybrid milestone strategy: Phase 1 validates single-lane foundation before adding parallelism, de-risking the transition to multi-lane execution.
- Deterministic merge ordering in C18 is independent of wall-clock lane completion timing, ensuring reproducibility (addresses AL006 and K12).
- Per-lane workspace partitions (C22) enforce isolation; a shared canonical integration partition aggregates merged state.
- Per-lane orchestrators (C13) enforce lane-scoped tool permissions and file-path scoping, reducing cross-contamination risk.
- Integration sub-flow (C18→C19→C20→C21) is a dedicated stage with its own review/patch cycle, not a simple merge step.
- High HITL strictness retained across all phases due to parallel complexity and unresolved open questions Q7–Q9.
- Context packs are lane-scoped during parallel execution and integration-scoped during merge, keeping per-invocation context bounded (mitigates K9).
- Role separation enforced per-lane: each lane has distinct writer/reviewer/patcher pods with multi-vendor routing (mitigates K10).

---

## Milestones

### M1 — Phase 1: Foundation (Single-Lane Equivalent)
**Deliverables**
- **M1-D1:** Global Orchestrator (C11) with DAG workflow engine, budget/retry enforcement, and checkpoint logic.
- **M1-D2:** Intake & DAG Work Planner (C12) with input validation (plan_package_final.json, repo_context.json, workspace_context.json), run_manifest/event_log initialization, and work_plan.json generation.
- **M1-D3:** Workspace Manager (C22) with namespaced partitions, canonical vs non-canonical separation, workspace_index.json maintenance, and checkpoint/restore support.
- **M1-D4:** Single-lane execution path: one Writer (C14), one Validator (C15), one Reviewer (C16), one Patcher (C17) operating sequentially through C11.
- **M1-D5:** Audit spine: immutable run_manifest, append-only event_log, versioned artifact store with checkpoints at every stage boundary.
- **M1-D6:** Tool permission matrix enforcement for Intake, Generate, PatchApply, Validate, and Review stages.
- **M1-D7:** Secrets detection/redaction/blocking guardrails integrated into Validate stage and workspace writes.
- **M1-D8:** Stage_handoff artifacts emitted at every checkpoint boundary with required resumable-state fields.

**Exit Criteria**
- AT1 passes: valid plan accepted; chat logs and non-canonical inputs rejected.
- AT2 passes: missing/invalid repo_context.json rejected; valid repo_context.json accepted.
- AT3 passes: missing/invalid workspace_context.json rejected; valid workspace_context.json accepted.
- AT4 passes (single-lane subset): stage sequence includes Intake → Generate → Validate → Review with checkpoint at each transition.
- AT5 passes: stage_handoff artifact exists for every stage transition in single-lane flow.
- AT8 passes: run_manifest immutable; event_log append-only; artifacts versioned and checkpointed.
- AT9 passes: prose/markdown/schema-invalid artifacts rejected; JSON-only schema-valid artifacts accepted.
- AT19 passes: unauthorized tool invocations blocked and logged; authorized invocations succeed.
- AT20 passes: secrets detected and blocked/redacted before persistence.

**Depends on:** (none)

---

### M2 — Phase 2: Parallel Lanes (Two-Lane Test)
**Deliverables**
- **M2-D1:** Task Lane Orchestrator (C13) managing per-lane state machines (Generate→Validate→Review→Patch) with per-lane stage_handoffs and completion signals.
- **M2-D2:** Multi-lane Writer Pool (C14) consuming lane-scoped context packs and producing lane-scoped diff-only patchset artifacts.
- **M2-D3:** Per-Lane Validator (C15) running scoped validators and emitting per-lane test_results.json/quality_reports.json with AT# mapping.
- **M2-D4:** Per-Lane Reviewer (C16) producing lane-scoped review_findings.json (findings-only) with RFC 6901 evidence pointers.
- **M2-D5:** Per-Lane Patcher (C17) producing lane-scoped RFC 6902 patches and replacement patchsets within lane op caps.
- **M2-D6:** Demonstrated two-lane parallel execution on a test repository with independent modules.
- **M2-D7:** Per-lane workspace partitions enforced by C22 with unified workspace_index.json spanning partitions.

**Exit Criteria**
- AT12 passes: canonical JSON artifact modification only via RFC 6902 JSON Patch; direct replacement rejected.
- AT13 passes: diff-only patchset artifacts accepted; non-diff code mutation mechanisms rejected.
- AT14 passes: forbidden/protected path writes blocked; operation caps enforced; blocks logged.
- AT22 passes: writer/reviewer/judge roles distinct per lane; reviewers emit findings-only; multi-vendor routing recorded; no role performs more than one function within same stage.
- Two parallel lanes complete Generate→Validate→Review→Patch cycles independently without cross-contamination.
- Per-lane stage_handoffs emitted and validated.

**Depends on:** M1

---

### M3 — Phase 3: Integration Sub-Flow
**Deliverables**
- **M3-D1:** Integration Merge Engine (C18) with deterministic merge ordering algorithm independent of wall-clock lane completion timing.
- **M3-D2:** Integration Validator (C19) running full-repo validators on merged state and emitting integrated test_results.json/quality_reports.json.
- **M3-D3:** Integration Reviewer (C20) producing integration review_findings.json (findings-only) with RFC 6901 evidence pointers.
- **M3-D4:** Integration Patcher (C21) producing integration-level repairs within op caps and escalation thresholds.
- **M3-D5:** Structured conflict_report artifact emitted by C18 when merge conflicts exceed deterministic resolution thresholds.
- **M3-D6:** Deterministic merge ordering produces identical results regardless of lane completion order (resolves AL006).
- **M3-D7:** ChangeRequest artifact schema and deterministic rules for proposing/approving changes when conflicts require semantic deviation.

**Exit Criteria**
- AT15 passes: orchestrator selects smallest passing repair using deterministic tie-breakers (fewest ops → fewest bytes → first-generated) across integration repairs.
- AT10 passes: all evidence pointers in integration artifacts use {artifact_ref, entity_id|null, json_pointer, note} with valid RFC 6901 paths.
- AT16 passes: unapproved semantic changes to R#/AT#/O#/K# rejected; ChangeRequest with decision record and escalation required.
- Deterministic merge ordering verified: identical merged repo_snapshot.json produced regardless of lane completion order.
- Integration validation (build/test/lint) passes on merged state.
- Conflict escalation path demonstrated: conflicts beyond threshold trigger HITL with structured conflict summary.

**Depends on:** M2

---

### M4 — Phase 4: Judge, Freeze & Full Closure
**Deliverables**
- **M4-D1:** Judge Pod (C23) evaluating integrated outputs, producing trace_report.json with cross-lane and integration evidence, and emitting freeze/no-freeze recommendation with risk closure assessment against K# entries.
- **M4-D2:** Freeze Assembler (C24) collecting all CNS10-required artifacts, running final integrity/reproducibility/workspace-integrity checks, and emitting decision_record.json and release_bundle.json.
- **M4-D3:** Complete Implementation Freeze Bundle with all required artifacts and directories.
- **M4-D4:** Flaky/nondeterministic failure handling with deterministic retry rules, bounded budgets, quarantine, and escalation path (patch → reroute → HITL → ChangeRequest).
- **M4-D5:** Full AT suite validation confirming all 23 acceptance tests pass.

**Exit Criteria**
- AT5 passes: stage_handoff artifact exists for every stage transition including Judge and Freeze.
- AT6 passes: every stage_handoff contains all required fields and valid evidence pointers.
- AT7 passes: freeze bundle contains all minimum required artifacts and directories.
- AT17 passes: workspace enforces canonical/non-canonical partition; canonical immutable after freeze; non-canonical append-only with provenance; context packs deterministically built.
- AT18 passes: all workspace items indexed; unindexed items or out-of-bounds context packs cause validation failure.
- AT21 passes: freeze denied when any gate fails; permitted only when all gates pass.
- AT23 passes: retries follow deterministic policy with bounded budgets; exceeded budgets trigger quarantine and escalation.
- All 23 acceptance tests (AT1–AT23) pass in end-to-end run.
- Risk closure assessment covers all K# entries with evidence pointers.

**Depends on:** M3

---

## Risk Register (K#)
- **K1 (critical):** Silent plan-semantics drift / scope expansion: implementation artifacts or code changes effectively alter the meaning of upstream R#/AT#/O#/K# without an explicit ChangeRequest + governed decision, violating no-scope-expansion constraints.
  - Mitigation: Add deterministic semantic-drift/trace gates: (1) require explicit mapping from each change/patchset and decision clause to referenced R#/AT#/O#/K# IDs; (2) validator rejects any patchset/decision_record that introduces new requirements/acceptance criteria not present in plan_package_final.json unless a ChangeRequest artifact is present and approved per policy; (3) enforce reviewer role emits findings-only and cannot author ChangeRequest/patchsets; (4) Intake validator reroutes upstream if plan ambiguities force assumptions that would change semantics.
  - Owner: Orchestrator+Validators; Status: planned
- **K2 (critical):** Audit spine breakage: missing/overwritten run_manifest, non-append-only event_log, or unversioned artifacts leads to irreproducible outcomes and invalidates evidence pointers.
  - Mitigation: Enforce immutability/append-only at storage interface; validator requires: run_manifest immutable hash recorded at first emission, event_log monotonic sequence numbers, artifact_ref uniqueness + content hashes; stage transitions blocked if any required audit artifacts are missing or mutated.
  - Owner: Orchestrator+ArtifactStore+Validators; Status: planned
- **K3 (high):** Evidence pointer standard (RFC 6901) not used consistently: artifacts embed freeform citations (paths/prose/non-RFC6901 references), causing unverifiable traceability and broken downstream consumption.
  - Mitigation: Add strict evidence-reference linter validator: only allow evidence pointer objects {artifact_ref, entity_id|null, json_pointer, note}; reject any nonconforming reference fields; require trace_report.json to enumerate all evidence pointers and validate pointers resolve against versioned JSON artifacts before freeze.
  - Owner: Validators; Status: planned
- **K4 (critical):** An agent generates a code patchset that introduces a severe security vulnerability (e.g., RCE, data exfiltration) that is missed by automated validators.
  - Mitigation: Implement and mandate a comprehensive suite of security validators (e.g., SAST/DAST/dependency scanning as applicable) as a freeze gate. Critical findings must trigger a mandatory HITL review by Security/Compliance prior to any freeze/release decision.
  - Owner: Security/Compliance Team; Status: planned
- **K5 (critical):** Repository protection bypass: patchsets or tools modify forbidden/protected paths or perform high-risk operations outside policy (e.g., CI/release config, auth/deploy/credential handling), leading to security/compliance incidents.
  - Mitigation: Enforce deterministic forbidden/protected-path controls at both patch-apply time and tool-sandbox policy using repo_context.json patterns: reject before apply/execute; require HITL for any change touching protected paths or release/deploy/auth configuration; enforce operation caps and file-type restrictions; block any tool execution attempt to write forbidden paths and record in audit log.
  - Owner: Orchestrator Policy+Validators+Platform/DevOps; Status: planned
- **K6 (high):** Nondeterministic tool executions (flaky tests/builds) cause inconsistent gate outcomes, leading to incorrect freezes or endless repair loops / exhausted budgets.
  - Mitigation: Define deterministic retry/quarantine rules: fixed retry budget, fixed backoff schedule, record all attempts in event_log, classify failures as flaky using deterministic thresholds, and block freeze unless gates pass under the deterministic policy; add stabilization/quarantine path with bounded changes and deterministic escalation (patch → reroute → HITL → ChangeRequest).
  - Owner: Orchestrator Policy+Validators; Status: planned
- **K7 (high):** Secrets leakage into canonical artifacts or workspace (event log, patches, snapshots, context packs, stage handoffs), violating secrets policy and creating durable compromise.
  - Mitigation: Mandatory secrets scanning at each checkpoint and before freeze across patchsets/logs/repo_snapshot/context_packs/stage_handoffs; deterministic redaction/blocking rules (block persistence and require remediation patchset); deny network exfiltration by default; ensure scanner findings are stored as structured artifacts without embedding secrets.
  - Owner: Orchestrator Policy+Security Validators; Status: planned
- **K8 (high):** Workspace integrity failure or corruption: canonical vs non-canonical separation is violated (overwrites/unversioned state), or workspace becomes inconsistent, breaking deterministic resume and traceability.
  - Mitigation: Run workspace integrity validators at each stage handoff: schema compliance, index completeness, artifact checksums, and immutability flags. Enforce workspace namespace policy: canonical paths append-only/versioned; non-canonical append-only with provenance; workspace_index.json must enumerate all items; validator rejects unindexed items or canonical overwrites; checkpoint immutable workspace versions.
  - Owner: Orchestrator+Workspace Validators; Status: planned
- **K9 (medium):** Context pack overflow or under-selection: bounded context packs omit critical artifacts or exceed size limits, causing incorrect decisions/patches or truncation-driven errors.
  - Mitigation: Deterministic, versioned context-pack selection rules with priorities and explicit byte/token budgets; validator checks pack completeness against stage-required sets and rejects out-of-bounds packs. Include a structured mechanism to flag missing context and a context-adequacy check during Review.
  - Owner: Orchestrator+Validators; Status: planned
- **K10 (high):** Role separation failure: writer/reviewer/judge boundaries and/or multi-vendor diversity are not enforced, allowing self-review/self-approval and undermining independence/governance.
  - Mitigation: Orchestrator-enforced output-type allowlists by role (reviewers findings-only; judges decisions-only; writers/patchers write/patch only) plus multi-vendor routing constraints (review/judge model family must differ from writer). Record agent/model/vendor provenance in run_manifest for each stage output and validate separation constraints before stage transitions/freeze.
  - Owner: Orchestrator Policy; Status: planned
- **K11 (high):** RFC 6902 JSON Patch misuse: syntactically valid patches touch forbidden JSON Pointer paths, exceed op caps, or create schema/logic-invalid states, corrupting canonical JSON artifacts (e.g., work_plan).
  - Mitigation: Validator enforces: (1) op count caps, (2) forbidden JSON Pointer path rules per artifact type, (3) schema revalidation after patch application, (4) deterministic patch ordering and tie-breakers; for critical artifacts, add semantic validators for logical consistency; reject nonconforming patches before apply.
  - Owner: Orchestrator+Schema/Validation Team; Status: planned
- **K12 (high):** Integration/merge conflicts: multiple patchsets (including parallel pods) produce overlapping diffs causing conflicts, inconsistent base/head commits, or partial application that breaks reproducibility; automated conflict resolution may lack safe fallback.
  - Mitigation: Require explicit base_commit in each patchset; deterministic apply/merge ordering rules independent of wall-clock completion timing; strict limits for automated conflict resolution; if limits exceeded, block and trigger reroute to integration stage or HITL with a pre-packaged conflict summary. Emit a structured integration_report/conflict_report describing classification and chosen deterministic escalation path.
  - Owner: Orchestrator Policy+Integration Validators; Status: planned
- **K13 (medium):** Acceptance-test trace mismatch: test_results cannot be deterministically mapped to AT# acceptance tests, breaking trace completeness gates and weakening validation credibility.
  - Mitigation: Define required AT# linkage fields in test metadata; validator requires that each AT# has either a passing mapped test or an explicit, approved waiver/ChangeRequest; trace_report must enumerate AT#→test evidence pointers.
  - Owner: QA/Trace Validators; Status: planned
- **K14 (high):** Release artifact unsafe/incorrect: release_bundle produced without passing all gates, includes unreviewed binaries, or references non-reproducible build steps.
  - Mitigation: Freeze gate requires reproducible build recipe captured in structured artifact; release_bundle allowed only after all validators pass; require hashed build outputs and provenance record; disallow network-dependent build steps unless explicitly allowlisted and captured in dependency policy artifacts.
  - Owner: Orchestrator+Release Validators; Status: planned
- **K15 (high):** Supply-chain/network risk: package manager operations or network access pull unpinned dependencies, introduce version drift across runs, or leak information, undermining reproducibility and security.
  - Mitigation: Default network deny; allow network only in explicitly designated stages with allowlisted domains and deterministic lockfile requirements. Mandate lockfiles and/or audited offline mirrors/caching proxies when network is enabled. Validator checks dependency changes are pinned and justified with evidence pointers.
  - Owner: Orchestrator Policy+Security/Compliance Validators+Platform/DevOps; Status: planned
- **K16 (medium):** Over-broad refactors: patchsets attempt repo-wide rewrites contrary to bounded-change constraints, increasing failure rate and review burden.
  - Mitigation: Define patchset size limits (files changed, diff hunks, bytes, risk-weighted thresholds) and require decomposition into staged patchsets; validator rejects oversized patchsets unless approved via HITL with rationale captured in decision_record.
  - Owner: Orchestrator Policy+Validators; Status: planned
- **K17 (medium):** Checkpoint/resume inconsistency: missing or incomplete stage_handoff artifacts cause inability to resume deterministically, leading to duplicated work or inconsistent outcomes.
  - Mitigation: Require stage_handoff emission at every checkpoint boundary with mandatory pointers to work_plan, current patchsets, validation outputs, and pending blockers; validator blocks stage transition if handoff missing or pointers do not resolve.
  - Owner: Orchestrator+Workspace Validators; Status: planned
- **K18 (high):** Decision integrity risk: judge outputs or decision_record include conclusions/waivers without linked evidence pointers, allowing ungrounded approvals/waivers.
  - Mitigation: Decision validator: every approval/waiver must include evidence pointers to passing gate artifacts or an explicit policy exception with HITL signoff; disallow freeze if any decision clause lacks evidence pointers.
  - Owner: Judge Policy+Validators; Status: planned
- **K19 (high):** Misconfigured tool permissions: stages accidentally permit destructive commands, broad filesystem writes, undeclared entrypoints, or unbounded artifact mutation, enabling accidental damage or policy bypass.
  - Mitigation: Machine-enforced tool permission matrix per stage with deny-by-default; validate effective permissions (policy-as-data) continuously; audit logging of tool invocations and denials; HITL required to expand permissions beyond baseline.
  - Owner: Orchestrator Policy; Status: planned
- **K20 (high):** Upstream plan_package_final.json is ambiguous or incomplete, forcing the Implementation Council to make assumptions that later turn out to be incorrect, requiring costly rework and/or semantic drift.
  - Mitigation: Intake stage includes validator that checks the input plan for completeness/clarity. If validation fails, orchestrator immediately reroutes upstream to the Planning Council rather than allowing assumption-based implementation.
  - Owner: Intake & Validation Team; Status: planned
- **K21 (medium):** The 'smallest passing repair' selection logic is too simplistic and consistently chooses a sub-optimal patch from multiple valid proposals, leading to technical debt.
  - Mitigation: Add deterministic secondary evaluation signals (e.g., style/lint adherence, complexity deltas) as additional structured validators or deterministic scoring, while keeping selection rules auditable and reproducible. Escalate to HITL when multiple passing candidates have materially different long-term maintenance risk.
  - Owner: Orchestrator Design Team; Status: planned
- **K22 (low):** Non-canonical workspace data (e.g., scratchpad notes) grows indefinitely, making audits difficult and potentially exceeding storage quotas.
  - Mitigation: Define and implement automated compaction and retention policies for non-canonical data. Create summarization artifacts (non-canonical, with provenance) before purging to preserve audit intent.
  - Owner: Workspace Backend Team; Status: planned

---

## Governance

### Tool Policy
#### Stage Policies
- **Intake**
  - Allowlisted tools: artifact_read, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - workspace_write: allow_noncanonical_only
    - artifact_write: allow_noncanonical_only
    - notes: Ingest and validate plan_package_final.json and required context artifacts; no code changes; do not persist secrets.
  - HITL triggers:
    - Missing required inputs (plan_package_final.json, repo_context.json, workspace_context.json)
    - Schema validation failures that cannot be repaired via RFC6902 without changing semantics
    - Input plan_package_final.json incomplete/ambiguous per intake validator (reroute upstream)

- **WorkPlanning**
  - Allowlisted tools: artifact_read, artifact_write, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - workspace_write: canonical_versioned_only
    - artifact_write: canonical_allowed_only_for_declared_types
    - evidence_standard: rfc6901_only
    - notes: Compile/update work_plan and trace mappings only; no repo mutation.
  - HITL triggers:
    - Proposed work requires changing plan semantics (must raise ChangeRequest)
    - Work plan implies assumptions due to ambiguous plan semantics (reroute upstream)

- **Generate**
  - Allowlisted tools: artifact_read, artifact_write, workspace_write, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - artifact_write: canonical_allowed_only_for_declared_types
    - workspace_write: canonical_versioned_only
    - evidence_standard: rfc6901_only
    - notes: Draft patchsets and required mappings as artifacts; no direct repo mutation.
  - HITL triggers:
    - Proposed work requires changing plan semantics (must raise ChangeRequest)
    - Proposed patchsets touch protected/forbidden paths per repo_context

- **PatchApply**
  - Allowlisted tools: artifact_read, patch_apply, repo_snapshot, artifact_write, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: allow_via_patch_apply_only
    - repo_write_paths: enforced_by_repo_context
    - patchset_limits:
      - max_files_changed: policy_defined
      - max_diff_bytes: policy_defined
      - forbidden_file_globs: from_repo_context
    - notes: Only apply diff-only patchsets against declared base_commit; direct edits prohibited.
  - HITL triggers:
    - Patch application conflict beyond deterministic thresholds
    - Any attempted modification of forbidden/protected paths
    - Patchset exceeds size/risk limits

- **Validate**
  - Allowlisted tools: artifact_read, run_build, run_tests, run_lint, run_typecheck, security_scan, secrets_scan, artifact_write, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - tool_entrypoints: must_be_declared_in_repo_context
    - retry_policy: deterministic_budgets_only
    - notes: Execute deterministic validators and emit structured results; do not run undeclared commands.
  - HITL triggers:
    - Secrets detected in any artifact/workspace output
    - Flaky failure classification exceeds retry/quarantine policy thresholds
    - Security validator reports critical finding
    - Validator outputs indicate policy violations requiring exception

- **IntegrateAndValidate**
  - Allowlisted tools: artifact_read, patch_apply, repo_snapshot, run_build, run_tests, security_scan, secrets_scan, artifact_write, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: allow_via_patch_apply_only
    - repo_write_paths: enforced_by_repo_context
    - retry_policy: deterministic_budgets_only
    - notes: Deterministic integration merge/apply and full-repo validation; emits structured conflict/integration reports.
  - HITL triggers:
    - patch_conflict_unresolved
    - security_validator_critical_finding
    - build_failure
    - Deterministic retry budget exhausted

- **Review**
  - Allowlisted tools: artifact_read, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - artifact_write: deny
    - workspace_write: deny
    - notes: Reviewers are findings-only; they do not emit canonical writing/patch artifacts.
  - HITL triggers:
    - critical_finding_proposed
    - Reviewer identifies high-risk area requiring manual security/compliance approval
    - Reviewer flags ambiguity that implies upstream plan defect (reroute to Planning Council)

- **Judge**
  - Allowlisted tools: artifact_read, artifact_write, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - decision_requirements: all_approvals_must_have_rfc6901_evidence_pointers
    - notes: Judge emits decisions/waivers only; no patch authoring.
  - HITL triggers:
    - Any waiver of a gate (tests/build/security/trace/reproducibility/workspace integrity) is requested
    - Any ChangeRequest approval is requested
    - Any permission expansion beyond baseline is requested

- **Freeze**
  - Allowlisted tools: artifact_read, artifact_write, bundle_release, secrets_scan, schema_validate
  - Restrictions:
    - network: deny
    - repo_write: deny
    - freeze_condition: all_required_gates_pass_and_trace_complete
    - notes: Emit Implementation Freeze Bundle only when validators confirm all gates; otherwise block.
  - HITL triggers:
    - Attempt to freeze with any failing gate
    - Missing required artifacts in the freeze bundle list
    - Unresolved trace/evidence pointer validation errors
    - Secrets detected during pre-freeze scan

### HITL Policy
- **When to interrupt**
  - Any proposed ChangeRequest (plan-semantics change) or any evidence of silent scope expansion
  - Any attempt to modify forbidden/protected paths or perform high-risk repo operations (auth, deploy, credential handling)
  - Any secrets detection event in artifacts/workspace/logs or suspected exfiltration vector
  - Any security validator reports a 'critical' severity finding
  - Any deterministic retry budget for a failing validator is exhausted
  - Any merge/integration conflict beyond deterministic resolution thresholds
  - Any waiver request for required freeze gates (build/test/lint/typecheck/security/trace/reproducibility/workspace integrity)
  - Any tool-permission expansion beyond baseline (e.g., enabling network, broad filesystem writes, executing undeclared entrypoints)
  - The orchestrator detects a potential integrity violation in the audit spine or workspace

- **Approval roles**
  - Orchestrator Policy Owner
  - Security/Compliance Approver
  - Implementation Council Judge (HITL signatory)
  - Repo Owner/Maintainer (for protected paths and release approval)
  - Implementation Council Lead
  - Platform/DevOps Lead
  - Planning Council Representative

---

## Decision Log

### DEC1
- **Question:** Which architecture option should be selected for the Implementation Council?
- **Choice:** O2 — Task-parallel fan-out/fan-in with deterministic integration sub-flow.
- **Rationale:** O2 provides faster wall-clock execution for independent tasks via parallel lanes while preserving all MUST requirements. The hybrid milestone strategy de-risks by proving single-lane first (Phase 1), then incrementally adding parallelism (Phase 2) and integration (Phase 3). O2's dedicated integration sub-flow directly addresses merge-conflict risk (K12) and alignment warning AL006 with a deterministic merge-ordering algorithm.
- **Alternatives considered:**
  - O1: Sequential staged pipeline — simplest model but slowest wall-clock time; single failing task blocks all progress.
  - O3: Checkpoint-driven micro-cycle — good for long-horizon but no parallelism benefit; partial-freeze adds downstream complexity.
  - O4: Monolithic council — low modularity; harder to enforce role separation internally; high blast-radius.
- **What would change this decision:**
  - Target repository has extremely high inter-module coupling making clean lane partitioning infeasible (would favor O1 or O3).
  - Wall-clock time is not a priority and simplicity is paramount (would favor O1).
  - Long-horizon budget constraints dominate and partial-freeze is required (would favor O3).
  - Orchestrator implementation cannot support DAG scheduling and per-lane budget management (would favor O1).
- **Evidence pointers:**
  - {artifact_ref: architecture_canonical_v1, entity_id: null, json_pointer: /options/1, note: O2 architecture definition with parallel lanes and integration sub-flow, candidate_id: B2B, artifact_id: architecture_canonical, quote: null}
  - {artifact_ref: branch_set_v1, entity_id: null, json_pointer: /branches/B2, note: Branch B2 selects O2 with hybrid milestone strategy, candidate_id: B2B, artifact_id: branch_set, quote: null}
  - {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/11, note: K12 merge conflict risk — primary elevated risk for O2, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}

### DEC2
- **Question:** What milestone strategy should be used for delivering O2?
- **Choice:** Hybrid 4-phase strategy: Phase 1 (big-bang foundation with single-lane), Phase 2 (thin-slice parallel lanes), Phase 3 (thin-slice integration), Phase 4 (Judge + Freeze + full closure).
- **Rationale:** The hybrid approach de-risks parallel execution by first proving the single-lane path works in Phase 1, which validates intake, workspace governance, audit spine, and core enforcement mechanisms. Phases 2 and 3 incrementally add the parallel and integration complexity, each with focused exit criteria. This avoids a big-bang delivery of the full parallel system while still reaching parallelism earlier than a purely sequential thin-slice approach.
- **Alternatives considered:**
  - Pure thin-slice: deliver one end-to-end slice per phase — slower to reach parallelism benefit.
  - Pure big-bang: deliver all O2 components at once — high risk of integration failures.
  - Two-phase: foundation then everything else — insufficient gating on integration sub-flow.
- **What would change this decision:**
  - If the team has prior experience with parallel orchestration, Phases 2 and 3 could be merged.
  - If the target repo is very small, a 2-phase approach may suffice.
  - If integration complexity proves low in Phase 2 testing, Phase 3 scope could be reduced.
- **Evidence pointers:**
  - {artifact_ref: branch_set_v1, entity_id: null, json_pointer: /branches/B2/milestone_strategy, note: Hybrid milestone strategy definition for branch B2, candidate_id: B2B, artifact_id: branch_set, quote: null}

### DEC3
- **Question:** How should merge ordering be made deterministic for parallel lane outputs?
- **Choice:** Deterministic merge ordering algorithm in C18 that is independent of wall-clock lane completion timing, using a fixed ordering derived from work_plan.json task/lane IDs.
- **Rationale:** Reproducibility requires that the same set of lane patchsets always produces the same merged state regardless of which lane finishes first. Using work_plan.json task/lane IDs as the canonical ordering key ensures the algorithm is stable, auditable, and independent of runtime scheduling. This directly resolves alignment warning AL006.
- **Alternatives considered:**
  - Completion-order merge: simpler but non-reproducible across runs.
  - Topological sort by dependency graph: more complex but could optimize conflict detection — deferred as enhancement.
  - Single integration lane that rebases all others: high conflict rate for independent changes.
- **What would change this decision:**
  - If dependency-aware ordering significantly reduces conflict rates in practice, topological sort could replace fixed ID ordering.
  - If Q9 (conflict resolution strategy) is answered with a different merge model, the algorithm would adapt.
- **Evidence pointers:**
  - {artifact_ref: architecture_canonical_v1, entity_id: null, json_pointer: /options/1/components/7, note: C18 Integration Merge Engine responsibilities include deterministic merge ordering, candidate_id: B2B, artifact_id: architecture_canonical, quote: null}
  - {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/11, note: K12 mitigation requires deterministic apply/merge ordering independent of wall-clock timing, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}
  - {artifact_ref: alignment_warnings_v1, entity_id: null, json_pointer: /AL006, note: AL006 alignment warning on merge ordering determinism, candidate_id: B2B, artifact_id: alignment_warnings, quote: null}

### DEC4
- **Question:** What governance posture should be applied for tooling and HITL?
- **Choice:** Sandboxed tooling with high HITL strictness across all phases.
- **Rationale:** Parallel lanes increase the attack surface for tool misuse and cross-contamination. High HITL strictness ensures human oversight at critical junctures (merge conflicts, ChangeRequests, gate waivers, permission expansions). Sandboxed tooling enforces deny-by-default with per-stage allowlists, consistent with the tool policy in risk_gov_canonical.
- **Alternatives considered:**
  - Medium HITL strictness: faster iteration but higher risk of undetected issues in parallel execution.
  - Unsandboxed tooling: unacceptable given CNS1/CNS9 constraints.
- **What would change this decision:**
  - Resolution of Q7–Q9 with low-risk answers could allow relaxing to medium HITL strictness in later phases.
  - Demonstrated low conflict rates in Phase 2/3 testing could justify reduced HITL for subsequent runs.
- **Evidence pointers:**
  - {artifact_ref: branch_set_v1, entity_id: null, json_pointer: /branches/B2/governance_posture, note: Branch B2 governance posture: sandboxed tooling, high HITL strictness, candidate_id: B2B, artifact_id: branch_set, quote: null}
  - {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /tool_policy, note: Tool policy with deny-by-default and per-stage allowlists, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}

### DEC5
- **Question:** How should acceptance tests be phased across milestones?
- **Choice:** Phase 1 gates on AT1–AT3, AT4 (single-lane subset), AT5, AT8, AT9, AT19, AT20. Phase 2 gates on AT12–AT14, AT22. Phase 3 gates on AT10, AT15, AT16. Phase 4 gates on AT5–AT7, AT17–AT18, AT21, AT23. Full AT suite at final Freeze.
- **Rationale:** Each phase gates on the acceptance tests most relevant to its deliverables. Phase 1 covers input validation, audit spine, schema enforcement, tool permissions, and secrets policy. Phase 2 covers repair mechanisms and role separation in parallel context. Phase 3 covers integration-specific concerns (evidence pointers across integration, smallest-passing-repair, ChangeRequest). Phase 4 covers judge/freeze gates and full closure. This ensures incremental confidence building.
- **Alternatives considered:**
  - Run all ATs at every phase: excessive for early phases where components are not yet delivered.
  - Gate only at final Freeze: insufficient incremental validation; late discovery of issues.
- **What would change this decision:**
  - If a critical defect is found in a later phase that traces to an earlier phase's AT, the phasing would be revised to include regression checks.
  - If AT dependencies are discovered that cross phase boundaries, phasing would be adjusted.
- **Evidence pointers:**
  - {artifact_ref: branch_set_v1, entity_id: null, json_pointer: /branches/B2/acceptance_strategy, note: Acceptance strategy phasing for branch B2, candidate_id: B2B, artifact_id: branch_set, quote: null}
  - {artifact_ref: acceptance_canonical_v1, entity_id: null, json_pointer: /acceptance_tests, note: Full acceptance test suite (AT1–AT23), candidate_id: B2B, artifact_id: acceptance_canonical, quote: null}

### DEC6
- **Question:** How should K12 (merge conflicts from parallel lanes) be mitigated?
- **Choice:** Phase 3 dedicated integration sub-flow with deterministic merge ordering, structured conflict reports, bounded automated resolution, and HITL escalation when thresholds are exceeded.
- **Rationale:** K12 is the primary elevated risk for O2. Dedicating Phase 3 to integration with its own validate/review/patch cycle ensures conflicts are detected and resolved systematically. The deterministic merge ordering algorithm ensures reproducibility. Structured conflict_report artifacts provide transparency. Bounded automated resolution with explicit escalation thresholds prevents unbounded rework while ensuring human oversight for complex conflicts.
- **Alternatives considered:**
  - Inline conflict resolution during lane execution: breaks lane isolation and complicates per-lane state machines.
  - Post-freeze conflict resolution: too late; conflicts must be resolved before Judge evaluation.
  - Reject all conflicts and require re-partitioning: too conservative; many conflicts are trivially resolvable.
- **What would change this decision:**
  - If Q9 is answered with a specific merge strategy that changes the integration model.
  - If conflict rates in practice are near-zero, the integration sub-flow could be simplified.
  - If conflict rates are extremely high, the system should fall back to O1-style sequential execution.
- **Evidence pointers:**
  - {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/11, note: K12 risk entry with mitigation strategy, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}
  - {artifact_ref: architecture_canonical_v1, entity_id: null, json_pointer: /options/1/components/7, note: C18 Integration Merge Engine with conflict detection and deterministic ordering, candidate_id: B2B, artifact_id: architecture_canonical, quote: null}

---

## Supplemental Evaluations

### Risk Posture Assessment for O2 Parallel Architecture
K12 (merge conflicts) is elevated to primary risk due to parallel lanes. Mitigation is addressed in Phase 3 with deterministic merge ordering and explicit conflict escalation. K8 (workspace integrity) risk is higher due to namespaced partitions; mitigated by C22 partition enforcement and workspace integrity validators at each checkpoint. K10 (role separation) risk increases with parallel pods; mitigated by per-lane orchestrator-enforced output-type allowlists and multi-vendor routing. K9 (context pack overflow) risk is lower per-lane due to smaller scoped packs but requires integration-scoped pack management in Phase 3. K6 (flaky tests) risk is distributed across lanes but integration revalidation can amplify it; mitigated by deterministic retry budgets and quarantine policies. Open questions Q7 (diff format), Q8 (smallest passing repair selection), and Q9 (conflict resolution strategy) remain blocking and justify high HITL strictness.

Evidence:
- {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/11, note: K12 merge conflict risk, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}
- {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/7, note: K8 workspace integrity risk, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}
- {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/9, note: K10 role separation risk, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}
- {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/8, note: K9 context pack overflow risk, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}
- {artifact_ref: risk_gov_canonical_v1, entity_id: null, json_pointer: /risk_register/5, note: K6 flaky test risk, candidate_id: B2B, artifact_id: risk_gov_canonical, quote: null}

### Alignment Warning Coverage
AL006 (merge ordering determinism) is directly resolved in Phase 3 via C18's deterministic merge ordering algorithm with fixed ordering derived from work_plan.json task/lane IDs. AL004 (context pack rules) is addressed by lane-scoped context packs in Phase 2 and integration-scoped packs in Phase 3. AL001/AL002/AL003/AL007 are resolved in Phase 1 alongside O1-equivalent components (intake validation, audit spine, workspace governance, stage handoffs).

Evidence:
- {artifact_ref: alignment_warnings_v1, entity_id: null, json_pointer: /AL006, note: AL006 merge ordering alignment warning, candidate_id: B2B, artifact_id: alignment_warnings, quote: null}
- {artifact_ref: alignment_warnings_v1, entity_id: null, json_pointer: /AL004, note: AL004 context pack rules alignment warning, candidate_id: B2B, artifact_id: alignment_warnings, quote: null}

### Open Question Impact on B2B
Q7 (diff format), Q8 (smallest passing repair), and Q9 (conflict resolution for parallel patchsets) are the most impactful blocking questions for this candidate. Q9 is especially critical because it directly affects the Integration Merge Engine (C18) design. Q7 affects all patchset-producing components (C14, C17, C21). Q8 affects orchestrator selection logic in C11 and C13. Until these are resolved, high HITL strictness is warranted and the deterministic merge ordering algorithm in C18 should be designed with extensibility for multiple conflict resolution strategies.

Evidence:
- {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /open_questions/8, note: Q9: conflict resolution for parallel patchsets, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}
- {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /open_questions/6, note: Q7: diff format for patchsets, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}
- {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /open_questions/7, note: Q8: smallest passing repair selection, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}

---

## Synthesis Warnings
- **W1 (high):** Open questions Q7, Q8, and Q9 are blocking and directly impact C18 (Integration Merge Engine), C14/C17/C21 (patchset producers), and C11/C13 (orchestrator selection logic). Phase 3 design cannot be finalized until these are resolved.
  - routing_target: clarification
  - Evidence:
    - {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /open_questions/6, note: Q7 blocking: diff format, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}
    - {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /open_questions/7, note: Q8 blocking: smallest passing repair selection, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}
    - {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /open_questions/8, note: Q9 blocking: conflict resolution for parallel patchsets, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}

- **W2 (medium):** Assumptions A6 (workspace backend namespacing/access control) and A8 (tool sandbox granularity) have low confidence and are critical for O2's per-lane workspace partitions and tool permission enforcement. If these assumptions prove false, the parallel lane isolation model may need redesign.
  - routing_target: clarification
  - Evidence:
    - {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /assumptions/5, note: A6: workspace backend capabilities — low confidence, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}
    - {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /assumptions/7, note: A8: tool sandbox granularity — low confidence, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}

- **W3 (medium):** Assumption A10 (deterministic diff application across merge conflicts/rebase) has low confidence and is foundational to C18's merge ordering algorithm. If diff application is not deterministic across conflict scenarios, the integration sub-flow requires a fundamentally different approach.
  - routing_target: clarification
  - Evidence:
    - {artifact_ref: capsule_canonical_v1, entity_id: null, json_pointer: /assumptions/9, note: A10: deterministic diff application across merge conflicts — low confidence, candidate_id: B2B, artifact_id: capsule_canonical, quote: null}

---

## Appendix: Glossary
- **Implementation Council:** A CouncilOS subsystem that consumes a frozen planning output (plan_package_final.json) and produces audited, reproducible repository changes and release artifacts under orchestrator-controlled policies and validators.
- **Implementation Freeze Bundle:** The canonical, typed/versioned set of JSON artifacts emitted at freeze, including run manifest, event log, work plan, patchsets, repo snapshot, test/quality/trace/review/release artifacts, decision record, and required workspace artifacts (workspace_index, context_packs, stage_handoffs).
- **Shared Workspace:** A tool-accessible, versioned and auditable storage area treated as an extension of the artifact system, containing canonical JSON artifacts and explicitly non-canonical notes/summaries, governed by permissions, retention, and checkpoint/resume handoffs.
- **Evidence Pointer:** An RFC 6901 JSON Pointer object {artifact_ref, entity_id|null, json_pointer, note} referencing a versioned JSON artifact location; used as the sole standard for traceability across decisions, findings, and state representations.
- **Stage Handoff:** A required JSON artifact emitted at each checkpoint boundary capturing resumable state: objectives, completed work, pending blockers, next actions, and pointers into canonical artifacts (work plan, patchsets, validation outputs).
- **ChangeRequest:** A canonical artifact proposed when plan semantics would need to change; requires explicit decisioning and policy-triggered escalation (HITL and/or reroute upstream) before any semantic deviation is allowed.
- **Orchestrator:** Deterministic code component with sole authority over routing, budgets, iteration limits, termination, checkpoint/resume, tool permissions, selection logic, and validator enforcement.
- **Context Pack:** A bounded, deterministically-compiled set of workspace artifacts provided to a role at a stage, selected by explicit rules and cited via evidence pointers.
- **Patchset:** A JSON artifact embedding code diffs with deterministic apply order for repo modifications; used as the sole mechanism for code repair/changes in the workflow.
- **Validator:** A code-owned deterministic gate that checks artifact integrity/completeness and quality criteria to allow stage transitions or freeze.

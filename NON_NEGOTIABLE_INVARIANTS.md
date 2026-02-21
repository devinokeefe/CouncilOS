**CouncilOS vNext (One Sentence)**
CouncilOS vNext turns a vague brief into a versioned frozen plan and then executes it via persisted execution contracts (WorkGraph + VerificationPlan), completing milestones only when required expectations are PASS with linked evidence, and evolving the plan only via ChangeRequests -> new frozen plan versions with lineage.

**Authority Model (3 Roots + Receipt)**
- Semantic authority: `plan_package_final_vN.json` defines intent, scope, constraints, and acceptance requirements.
- Seal/binding authority: `plan_freeze_record.json` binds plan hash, selected draft, approvals, snapshot refs, and hash spec.
- Transport/index authority: `handoff_manifest.json` lists required artifacts, hashes, and repo acquisition specs.
- Acceptance receipt: `handoff_ack.json` records what implementation accepted and acquired.

**Non-Negotiable Invariants**
- Single semantic authority: `plan_package_final_vN.json` is the only file that defines intent, scope, constraints, and acceptance requirements.
- Freeze is hash-bound to a specific draft: freeze must reference `selected_draft_ref` and approvals must match that exact plan hash.
- Deterministic hashing everywhere: all integrity checks use one `hash_spec.json` and one canonicalization implementation.
- Handoff is deterministic and verifiable: implementation must verify manifest digest, required artifact hashes, and repo baseline tree hash (plus env snapshot if required).
- Plan to execution contracts are compiled and persisted: `work_graph.json`, `verification_plan.json`, `execution_plan.json` are deterministic outputs and are reused when inputs match.
- Done is evidence-gated: milestone completion requires MUST expectations to be PASS with evidence refs and `decision_record.decision == PASS`.
- No deviation without change control: violations emit `change_request_vN.json` and stop unless explicitly allowed.

**Authority Model**
- Semantic authority: `plan_package_final_vN.json` authored by planning.
- Seal/binding authority: `plan_freeze_record.json` binds plan hash, selected draft, approvals, and snapshot refs.
- Transport/index authority: `handoff_manifest.json` lists required artifacts, hashes, and repo acquisition spec.
- Acceptance receipt: `handoff_ack.json` records what implementation accepted and acquired.

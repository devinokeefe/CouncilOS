from __future__ import annotations

from dataclasses import dataclass

from council_os.implementation.schemas import (
    ProgramGraphPayload,
    RepoContextPayloadV21,
    RepoValidatorEntrypoint,
    VerificationCheckRequirement,
    VerificationEvidenceRequirement,
    VerificationPlanPayload,
    VerificationSuite,
)


@dataclass(frozen=True)
class _SuiteSpec:
    suite_id: str
    env_class: str
    runner: str
    maps_to_acceptance: set[str]


class VerificationPlanCompiler:
    def _derive_suite(self, validator: RepoValidatorEntrypoint) -> _SuiteSpec:
        name = f"{validator.id} {validator.name}".lower()
        suite_id = "unit"
        env_class = "local"
        if any(key in name for key in ("security", "sast", "vuln")):
            suite_id = "security"
            env_class = "local"
        elif "secrets" in name:
            suite_id = "secrets"
            env_class = "local"
        elif "sbom" in name:
            suite_id = "sbom"
            env_class = "local"
        elif "scaled" in name or "scale" in name:
            suite_id = "remote_scaled"
            env_class = "scaled"
        elif "e2e" in name:
            suite_id = "e2e"
            env_class = "staging"
        elif "contract" in name:
            suite_id = "contract"
            env_class = "local"
        elif "load" in name:
            suite_id = "load"
            env_class = "scaled"
        elif "canary" in name:
            suite_id = "remote_canary"
            env_class = "canary"
        elif "remote" in name:
            suite_id = "remote_op"
            env_class = "remote"
        maps = set(validator.maps_to_acceptance_tests or [])
        return _SuiteSpec(suite_id=suite_id, env_class=env_class, runner=validator.name, maps_to_acceptance=maps)

    def compile(self, program_graph: ProgramGraphPayload, repo_context: RepoContextPayloadV21) -> VerificationPlanPayload:
        checks: list[VerificationCheckRequirement] = []
        runner_name = repo_context.validator_entrypoints[0].name if repo_context.validator_entrypoints else "validator"
        derived_suites = [self._derive_suite(v) for v in repo_context.validator_entrypoints]
        for node in program_graph.nodes:
            if node.type != "check":
                continue
            is_acceptance = node.id.startswith("CHECK-AT-")
            suite_id = "acceptance" if is_acceptance else "unit"
            suites: list[VerificationSuite] = [
                VerificationSuite(suite_id=suite_id, env_class="local", runner=runner_name, gating="must")
            ]
            test_id = node.id.replace("CHECK-AT-", "", 1) if is_acceptance else None
            candidates: list[_SuiteSpec] = []
            for suite in derived_suites:
                if suite.maps_to_acceptance:
                    if not is_acceptance or (test_id not in suite.maps_to_acceptance):
                        continue
                candidates.append(suite)
            candidates.sort(key=lambda item: (item.suite_id, item.runner))
            seen = {suite_id}
            for suite in candidates:
                if suite.suite_id in seen:
                    continue
                suites.append(
                    VerificationSuite(
                        suite_id=suite.suite_id,
                        env_class=suite.env_class,
                        runner=suite.runner,
                        gating="must",
                    )
                )
                seen.add(suite.suite_id)
            evidence = [
                VerificationEvidenceRequirement(artifact_type=req.artifact_type, env_class=req.env_class)
                for req in node.required_evidence
            ]
            checks.append(
                VerificationCheckRequirement(
                    program_check_id=node.id,
                    required_suites=suites,
                    evidence_requirements=evidence,
                )
            )
        return VerificationPlanPayload(schema_version="2.1.0", verification_plan_id="verification_plan_default", check_requirements=checks)

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import jsonpatch
from pydantic import BaseModel, ValidationError

from council_os.agents.prompts.library import PromptLibrary
from council_os.agents.providers.base import QuotaExceededError
from council_os.agents.providers.openrouter import OpenRouterProvider
from council_os.agents.roles import RoleConfig
from council_os.agents.runtime import AgentRuntime, RuntimeErrorException, _strict_json_schema
from council_os.agents.schemas import (
    SCHEMA_VERSION,
    AcceptanceCanonicalPayload,
    AcceptanceDraftPayload,
    AcceptanceTest,
    AlignmentWarning,
    AlignmentWarningsPayload,
    ArbitrationDecisionPayload,
    ArchitectureCanonicalPayload,
    ArchitectureMergePayload,
    ArchitectureOption,
    ArchitectureSection,
    ArtifactEnvelope,
    Assumption,
    BranchSetPayload,
    BranchSpec,
    CandidateDeltaPayload,
    CandidateInputs,
    ClarificationItem,
    ClarificationPlanPayload,
    Component,
    ConsistencyFinding,
    ConsistencyFindingsPayload,
    DecisionRecordPayload,
    FailureModeFindingItem,
    FailureModeFindingsPayload,
    FreezeRecordPayload,
    FreezeTradeoff,
    GovernanceSection,
    HitlPolicy,
    Interface,
    JudgePairwisePayload,
    MarkdownRenderPayload,
    PatchApplyCandidateResult,
    PatchApplyResultPayload,
    PatchOrReroutePayload,
    PlanCandidatePayload,
    PlanFrozenPayload,
    PlanMeta,
    PlanPackage,
    ProjectCapsulePayload,
    QaStrategyCanonicalPayload,
    QaTemplatesDraftPayload,
    RequirementsCanonicalPayload,
    RequirementsDraftPayload,
    RequirementsMergePayload,
    RiskGovCanonicalPayload,
    RiskGovMergePayload,
    RiskItem,
    RunMetricsPayload,
    ToolPolicy,
    ToolPolicyStage,
    TraceGraphFinding,
    TraceGraphFindingsPayload,
    Tradeoffs,
    TriageDefect,
    TriageFindingsPayload,
    ValidatorReportPayload,
    export_schema_files,
    schema_model_for,
)
from council_os.artifacts.store import ArtifactExistsError, ArtifactNotFoundError, ArtifactStore
from council_os.audit.event_log import append_event, new_event, read_last_event
from council_os.audit.manifest import ManifestInput, create_manifest
from council_os.orchestrator.checkpoints import (
    Checkpoint,
    CheckpointState,
    latest_checkpoint,
    list_checkpoints,
    load_checkpoint,
    write_checkpoint,
)
from council_os.orchestrator.locks import run_lock, update_run_lock
from council_os.orchestrator.feedback.config import FeedbackConfig
from council_os.orchestrator.feedback.gates import HumanFeedbackGate, NeedsUserInput
from council_os.orchestrator.feedback.human_input import AutoProvider, CLIProvider, HumanInputProvider
from council_os.orchestrator.feedback.plan_review import run_plan_review_loop
from council_os.orchestrator.feedback import event_log as feedback_event_log
from council_os.orchestrator.feedback.schemas import (
    ClarificationQuestions,
    ClarificationResponses,
    ClarificationResolutions,
    PrePlanTriage,
)
from council_os.orchestrator.feedback.store import PlanningArtifactStore
from council_os.orchestrator.feedback.triage import build_clarification_questions, run_preplan_triage
from council_os.orchestrator.feedback.utils import plan_hash
from council_os.validation.validators import validate_candidate

PROMPT_LABELS: dict[str, tuple[str, str]] = {
    "capsule_normalizer_v3": ("CAPSULE_NORMALIZER_SYSTEM", "CAPSULE_NORMALIZER_USER"),
    "capsule_reconciler_v1": ("CAPSULE_RECONCILER_SYSTEM", "CAPSULE_RECONCILER_USER"),
    "clarification_gate_v3": ("CLARIFICATION_GATE_SYSTEM", "CLARIFICATION_GATE_USER"),
    "requirements_drafter_v3": ("REQ_POD_SYSTEM", "REQ_POD_USER"),
    "arch_options_drafter_v3": ("ARCH_POD_SYSTEM", "ARCH_POD_USER"),
    "qa_templates_drafter_v3": ("QA_POD_SYSTEM", "QA_POD_USER"),
    "risk_gov_drafter_v3": ("RISK_POD_SYSTEM", "RISK_POD_USER"),
    "requirements_merger_v3": ("REQ_MERGE_SYSTEM", "REQ_MERGE_USER"),
    "arch_merger_v3": ("ARCH_MERGE_SYSTEM", "ARCH_MERGE_USER"),
    "risk_gov_merger_v3": ("RISK_MERGE_SYSTEM", "RISK_MERGE_USER"),
    "qa_strategy_merger_v1": ("QA_STRATEGY_MERGER_SYSTEM", "QA_STRATEGY_MERGER_USER"),
    "acceptance_generator_v3": ("ACCEPTANCE_GEN_SYSTEM", "ACCEPTANCE_GEN_USER"),
    "alignment_auditor_v3": ("ALIGNMENT_AUDITOR_SYSTEM", "ALIGNMENT_AUDITOR_USER"),
    "branch_builder_v3": ("BRANCH_BUILDER_SYSTEM", "BRANCH_BUILDER_USER"),
    "candidate_delta_synthesizer_v3": (
        "CANDIDATE_DELTA_SYNTHESIZER_SYSTEM",
        "CANDIDATE_DELTA_SYNTHESIZER_USER",
    ),
    "consistency_auditor_v3": ("CONSISTENCY_AUDITOR_SYSTEM", "CONSISTENCY_AUDITOR_USER"),
    "trace_graph_auditor_v3": ("TRACE_GRAPH_AUDITOR_SYSTEM", "TRACE_GRAPH_AUDITOR_USER"),
    "triage_critic_v3": ("TRIAGE_CRITIC_SYSTEM", "TRIAGE_CRITIC_USER"),
    "patch_generator_v3": ("PATCH_GENERATOR_SYSTEM", "PATCH_GENERATOR_USER"),
    "failure_mode_analyst_v3": ("FAILURE_MODE_ANALYST_SYSTEM", "FAILURE_MODE_ANALYST_USER"),
    "judge_pairwise_v3": ("JUDGE_PAIRWISE_SYSTEM", "JUDGE_PAIRWISE_USER"),
    "arbitrator_v3": ("ARBITRATOR_SYSTEM", "ARBITRATOR_USER"),
    "decision_record_writer_v3": ("DECISION_RECORD_SYSTEM", "DECISION_RECORD_USER"),
    "plan_renderer_v3": ("PLAN_RENDERER_SYSTEM", "PLAN_RENDERER_USER"),
}

IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
FORBIDDEN_PATCH_PREFIXES = ("/plan_package/acceptance_tests",)
_UNSET_SENTINEL = object()


@dataclass
class ModelCall:
    name: str
    model_key: str
    profile: str | None
    max_output_tokens: int | None
    overrides: dict[str, object]
    enabled: bool = True


@dataclass
class StageConfig:
    id: str
    kind: str
    prompt_id: str | None
    output_schema: str
    schema_required: bool
    tooling_allowed: str | None
    max_attempts: int
    calls: list[ModelCall]
    fallbacks: list[ModelCall]
    policy: dict[str, object]
    selection: dict[str, object]
    map_over: str | None


@dataclass
class CandidateState:
    candidate_id: str
    branch_id: str
    synthesizer_id: str
    author_model: str
    author_family: str
    delta_ref: str
    plan_ref: str
    payload: PlanCandidatePayload
    validator_ref: str | None = None
    triage_ref: str | None = None
    failure_mode_ref: str | None = None
    validator_report: ValidatorReportPayload | None = None
    triage_report: TriageFindingsPayload | None = None
    failure_mode: FailureModeFindingsPayload | None = None
    validator_status: str = "FAIL_STRUCTURAL"
    triage_blocked: bool = True
    failure_mode_pass: bool = False


@dataclass
class RunResult:
    run_id: UUID
    run_root: Path
    frozen_artifact_id: str


class HQPipeline:
    def __init__(
        self,
        storage_root: Path,
        config_path: Path,
        config_raw: str,
        config: dict[str, object],
        run_id: UUID | None = None,
        run_root: Path | None = None,
    ) -> None:
        self.storage_root = storage_root
        self.config_path = config_path
        self.config_raw = config_raw
        self.config = config
        self.run_id = run_id or uuid4()
        self.run_root = run_root or (self.storage_root / str(self.run_id))
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.prompt_library = PromptLibrary()
        self._last_event_id: str | None = None
        self._schema_version = self._schema_version_from_config()
        self._run_lock_path: Path | None = None

        self.providers = self._build_providers()
        self.runtimes = {
            name: AgentRuntime(
                provider,
                run_root=self.run_root,
                run_id=self.run_id,
                store_full_prompts=self._log_prompts,
                log_model_metadata=self._log_model_metadata,
                log_raw_model_text=self._log_raw_model_text,
            )
            for name, provider in self.providers.items()
        }
        self.stage_map = {stage["id"]: self._parse_stage(stage) for stage in self._stages_raw}
        self._stage_order = [str(stage.get("id")) for stage in self._stages_raw if stage.get("id")]
        self._stage_index = {stage_id: idx + 1 for idx, stage_id in enumerate(self._stage_order)}
        self._log_stage_transitions = bool(
            self._runtime_cfg.get("orchestrator", {}).get("log_stage_transitions", True)
        )
        self._safe_mode = bool(self._runtime_cfg.get("orchestrator", {}).get("safe_mode", False))
        self._max_parallel_llm_calls = self._resolve_max_parallel_llm_calls()
        self._llm_semaphore = threading.BoundedSemaphore(self._max_parallel_llm_calls)

    @property
    def _runtime_cfg(self) -> dict[str, object]:
        return dict(self.config.get("runtime", {}))

    @property
    def _stages_raw(self) -> list[dict[str, object]]:
        raw = self.config.get("stages", [])
        return [s for s in raw if isinstance(s, dict)]

    @property
    def _models(self) -> dict[str, dict[str, object]]:
        return dict(self.config.get("models", {}))

    @property
    def _profiles(self) -> dict[str, dict[str, object]]:
        return dict(self.config.get("profiles", {}))

    @property
    def _providers_cfg(self) -> dict[str, dict[str, object]]:
        return dict(self.config.get("providers", {}))

    @property
    def _guardrails(self) -> dict[str, object]:
        return dict(self.config.get("guardrails", {}))

    @property
    def _workflow(self) -> dict[str, object]:
        return dict(self.config.get("workflow", {}))

    @property
    def _llm_timeout_sec(self) -> float | None:
        orchestrator_cfg = self._runtime_cfg.get("orchestrator", {})
        if not isinstance(orchestrator_cfg, dict):
            return None
        raw = orchestrator_cfg.get("llm_timeout_sec")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    @property
    def _checkpoint_candidates(self) -> bool:
        orchestrator_cfg = self._runtime_cfg.get("orchestrator", {})
        if not isinstance(orchestrator_cfg, dict):
            return False
        return bool(orchestrator_cfg.get("checkpoint_candidates", False))

    @property
    def _checkpoint_repair_rounds(self) -> bool:
        orchestrator_cfg = self._runtime_cfg.get("orchestrator", {})
        if not isinstance(orchestrator_cfg, dict):
            return False
        return bool(orchestrator_cfg.get("checkpoint_repair_rounds", False))

    @property
    def _judge_policy(self) -> dict[str, object]:
        return dict(self.config.get("judge_policy", {}))

    @property
    def _log_prompts(self) -> bool:
        audit_cfg = self._runtime_cfg.get("audit", {})
        return bool(audit_cfg.get("log_prompts", False))

    @property
    def _log_model_metadata(self) -> bool:
        audit_cfg = self._runtime_cfg.get("audit", {})
        return bool(audit_cfg.get("log_model_metadata", True))

    @property
    def _log_raw_model_text(self) -> bool:
        audit_cfg = self._runtime_cfg.get("audit", {})
        return bool(audit_cfg.get("log_raw_model_text", False))

    def _resolve_max_parallel_llm_calls(self) -> int:
        orchestrator_cfg = self._runtime_cfg.get("orchestrator", {})
        if isinstance(orchestrator_cfg, dict):
            concurrency_cfg = orchestrator_cfg.get("concurrency", {})
        else:
            concurrency_cfg = {}
        if isinstance(concurrency_cfg, dict):
            value = concurrency_cfg.get("max_parallel_llm_calls")
            if isinstance(value, int) and value > 0:
                return value
        return 4

    def _run_parallel_tasks(
        self,
        tasks: list[tuple[int, Callable[[], object]]],
    ) -> tuple[dict[int, object], list[tuple[int, Exception]]]:
        results: dict[int, object] = {}
        errors: list[tuple[int, Exception]] = []
        if not tasks:
            return results, errors
        max_workers = min(self._max_parallel_llm_calls, len(tasks))
        if max_workers <= 1:
            for idx, task in tasks:
                try:
                    results[idx] = task()
                except Exception as exc:
                    errors.append((idx, exc))
            return results, errors
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(task): idx for idx, task in tasks}
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    errors.append((idx, exc))
        return results, errors

    def _build_providers(self) -> dict[str, object]:
        providers: dict[str, object] = {}
        for name, cfg in self._providers_cfg.items():
            adapter = str(cfg.get("adapter", ""))
            if adapter == "openrouter":
                api_key = _env_value(str(cfg.get("api_key_env", "OPENROUTER_API_KEY")))
                base_url = _env_value(str(cfg.get("base_url_env", "OPENROUTER_BASE_URL"))) or (
                    str(cfg.get("base_url")) if cfg.get("base_url") else None
                )
                http_referer = _env_value(str(cfg.get("http_referer_env", "OPENROUTER_HTTP_REFERER"))) or (
                    str(cfg.get("http_referer")) if cfg.get("http_referer") else None
                )
                app_title = _env_value(str(cfg.get("app_title_env", "OPENROUTER_APP_TITLE"))) or (
                    str(cfg.get("app_title")) if cfg.get("app_title") else None
                )
                providers[name] = OpenRouterProvider(
                    api_key=api_key,
                    base_url=base_url,
                    http_referer=http_referer,
                    app_title=app_title,
                )
            else:
                raise ValueError(f"Unsupported provider adapter: {adapter}")
        return providers

    def _validate_credentials(self) -> None:
        used_providers = {str(cfg.get("provider", "")) for cfg in self._models.values() if isinstance(cfg, dict)}
        missing: set[str] = set()
        for provider_name in used_providers:
            cfg = self._providers_cfg.get(provider_name)
            if not isinstance(cfg, dict):
                continue
            adapter = str(cfg.get("adapter", ""))
            if adapter == "openrouter":
                env_name = str(cfg.get("api_key_env", "")).strip()
                if env_name and _env_value(env_name) is None:
                    missing.add(env_name)
            else:
                raise RuntimeError(f"Unsupported provider adapter: {adapter}")
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise RuntimeError(f"Missing required environment variables for HQ config: {missing_list}")

    def _parse_stage(self, raw: dict[str, object]) -> StageConfig:
        calls = [self._parse_call(item) for item in raw.get("calls", []) if isinstance(item, dict)]
        fallbacks = [self._parse_call(item) for item in raw.get("fallbacks", []) if isinstance(item, dict)]
        return StageConfig(
            id=str(raw.get("id")),
            kind=str(raw.get("kind")),
            prompt_id=str(raw.get("prompt_id")) if raw.get("prompt_id") else None,
            output_schema=str(raw.get("output_schema")),
            schema_required=bool(raw.get("schema_required", False)),
            tooling_allowed=str(raw.get("tooling_allowed")) if raw.get("tooling_allowed") else None,
            max_attempts=int(raw.get("max_attempts", self._runtime_default_max_attempts())),
            calls=calls,
            fallbacks=fallbacks,
            policy=dict(raw.get("policy", {})) if isinstance(raw.get("policy", {}), dict) else {},
            selection=dict(raw.get("selection", {})) if isinstance(raw.get("selection", {}), dict) else {},
            map_over=str(raw.get("map_over")) if raw.get("map_over") else None,
        )

    def _parse_call(self, raw: dict[str, object]) -> ModelCall:
        enabled = True
        enabled_if = raw.get("enabled_if")
        if isinstance(enabled_if, str) and enabled_if:
            enabled = bool(_resolve_config_path(self.config, enabled_if))
        return ModelCall(
            name=str(raw.get("name", "")),
            model_key=str(raw.get("model")),
            profile=str(raw.get("profile")) if raw.get("profile") else None,
            max_output_tokens=int(raw.get("max_output_tokens")) if raw.get("max_output_tokens") else None,
            overrides=dict(raw.get("overrides", {})) if isinstance(raw.get("overrides", {}), dict) else {},
            enabled=enabled,
        )

    def _runtime_default_max_attempts(self) -> int:
        orchestrator_cfg = self._runtime_cfg.get("orchestrator", {})
        return int(orchestrator_cfg.get("default_max_attempts_per_llm_call", 2))

    def _schema_version_from_config(self) -> str:
        configured = str(self.config.get("schemas_version", "")).strip()
        if not configured:
            # Backward compatibility for snapshots created before schemas_version was required.
            return SCHEMA_VERSION
        if configured != SCHEMA_VERSION:
            raise ValueError(
                f"schemas_version mismatch: config={configured}, runtime={SCHEMA_VERSION}. "
                "Update config or runtime schema version."
            )
        return configured

    def _schema_dir(self) -> Path:
        artifacts_cfg = self._runtime_cfg.get("artifacts", {})
        schema_dir = str(artifacts_cfg.get("schema_dir", "schemas"))
        base = self.config_path.parent
        return (base / schema_dir).resolve()

    def _store(self) -> ArtifactStore:
        return ArtifactStore(self.storage_root, self.run_id)

    def _candidate_metadata(self, state: CandidateState) -> dict[str, object]:
        return {
            "candidate_id": state.candidate_id,
            "branch_id": state.branch_id,
            "synthesizer_id": state.synthesizer_id,
            "author_model": state.author_model,
            "author_family": state.author_family,
            "delta_ref": state.delta_ref,
            "plan_ref": state.plan_ref,
            "validator_ref": state.validator_ref,
            "triage_ref": state.triage_ref,
            "failure_mode_ref": state.failure_mode_ref,
        }

    def _expected_candidate_ids(self, branch_set_payload: dict[str, object]) -> list[str]:
        stage = self.stage_map["synthesis.candidate_delta"]
        branches = BranchSetPayload.model_validate(branch_set_payload).branches
        expected: list[str] = []
        for branch in branches:
            for call_idx, call in enumerate(stage.calls):
                if not call.enabled:
                    continue
                expected.append(f"{branch.branch_id}{chr(ord('A') + call_idx)}")
        return expected

    def _candidate_version(self, plan_ref: str) -> int:
        match = re.match(r"^plan_candidate_(.+)_v(\d+)$", plan_ref)
        if not match:
            return 1
        return int(match.group(2))

    def _artifact_version(self, artifact_ref: str) -> int:
        match = re.match(r".+_v(\d+)$", artifact_ref)
        if not match:
            return 1
        return int(match.group(1))

    def _normalize_tool_policy(self, tool_policy: ToolPolicy) -> ToolPolicy:
        def norm_stage(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", value.lower())

        canonical = {
            "intake": "Intake",
            "generate": "Generate",
            "validate": "Validate",
            "review": "Review",
            "judge": "Judge",
            "freeze": "Freeze",
        }
        aliases = {
            "workplanning": "Generate",
            "plan": "Generate",
            "patchapply": "Generate",
            "patch": "Generate",
            "integrateandvalidate": "Validate",
            "integrate": "Validate",
        }
        stage_map = {**canonical, **aliases}
        merged: dict[str, ToolPolicyStage] = {}

        def merge_stage(target: str, stage: ToolPolicyStage) -> None:
            existing = merged.get(target)
            if existing is None:
                merged[target] = ToolPolicyStage(
                    stage=target,
                    allowlisted_tools=list(stage.allowlisted_tools),
                    restrictions=[dict(r) for r in stage.restrictions],
                    hitl_triggers=list(stage.hitl_triggers),
                )
                return
            allowlisted = list(existing.allowlisted_tools)
            for tool in stage.allowlisted_tools:
                if tool not in allowlisted:
                    allowlisted.append(tool)
            existing_restrictions = [dict(r) for r in existing.restrictions]
            seen = {json.dumps(r, sort_keys=True) for r in existing_restrictions}
            for restriction in stage.restrictions:
                key = json.dumps(restriction, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                existing_restrictions.append(dict(restriction))
            triggers = list(existing.hitl_triggers)
            for trigger in stage.hitl_triggers:
                if trigger not in triggers:
                    triggers.append(trigger)
            merged[target] = ToolPolicyStage(
                stage=target,
                allowlisted_tools=allowlisted,
                restrictions=existing_restrictions,
                hitl_triggers=triggers,
            )

        for stage in tool_policy.stage_policies:
            normalized = norm_stage(stage.stage)
            target = stage_map.get(normalized, stage.stage)
            merge_stage(target, stage)

        def ensure_artifact_write(stage_name: str, note: str) -> None:
            stage = merged.get(stage_name)
            if stage is None:
                return
            allowlisted = list(stage.allowlisted_tools)
            if "artifact_write" not in allowlisted:
                allowlisted.append("artifact_write")
            restrictions = [dict(r) for r in stage.restrictions]
            updated = False
            has_artifact_write = False
            for restriction in restrictions:
                if "artifact_write" not in restriction:
                    continue
                has_artifact_write = True
                value = str(restriction.get("artifact_write", "")).lower()
                if value in {"deny", "allow_noncanonical_only"}:
                    restriction["artifact_write"] = "canonical_allowed_only_for_declared_types"
                    updated = True
            if not has_artifact_write:
                restrictions.append(
                    {
                        "artifact_write": "canonical_allowed_only_for_declared_types",
                        "notes": note,
                    }
                )
                updated = True
            if updated or allowlisted != stage.allowlisted_tools:
                merged[stage_name] = ToolPolicyStage(
                    stage=stage_name,
                    allowlisted_tools=allowlisted,
                    restrictions=restrictions,
                    hitl_triggers=stage.hitl_triggers,
                )

        ensure_artifact_write(
            "Intake",
            "Allow canonical audit-spine artifacts (run_manifest, event_log, stage_handoff).",
        )
        ensure_artifact_write(
            "Review",
            "Allow findings-only canonical artifacts (review_findings.json).",
        )

        ordered: list[ToolPolicyStage] = []
        for name in ("Intake", "Generate", "Validate", "Review", "Judge", "Freeze"):
            stage = merged.pop(name, None)
            if stage is not None:
                ordered.append(stage)
        for stage in merged.values():
            ordered.append(stage)
        return ToolPolicy(stage_policies=ordered)

    def _align_architecture_risks(
        self,
        arch_payload: dict[str, object],
        risk_payload: dict[str, object],
        *,
        current_ref: str,
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope | None:
        try:
            arch = ArchitectureCanonicalPayload.model_validate(arch_payload)
            risk = RiskGovCanonicalPayload.model_validate(risk_payload)
        except ValidationError:
            return None

        def norm(text: str) -> str:
            return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()

        desc_map: dict[str, str] = {}
        for item in risk.risk_register:
            key = norm(item.description)
            if key and key not in desc_map:
                desc_map[key] = item.id

        def match_risk(value: str) -> str | None:
            value = value.strip()
            if not value:
                return None
            if re.match(r"^K\d+$", value):
                return value
            id_match = re.search(r"K\d+", value)
            if id_match:
                return id_match.group(0)
            key = norm(value)
            if key in desc_map:
                return desc_map[key]
            if key:
                matches = {rid for desc, rid in desc_map.items() if key in desc or desc in key}
                if len(matches) == 1:
                    return next(iter(matches))
            return None

        changed = False
        updated_options: list[ArchitectureOption] = []
        for option in arch.options:
            new_risks: list[str] = []
            for entry in option.risks:
                mapped = match_risk(entry)
                if mapped is not None:
                    if mapped != entry:
                        changed = True
                    new_risks.append(mapped)
                else:
                    new_risks.append(entry)
            if new_risks != option.risks:
                changed = True
                option = option.model_copy(update={"risks": new_risks})
            updated_options.append(option)

        if not changed:
            return None

        new_version = self._artifact_version(current_ref) + 1
        payload = ArchitectureCanonicalPayload(
            options=updated_options,
            option_id_map=arch.option_id_map,
            component_id_map=arch.component_id_map,
            interface_id_map=arch.interface_id_map,
            merge_log=arch.merge_log,
        )
        env = ArtifactEnvelope(
            artifact_type="architecture_canonical",
            artifact_id=f"architecture_canonical_v{new_version}",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[current_ref, artifact_refs.get("risk_gov_canonical", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage="canonicalize.architecture")
        return env

    def _find_candidate_artifact_ref(
        self,
        store: ArtifactStore,
        artifact_type: str,
        *,
        candidate_id: str,
        prefix: str,
        preferred_version: int | None = None,
    ) -> str | None:
        best_version = -1
        best_ref: str | None = None
        preferred_ref: str | None = None
        pattern = re.compile(rf"^{re.escape(prefix)}_{re.escape(candidate_id)}_v(\d+)$")
        for env in store.list_artifacts(artifact_type):
            match = pattern.match(env.artifact_id)
            if not match:
                continue
            version = int(match.group(1))
            if preferred_version is not None and version == preferred_version:
                preferred_ref = env.artifact_id
            if version > best_version:
                best_version = version
                best_ref = env.artifact_id
        if preferred_ref:
            return preferred_ref
        return best_ref

    def _candidate_passes(self, candidate: CandidateState) -> bool:
        return (
            candidate.validator_status == "PASS"
            and not candidate.triage_blocked
            and candidate.failure_mode_pass
        )

    def _next_candidate_version(
        self, candidates: dict[str, CandidateState], candidate_id: str
    ) -> int:
        current = candidates.get(candidate_id)
        if not current:
            return 1
        return self._candidate_version(current.plan_ref) + 1

    def _restore_candidates_from_metadata(
        self,
        store: ArtifactStore,
        candidate_meta: list[object],
    ) -> tuple[dict[str, CandidateState], dict[str, object]]:
        candidates: dict[str, CandidateState] = {}
        summaries: list[dict[str, object]] = []
        for entry in candidate_meta:
            if not isinstance(entry, dict):
                continue
            candidate_id = str(entry.get("candidate_id", "")).strip()
            plan_ref = str(entry.get("plan_ref", "")).strip()
            if not candidate_id or not plan_ref:
                continue
            plan_env = self._read_artifact(store, plan_ref)
            if plan_env is None or plan_env.artifact_type != "plan_candidate":
                continue

            version = self._candidate_version(plan_ref)
            delta_ref = str(entry.get("delta_ref", "")).strip()
            if not delta_ref:
                delta_ref = self._find_candidate_artifact_ref(
                    store,
                    "candidate_delta",
                    candidate_id=candidate_id,
                    prefix="candidate_delta",
                    preferred_version=version,
                ) or f"candidate_delta_{candidate_id}_v{version}"

            validator_ref = str(entry.get("validator_ref", "")).strip() or f"validator_{candidate_id}_v{version}"

            triage_ref = str(entry.get("triage_ref", "")).strip()
            if not triage_ref:
                triage_ref = self._find_candidate_artifact_ref(
                    store,
                    "triage_report",
                    candidate_id=candidate_id,
                    prefix="triage",
                    preferred_version=version,
                ) or f"triage_{candidate_id}_v{version}"

            failure_ref = str(entry.get("failure_mode_ref", "")).strip()
            if not failure_ref:
                failure_ref = self._find_candidate_artifact_ref(
                    store,
                    "failure_mode_findings",
                    candidate_id=candidate_id,
                    prefix="failure_modes",
                    preferred_version=version,
                ) or f"failure_modes_{candidate_id}_v{version}"

            validator_env = self._read_artifact(store, validator_ref)
            triage_env = self._read_artifact(store, triage_ref)
            failure_env = self._read_artifact(store, failure_ref)
            if validator_env is None or triage_env is None or failure_env is None:
                continue

            payload = PlanCandidatePayload.model_validate(plan_env.payload)
            validator_report = ValidatorReportPayload.model_validate(validator_env.payload)
            triage_report = TriageFindingsPayload.model_validate(triage_env.payload)
            failure_mode = FailureModeFindingsPayload.model_validate(failure_env.payload)
            triage_blocked = triage_report.verdict == "reject" or any(
                d.label == "blocker" for d in triage_report.defects
            )
            triage_version = self._artifact_version(triage_ref)
            if triage_version < version and validator_report.overall_status == "PASS":
                triage_blocked = False

            state = CandidateState(
                candidate_id=candidate_id,
                branch_id=str(entry.get("branch_id", payload.branch_id)),
                synthesizer_id=str(entry.get("synthesizer_id", payload.synthesizer_id)),
                author_model=str(entry.get("author_model", "")) or "unknown",
                author_family=str(entry.get("author_family", "")) or "unknown",
                delta_ref=delta_ref,
                plan_ref=plan_ref,
                payload=payload,
                validator_ref=validator_ref,
                triage_ref=triage_ref,
                failure_mode_ref=failure_ref,
                validator_report=validator_report,
                triage_report=triage_report,
                failure_mode=failure_mode,
                validator_status=validator_report.overall_status,
                triage_blocked=triage_blocked,
                failure_mode_pass=bool(failure_mode.closure_pass),
            )
            candidates[candidate_id] = state
            summaries.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_ref": plan_ref,
                    "branch_id": state.branch_id,
                    "author_model": state.author_model,
                    "validator_status": state.validator_status,
                    "triage_blocked": state.triage_blocked,
                    "failure_mode_pass": state.failure_mode_pass,
                }
            )
        if not candidates:
            return {}, {"summaries": []}
        return candidates, {"summaries": summaries}

    def _restore_candidates_from_store(
        self,
        store: ArtifactStore,
    ) -> tuple[dict[str, CandidateState], dict[str, object]]:
        candidates: dict[str, CandidateState] = {}
        summaries: list[dict[str, object]] = []
        latest_by_candidate: dict[str, tuple[int, ArtifactEnvelope]] = {}
        for env in store.list_artifacts("plan_candidate"):
            match = re.match(r"^plan_candidate_(.+)_v(\d+)$", env.artifact_id)
            if match:
                candidate_id = match.group(1)
                version = int(match.group(2))
            else:
                candidate_id = env.payload.get("candidate_id") if isinstance(env.payload, dict) else None
                candidate_id = str(candidate_id or "")
                version = 1
            if not candidate_id:
                continue
            current = latest_by_candidate.get(candidate_id)
            if current is None or version > current[0]:
                latest_by_candidate[candidate_id] = (version, env)

        for candidate_id, (version, env) in latest_by_candidate.items():
            plan_ref = env.artifact_id
            payload = PlanCandidatePayload.model_validate(env.payload)
            validator_ref = f"validator_{candidate_id}_v{version}"
            validator_env = self._read_artifact(store, validator_ref)
            if validator_env is None:
                fallback_validator_ref = self._find_candidate_artifact_ref(
                    store,
                    "validator_report",
                    candidate_id=candidate_id,
                    prefix="validator",
                    preferred_version=version,
                )
                if fallback_validator_ref:
                    validator_ref = fallback_validator_ref
                    validator_env = self._read_artifact(store, validator_ref)

            triage_ref = self._find_candidate_artifact_ref(
                store,
                "triage_report",
                candidate_id=candidate_id,
                prefix="triage",
                preferred_version=version,
            ) or f"triage_{candidate_id}_v{version}"
            failure_ref = self._find_candidate_artifact_ref(
                store,
                "failure_mode_findings",
                candidate_id=candidate_id,
                prefix="failure_modes",
                preferred_version=version,
            ) or f"failure_modes_{candidate_id}_v{version}"

            triage_env = self._read_artifact(store, triage_ref)
            failure_env = self._read_artifact(store, failure_ref)
            if validator_env is None or triage_env is None or failure_env is None:
                continue

            validator_report = ValidatorReportPayload.model_validate(validator_env.payload)
            triage_report = TriageFindingsPayload.model_validate(triage_env.payload)
            failure_mode = FailureModeFindingsPayload.model_validate(failure_env.payload)
            triage_blocked = triage_report.verdict == "reject" or any(
                d.label == "blocker" for d in triage_report.defects
            )
            triage_version = self._artifact_version(triage_ref)
            if triage_version < version and validator_report.overall_status == "PASS":
                triage_blocked = False

            state = CandidateState(
                candidate_id=candidate_id,
                branch_id=payload.branch_id,
                synthesizer_id=payload.synthesizer_id,
                author_model="unknown",
                author_family="unknown",
                delta_ref=self._find_candidate_artifact_ref(
                    store,
                    "candidate_delta",
                    candidate_id=candidate_id,
                    prefix="candidate_delta",
                    preferred_version=version,
                )
                or f"candidate_delta_{candidate_id}_v{version}",
                plan_ref=plan_ref,
                payload=payload,
                validator_ref=validator_ref,
                triage_ref=triage_ref,
                failure_mode_ref=failure_ref,
                validator_report=validator_report,
                triage_report=triage_report,
                failure_mode=failure_mode,
                validator_status=validator_report.overall_status,
                triage_blocked=triage_blocked,
                failure_mode_pass=bool(failure_mode.closure_pass),
            )
            candidates[candidate_id] = state
            summaries.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_ref": plan_ref,
                    "branch_id": state.branch_id,
                    "author_model": state.author_model,
                    "validator_status": state.validator_status,
                    "triage_blocked": state.triage_blocked,
                    "failure_mode_pass": state.failure_mode_pass,
                }
            )
        if not candidates:
            return {}, {"summaries": []}
        return candidates, {"summaries": summaries}

    def _event(
        self,
        stage: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        refs: dict[str, str] | None = None,
    ) -> str:
        ev = new_event(
            run_id=self.run_id,
            stage=stage,
            actor={"kind": "orchestrator", "role": "hq_pipeline"},
            event_type=event_type,  # type: ignore[arg-type]
            refs=refs,
            payload=payload or {},
        )
        append_event(self.run_root, ev)
        event_id = str(ev.event_id)
        self._last_event_id = event_id
        return event_id

    def _stage_index_value(self, stage_id: str) -> int:
        try:
            return self._stage_order.index(stage_id)
        except ValueError:
            return -1

    def _read_artifact(self, store: ArtifactStore, artifact_id: str) -> ArtifactEnvelope | None:
        try:
            env = store.read_artifact(artifact_id)
            if env.schema_version != self._schema_version:
                raise RuntimeError(
                    f"Artifact schema mismatch for {artifact_id}: {env.schema_version} != {self._schema_version}"
                )
            return env
        except ArtifactNotFoundError:
            return None

    def _verify_checkpoint_artifacts(self, store: ArtifactStore, artifact_refs: dict[str, str]) -> None:
        missing: list[str] = []
        mismatched: list[str] = []
        for key, artifact_id in artifact_refs.items():
            try:
                env = store.read_artifact(artifact_id)
            except ArtifactNotFoundError:
                missing.append(f"{key}={artifact_id}")
                continue
            if env.schema_version != self._schema_version:
                mismatched.append(
                    f"{key}={artifact_id} (schema={env.schema_version}, expected={self._schema_version})"
                )
        if missing or mismatched:
            parts: list[str] = []
            if missing:
                parts.append(f"missing artifacts: {', '.join(missing)}")
            if mismatched:
                parts.append(f"schema mismatch: {', '.join(mismatched)}")
            raise RuntimeError("Checkpoint artifact validation failed: " + "; ".join(parts))

    def _assert_no_orphaned_artifacts(self, store: ArtifactStore) -> None:
        if store.list_artifacts():
            raise RuntimeError(
                "Artifacts exist without a checkpoint. Resume the run or delete the run directory before starting."
            )

    def _checkpoint(
        self,
        stage_id: str,
        artifact_refs: dict[str, str],
        routing_state: dict[str, object] | None = None,
    ) -> None:
        stage_idx = self._stage_index_value(stage_id)
        state = dict(routing_state or {})
        state.setdefault("stage", stage_id)
        state.setdefault("index", stage_idx)
        cursor = self._event(stage_id, "decision", payload={"stage_index": stage_idx})
        checkpoint = write_checkpoint(
            self.run_root,
            self.run_id,
            CheckpointState(
                stage_name=stage_id,
                stage_index=stage_idx,
                event_cursor=cursor,
                artifact_refs=dict(artifact_refs),
                routing_state=state,
                tool_side_effect_ledger_ref=None,
            ),
        )
        self._event(
            stage_id,
            "checkpoint",
            refs={"checkpoint_id": checkpoint.checkpoint_id},
            payload={"stage_index": stage_idx},
        )

    def _raise_quota_circuit(
        self,
        stage_id: str,
        artifact_refs: dict[str, str],
        exc: QuotaExceededError,
    ) -> None:
        safe_stage = stage_id if stage_id in self._stage_index else "synthesis.candidate_delta"
        self._event(
            safe_stage,
            "decision",
            payload={
                "action": "circuit_breaker",
                "reason": "quota_exceeded",
                "provider": exc.provider,
                "status_code": exc.status_code,
                "role": exc.role,
                "request_id": exc.request_id,
            },
        )
        self._checkpoint(
            safe_stage,
            artifact_refs,
            routing_state={
                "stage": safe_stage,
                "reason": "quota_exceeded",
            },
        )
        raise RuntimeErrorException(
            "Circuit breaker tripped: quota exceeded. Checkpoint saved; resume after quota resets."
        ) from exc

    def _announce_stage(self, stage_id: str, detail: str | None = None) -> None:
        if not self._log_stage_transitions:
            return
        idx = self._stage_index.get(stage_id)
        total = len(self._stage_order)
        prefix = f"[{idx}/{total}] " if idx is not None and total else ""
        suffix = f" - {detail}" if detail else ""
        print(f"==> {prefix}{stage_id}{suffix}", flush=True)

    def _announce_stage_done(self, stage_id: str, elapsed_sec: float, detail: str | None = None) -> None:
        if not self._log_stage_transitions:
            return
        idx = self._stage_index.get(stage_id)
        total = len(self._stage_order)
        prefix = f"[{idx}/{total}] " if idx is not None and total else ""
        suffix = f" - {detail}" if detail else ""
        print(f"<== {prefix}{stage_id}{suffix} ({elapsed_sec:.2f}s)", flush=True)

    @contextmanager
    def _stage_timer(self, stage_id: str, detail: str | None = None) -> object:
        update_run_lock(self._run_lock_path, stage=stage_id)
        if self._log_stage_transitions:
            self._announce_stage(stage_id, detail=detail)
        start = time.perf_counter()
        try:
            yield
        finally:
            if self._log_stage_transitions:
                elapsed = time.perf_counter() - start
                self._announce_stage_done(stage_id, elapsed, detail=detail)

    def _write_artifact(self, store: ArtifactStore, envelope: ArtifactEnvelope, stage: str) -> None:
        skipped = False
        try:
            store.write_artifact(envelope)
        except ArtifactExistsError:
            skipped = True
        self._event(
            stage,
            "artifact_write",
            payload={"artifact_type": envelope.artifact_type, "skipped_existing": skipped},
            refs={"artifact_id": envelope.artifact_id},
        )

    def run(
        self,
        brief_path: Path,
        *,
        feedback_config: FeedbackConfig | None = None,
        feedback_provider: HumanInputProvider | None = None,
        _resume_checkpoint: Checkpoint | None = None,
        _skip_manifest: bool = False,
    ) -> RunResult:
        lock_cm = run_lock(self.run_root)
        self._run_lock_path = lock_cm.__enter__()
        try:
            return self._run_inner(
                brief_path,
                feedback_config=feedback_config,
                feedback_provider=feedback_provider,
                _resume_checkpoint=_resume_checkpoint,
                _skip_manifest=_skip_manifest,
            )
        finally:
            self._run_lock_path = None
            lock_cm.__exit__(None, None, None)

    def _run_inner(
        self,
        brief_path: Path,
        *,
        feedback_config: FeedbackConfig | None = None,
        feedback_provider: HumanInputProvider | None = None,
        _resume_checkpoint: Checkpoint | None = None,
        _skip_manifest: bool = False,
    ) -> RunResult:
        export_schema_files(self._schema_dir())
        self._validate_credentials()
        if not _skip_manifest:
            _ = self._create_manifest()
        brief_text = brief_path.read_text(encoding="utf-8")
        if _resume_checkpoint is None:
            (self.run_root / "config.snapshot.yml").write_text(self.config_raw, encoding="utf-8")
            (self.run_root / "brief.snapshot.md").write_text(brief_text, encoding="utf-8")
            self._write_execution_plan()
        store = self._store()
        artifact_refs: dict[str, str] = dict(_resume_checkpoint.artifact_refs) if _resume_checkpoint else {}
        routing_state: dict[str, object] = dict(_resume_checkpoint.routing_state) if _resume_checkpoint else {}
        resume_stage_index = _resume_checkpoint.stage_index if _resume_checkpoint else -1
        if _resume_checkpoint is None:
            self._assert_no_orphaned_artifacts(store)
        else:
            self._verify_checkpoint_artifacts(store, artifact_refs)
        run_start = time.perf_counter()
        feedback_cfg = feedback_config or FeedbackConfig.from_config(self.config)
        provider = self._resolve_feedback_provider(feedback_cfg, feedback_provider)
        planning_store = PlanningArtifactStore(self.run_root)
        feedback_gate = HumanFeedbackGate(run_id=str(self.run_id), run_root=self.run_root, provider=provider)
        clarification_resolutions: ClarificationResolutions | None = None
        clarification_context: dict[str, object] | None = None
        llm_enabled = bool(self._models)

        def invoke_json(
            messages: list[dict[str, str]],
            model: type[BaseModel],
            stage: str,
            meta: dict[str, object],
        ) -> BaseModel:
            return self._feedback_invoke_json(messages, model, stage, meta)

        def load_ref(key: str) -> ArtifactEnvelope | None:
            artifact_id = artifact_refs.get(key)
            if not artifact_id:
                return None
            return self._read_artifact(store, artifact_id)

        def enabled_call_count(stage_id: str) -> int:
            stage = self.stage_map.get(stage_id)
            if not stage:
                return 0
            return sum(1 for call in stage.calls if call.enabled)

        def collect_drafts() -> dict[str, str]:
            return {
                k: v
                for k, v in artifact_refs.items()
                if k.startswith("requirements_draft_")
                or k.startswith("architecture_draft_")
                or k.startswith("qa_templates_draft_")
                or k.startswith("risk_gov_draft_")
            }

        def drafts_complete(drafts: dict[str, str]) -> bool:
            expected = {
                "requirements_draft_": enabled_call_count("pods.requirements_draft"),
                "architecture_draft_": enabled_call_count("pods.architecture_options_draft"),
                "qa_templates_draft_": enabled_call_count("pods.qa_templates_draft"),
                "risk_gov_draft_": enabled_call_count("pods.risk_gov_draft"),
            }
            for prefix, count in expected.items():
                ids = [v for k, v in drafts.items() if k.startswith(prefix)]
                if len(ids) != count:
                    return False
                for artifact_id in ids:
                    if self._read_artifact(store, artifact_id) is None:
                        return False
            return True

        capsule = load_ref("capsule")
        capsule_draft_a = load_ref("capsule_draft_a")
        capsule_draft_b = load_ref("capsule_draft_b")
        if capsule is None:
            if capsule_draft_a is None or capsule_draft_b is None:
                with self._stage_timer("intake.capsule_normalize"):
                    draft_envs = self._stage_capsule_drafts(brief_text, artifact_refs)
                artifact_refs["capsule_draft_a"] = draft_envs["a"].artifact_id
                artifact_refs["capsule_draft_b"] = draft_envs["b"].artifact_id
                capsule_draft_a = draft_envs["a"]
            capsule_draft_b = draft_envs["b"]
            self._checkpoint("intake.capsule_normalize", artifact_refs)
        if capsule_draft_a is None or capsule_draft_b is None:
            raise RuntimeError("Capsule drafts missing after intake.capsule_normalize")
        with self._stage_timer("intake.capsule_reconcile"):
            capsule = self._stage_capsule_reconcile(
                brief_text,
                capsule_draft_a.payload,
                capsule_draft_b.payload,
                artifact_refs,
            )
        artifact_refs["capsule"] = capsule.artifact_id
        self._checkpoint("intake.capsule_reconcile", artifact_refs)

        # Pre-plan triage (cheap) and optional clarify-intent gate.
        if planning_store.exists("preplan_triage.json"):
            triage_payload = PrePlanTriage.model_validate_json(
                planning_store.path("preplan_triage.json").read_text(encoding="utf-8")
            )
        else:
            if llm_enabled:
                triage_payload = run_preplan_triage(
                    invoke_json=invoke_json,
                    brief=brief_text,
                    config_summary=json.dumps(feedback_cfg.__dict__, indent=2),
                    stage="clarify",
                )
            else:
                triage_payload = PrePlanTriage(schema_version="1.0", avenues_considered=[], uncertainties=[])
            planning_store.write_json("preplan_triage.json", triage_payload.model_dump())

        questions_payload: ClarificationQuestions | None = None
        if planning_store.exists("clarification_questions.json"):
            questions_payload = ClarificationQuestions.model_validate_json(
                planning_store.path("clarification_questions.json").read_text(encoding="utf-8")
            )

        if planning_store.exists("clarification_resolutions.json"):
            clarification_resolutions = ClarificationResolutions.model_validate_json(
                planning_store.path("clarification_resolutions.json").read_text(encoding="utf-8")
            )
        elif feedback_cfg.clarify:
            if questions_payload is None:
                if llm_enabled:
                    questions_payload = build_clarification_questions(
                        invoke_json=invoke_json,
                        brief=brief_text,
                        triage=triage_payload,
                        stage="clarify",
                    )
                else:
                    questions_payload = ClarificationQuestions(schema_version="1.0", questions=[])
                planning_store.write_json("clarification_questions.json", questions_payload.model_dump())
            if not questions_payload.questions:
                empty_responses = ClarificationResponses(schema_version="1.0", responses=[])
                planning_store.write_json("clarification_responses.json", empty_responses.model_dump())
                clarification_resolutions = ClarificationResolutions(schema_version="1.0", resolutions=[])
                planning_store.write_json(
                    "clarification_resolutions.json", clarification_resolutions.model_dump()
                )
            else:
                response = feedback_gate.request_response(
                    gate_type="clarify_intent",
                    request_artifact="clarification_questions.json",
                    response_artifact="clarification_responses.json",
                    response_model=ClarificationResponses,
                    round=0,
                    instructions={
                        "how_to_resume": f"rerun with --resume-run {self.run_id} --response-file <path>"
                    },
                )
                responses_payload = ClarificationResponses.model_validate(response.payload)
                clarification_resolutions = self._resolve_clarification_responses(
                    questions_payload,
                    responses_payload,
                    feedback_gate,
                )
                planning_store.write_json(
                    "clarification_resolutions.json", clarification_resolutions.model_dump()
                )

        if clarification_resolutions is not None:
            clarification_context = clarification_resolutions.model_dump()

        capsule_context: dict[str, object] | object = capsule.payload
        if isinstance(capsule.payload, dict):
            capsule_context = dict(capsule.payload)
            if clarification_context is not None:
                capsule_context["clarification_resolutions"] = clarification_context

        clarification = load_ref("clarification_plan")
        if clarification is None:
            with self._stage_timer("clarify.clarification_gate"):
                clarification = self._stage_clarification(capsule_context, artifact_refs)
            artifact_refs["clarification_plan"] = clarification.artifact_id
            self._checkpoint("clarify.clarification_gate", artifact_refs)

        drafts = collect_drafts()
        if not drafts_complete(drafts):
            drafts = self._stage_pods(capsule_context, artifact_refs)
            artifact_refs.update(drafts)
            self._checkpoint("pods.risk_gov_draft", artifact_refs)

        req_merge = load_ref("requirements_merge")
        if req_merge is None:
            with self._stage_timer("merge.requirements_merge"):
                req_merge = self._stage_requirements_merge(capsule_context, drafts, artifact_refs)
            artifact_refs["requirements_merge"] = req_merge.artifact_id
            self._checkpoint("merge.requirements_merge", artifact_refs)

        req_canon = load_ref("requirements_canonical")
        if req_canon is None:
            with self._stage_timer("canonicalize.requirements"):
                req_canon = self._stage_requirements_canonical(req_merge.payload, artifact_refs)
            artifact_refs["requirements_canonical"] = req_canon.artifact_id
            self._checkpoint("canonicalize.requirements", artifact_refs)

        arch_merge = load_ref("architecture_merge")
        if arch_merge is None:
            with self._stage_timer("merge.architecture_merge"):
                arch_merge = self._stage_arch_merge(capsule_context, drafts, artifact_refs)
            artifact_refs["architecture_merge"] = arch_merge.artifact_id
            self._checkpoint("merge.architecture_merge", artifact_refs)

        arch_canon = load_ref("architecture_canonical")
        if arch_canon is None:
            with self._stage_timer("canonicalize.architecture"):
                arch_canon = self._stage_arch_canonical(arch_merge.payload, artifact_refs)
            artifact_refs["architecture_canonical"] = arch_canon.artifact_id
            self._checkpoint("canonicalize.architecture", artifact_refs)

        risk_merge = load_ref("risk_gov_merge")
        if risk_merge is None:
            with self._stage_timer("merge.risk_gov_merge"):
            risk_merge = self._stage_risk_merge(
                capsule_context,
                drafts,
                req_canon.payload,
                arch_canon.payload,
                artifact_refs,
            )
            artifact_refs["risk_gov_merge"] = risk_merge.artifact_id
            self._checkpoint("merge.risk_gov_merge", artifact_refs)

        risk_canon = load_ref("risk_gov_canonical")
        if risk_canon is None:
            with self._stage_timer("canonicalize.risk_gov"):
                risk_canon = self._stage_risk_canonical(risk_merge.payload, artifact_refs)
            artifact_refs["risk_gov_canonical"] = risk_canon.artifact_id
            self._checkpoint("canonicalize.risk_gov", artifact_refs)

        aligned_arch = self._align_architecture_risks(
            arch_canon.payload,
            risk_canon.payload,
            current_ref=arch_canon.artifact_id,
            artifact_refs=artifact_refs,
        )
        if aligned_arch is not None:
            arch_canon = aligned_arch
            artifact_refs["architecture_canonical"] = arch_canon.artifact_id
            self._checkpoint("canonicalize.architecture", artifact_refs)

        qa_strategy = load_ref("qa_strategy_canonical")
        if qa_strategy is None:
            with self._stage_timer("merge.qa_strategy"):
            qa_strategy = self._stage_qa_strategy(capsule_context, drafts, artifact_refs)
            artifact_refs["qa_strategy_canonical"] = qa_strategy.artifact_id
            self._checkpoint("merge.qa_strategy", artifact_refs)

        acceptance_draft = load_ref("acceptance_draft")
        if acceptance_draft is None:
            with self._stage_timer("acceptance.generate"):
            acceptance_draft = self._stage_acceptance_generate(
                capsule_context,
                req_canon.payload,
                arch_canon.payload,
                qa_strategy.payload,
                artifact_refs,
            )
            artifact_refs["acceptance_draft"] = acceptance_draft.artifact_id
            self._checkpoint("acceptance.generate", artifact_refs)

        acceptance_canon = load_ref("acceptance_canonical")
        if acceptance_canon is None:
            with self._stage_timer("acceptance.canonicalize"):
                acceptance_canon = self._stage_acceptance_canonical(
                    acceptance_draft.payload,
                    req_canon.payload,
                    artifact_refs,
                )
            artifact_refs["acceptance_canonical"] = acceptance_canon.artifact_id
            self._checkpoint("acceptance.canonicalize", artifact_refs)

        alignment = load_ref("alignment_warnings")
        if alignment is None:
            with self._stage_timer("audit.alignment"):
            alignment = self._stage_alignment(
                capsule_context,
                req_canon.payload,
                acceptance_canon.payload,
                arch_canon.payload,
                risk_canon.payload,
                artifact_refs,
            )
            artifact_refs["alignment_warnings"] = alignment.artifact_id
            self._checkpoint("audit.alignment", artifact_refs)

        branch_set = load_ref("branch_set")
        if branch_set is None:
            with self._stage_timer("branch.build"):
            branch_set = self._stage_branch_build(
                capsule_context,
                req_canon.payload,
                acceptance_canon.payload,
                arch_canon.payload,
                risk_canon.payload,
                alignment.payload,
                artifact_refs,
            )
            artifact_refs["branch_set"] = branch_set.artifact_id
            self._checkpoint("branch.build", artifact_refs)

        candidates: dict[str, CandidateState] = {}
        judge_context: dict[str, object] = {"summaries": []}
        candidate_meta: list[dict[str, object]] | None = None
        candidates_complete = False
        candidate_cycle = 1
        candidate_stage_idx = self._stage_index_value("synthesis.candidate_delta")
        failure_stage_idx = self._stage_index_value("analysis.failure_modes")
        if _resume_checkpoint is not None and resume_stage_index >= candidate_stage_idx:
            candidate_cycle = int(routing_state.get("candidate_cycle", 1) or 1)
            if candidate_cycle < 1:
                candidate_cycle = 1
            candidate_meta = routing_state.get("candidates")
            candidates_complete = bool(
                routing_state.get("candidates_complete", resume_stage_index >= failure_stage_idx)
            )
            if isinstance(candidate_meta, list):
                candidates, judge_context = self._restore_candidates_from_metadata(store, candidate_meta)
            if not candidates:
                candidates, judge_context = self._restore_candidates_from_store(store)
                if candidates:
                    candidate_meta = [self._candidate_metadata(c) for c in candidates.values()]

        if not candidates:
            restored, restored_context = self._restore_candidates_from_store(store)
            if restored:
                candidates = restored
                judge_context = restored_context
                candidate_meta = [self._candidate_metadata(c) for c in candidates.values()]
            else:
                if store.list_artifacts("plan_candidate"):
                    if _resume_checkpoint is None:
                        raise RuntimeError(
                            "Partial candidate artifacts detected without a checkpoint. "
                            "Resume cannot safely continue; delete the run or resume from an earlier checkpoint."
                        )
                    self._event(
                        "synthesis.candidate_delta",
                        "decision",
                        payload={
                            "partial_candidate_artifacts": True,
                            "action": "resume_with_rebuild",
                        },
                    )

        for candidate_state in candidates.values():
            artifact_refs.setdefault(
                f"candidate_delta_{candidate_state.candidate_id}",
                candidate_state.delta_ref,
            )

        expected_candidate_ids = self._expected_candidate_ids(branch_set.payload)
        missing_candidate_ids = [cid for cid in expected_candidate_ids if cid not in candidates]

        if missing_candidate_ids:
            candidate_kwargs: dict[str, object] = {
                "existing_candidates": candidates,
                "existing_judge_context": judge_context,
                "expected_candidate_ids": expected_candidate_ids,
            }
            if "candidate_cycle" in inspect.signature(self._stage_candidate_flow).parameters:
                candidate_kwargs["candidate_cycle"] = candidate_cycle
            candidates, judge_context = self._stage_candidate_flow(
                capsule_context,
                req_canon.payload,
                acceptance_canon.payload,
                arch_canon.payload,
                risk_canon.payload,
                branch_set.payload,
                artifact_refs,
                **candidate_kwargs,
            )
            candidate_meta = [self._candidate_metadata(c) for c in candidates.values()]
            branch_payload = BranchSetPayload.model_validate(branch_set.payload)
            self._checkpoint(
                "analysis.failure_modes",
                artifact_refs,
                routing_state={
                    "candidate_count": len(candidates),
                    "branch_count": len(branch_payload.branches),
                    "branch_ids": [b.branch_id for b in branch_payload.branches],
                    "candidates": candidate_meta,
                    "expected_candidates": expected_candidate_ids,
                    "candidates_complete": True,
                    "candidate_cycle": candidate_cycle,
                },
            )
        else:
            if candidate_meta is None:
                candidate_meta = [self._candidate_metadata(c) for c in candidates.values()]
            if not candidates_complete and expected_candidate_ids:
                branch_payload = BranchSetPayload.model_validate(branch_set.payload)
                self._checkpoint(
                    "analysis.failure_modes",
                    artifact_refs,
                    routing_state={
                        "candidate_count": len(candidates),
                        "branch_count": len(branch_payload.branches),
                        "branch_ids": [b.branch_id for b in branch_payload.branches],
                        "candidates": candidate_meta,
                        "expected_candidates": expected_candidate_ids,
                        "candidates_complete": True,
                        "candidate_cycle": candidate_cycle,
                    },
                )

        max_cycles = int(
            self._runtime_cfg.get("orchestrator", {}).get("max_full_cycles_without_pass", 1)
        )
        if max_cycles < 1:
            max_cycles = 1

        passing_ids = sorted(
            cid for cid, candidate in candidates.items() if self._candidate_passes(candidate)
        )
        failing_ids = sorted(
            cid for cid, candidate in candidates.items() if not self._candidate_passes(candidate)
        )

        while not passing_ids and candidate_cycle < max_cycles and expected_candidate_ids:
            self._event(
                "synthesis.candidate_delta",
                "decision",
                payload={
                    "action": "rerun_failed_candidates",
                    "cycle": candidate_cycle + 1,
                    "max_cycles": max_cycles,
                    "failing_candidates": failing_ids,
                },
            )
            candidate_kwargs = {
                "existing_candidates": candidates,
                "existing_judge_context": judge_context,
                "expected_candidate_ids": expected_candidate_ids,
                "rerun_candidates": set(failing_ids),
            }
            if "candidate_cycle" in inspect.signature(self._stage_candidate_flow).parameters:
                candidate_kwargs["candidate_cycle"] = candidate_cycle + 1
            candidates, judge_context = self._stage_candidate_flow(
                capsule_context,
                req_canon.payload,
                acceptance_canon.payload,
                arch_canon.payload,
                risk_canon.payload,
                branch_set.payload,
                artifact_refs,
                **candidate_kwargs,
            )
            candidate_cycle += 1
            candidate_meta = [self._candidate_metadata(c) for c in candidates.values()]
            branch_payload = BranchSetPayload.model_validate(branch_set.payload)
            self._checkpoint(
                "analysis.failure_modes",
                artifact_refs,
                routing_state={
                    "candidate_count": len(candidates),
                    "branch_count": len(branch_payload.branches),
                    "branch_ids": [b.branch_id for b in branch_payload.branches],
                    "candidates": candidate_meta,
                    "expected_candidates": expected_candidate_ids,
                    "candidates_complete": True,
                    "candidate_cycle": candidate_cycle,
                },
            )
            passing_ids = sorted(
                cid for cid, candidate in candidates.items() if self._candidate_passes(candidate)
            )
            failing_ids = sorted(
                cid for cid, candidate in candidates.items() if not self._candidate_passes(candidate)
            )

        allow_judge_fallback = False
        if not passing_ids:
            allow_judge_fallback = True
            self._event(
                "analysis.failure_modes",
                "decision",
                payload={
                    "reason": "no_passing_candidates",
                    "action": "judge_fallback",
                    "cycle": candidate_cycle,
                    "max_cycles": max_cycles,
                    "candidate_statuses": {
                        cid: {
                            "validator_status": candidate.validator_status,
                            "triage_blocked": candidate.triage_blocked,
                            "failure_mode_pass": candidate.failure_mode_pass,
                        }
                        for cid, candidate in candidates.items()
                    },
                },
            )

        branch_payload = BranchSetPayload.model_validate(branch_set.payload)
        routing_state_base = {
            "candidate_count": len(candidates),
            "branch_count": len(branch_payload.branches),
            "branch_ids": [b.branch_id for b in branch_payload.branches],
            "candidates": candidate_meta or [],
            "expected_candidates": expected_candidate_ids,
            "candidates_complete": True,
            "candidate_cycle": candidate_cycle,
        }

        judge_artifacts = None
        winner_id = None
        if _resume_checkpoint is not None:
            pairwise_idx = self._stage_index_value("judge.pairwise")
            arbitrator_idx = self._stage_index_value("judge.arbitrator")
            resume_idx = pairwise_idx if pairwise_idx >= 0 else arbitrator_idx
            if resume_idx >= 0 and resume_stage_index >= resume_idx:
                judge_artifacts = routing_state.get("judge_artifacts")
                winner_id = routing_state.get("winner_id")
        if not isinstance(judge_artifacts, dict) or not winner_id or winner_id not in candidates:
            winner_id, judge_artifacts = self._stage_judge_tournament(
                candidates,
                judge_context,
                artifact_refs,
                allow_fallback=allow_judge_fallback,
            )
            routing_state_judge = dict(routing_state_base)
            routing_state_judge.update({"winner_id": winner_id, "judge_artifacts": judge_artifacts})
            if "judge.pairwise" in self.stage_map:
                self._checkpoint("judge.pairwise", artifact_refs, routing_state=routing_state_judge)
            self._checkpoint("judge.arbitrator", artifact_refs, routing_state=routing_state_judge)

        winner_state = candidates[winner_id]
        plan_dict = winner_state.payload.plan_package.model_dump(by_alias=True, mode="json")
        approval = None
        if feedback_cfg.plan_review:
            plan_dict, approval = run_plan_review_loop(
                run_id=str(self.run_id),
                run_root=self.run_root,
                plan=plan_dict,
                gate=feedback_gate,
                feedback_cfg=feedback_cfg,
                invoke_json=invoke_json,
                llm_enabled=llm_enabled,
            )
            winner_state.payload = winner_state.payload.model_copy(
                update={"plan_package": PlanPackage.model_validate(plan_dict)}
            )
            if feedback_cfg.require_explicit_approval:
                if (
                    approval is None
                    or not approval.approved
                    or approval.approved_plan_hash != plan_hash(plan_dict)
                ):
                    raise RuntimeError("Plan review approval required before freeze")

        freeze_record = load_ref("freeze_record")
        if freeze_record is None:
            with self._stage_timer("freeze.freeze"):
                freeze_record = self._stage_freeze(winner_state, judge_artifacts, artifact_refs)
            artifact_refs["freeze_record"] = freeze_record.artifact_id
            routing_state_freeze = dict(routing_state_base)
            routing_state_freeze.update({"winner_id": winner_id, "judge_artifacts": judge_artifacts})
            self._checkpoint("freeze.freeze", artifact_refs, routing_state=routing_state_freeze)

        plan_final = winner_state.payload.plan_package.model_dump(by_alias=True, mode="json")
        planning_store.write_json("plan_package_final.json", plan_final)
        planning_files = sorted(path.name for path in planning_store.root.iterdir() if path.is_file())
        planning_store.write_json(
            "planning_handoff_bundle.json",
            {
                "schema_version": "1.0",
                "run_id": str(self.run_id),
                "plan_package_final_ref": "plan_package_final.json",
                "event_log_ref": "event_log.jsonl" if planning_store.exists("event_log.jsonl") else None,
                "artifacts": planning_files,
            },
        )

        decision_record = load_ref("decision_record")
        if decision_record is None:
            with self._stage_timer("freeze.decision_record"):
                decision_record = self._stage_decision_record(winner_state, candidates, judge_artifacts, artifact_refs)
            artifact_refs["decision_record"] = decision_record.artifact_id
            routing_state_decision = dict(routing_state_base)
            routing_state_decision.update({"winner_id": winner_id, "judge_artifacts": judge_artifacts})
            self._checkpoint("freeze.decision_record", artifact_refs, routing_state=routing_state_decision)

        renderer_env = self._read_artifact(store, "markdown_render_v1")
        if renderer_env is None:
            if "freeze.renderer" in self.stage_map:
                if self._safe_mode:
                    try:
                        with self._stage_timer("freeze.renderer"):
                            renderer_env = self._stage_renderer(winner_state, artifact_refs)
                    except Exception as exc:
                        self._event(
                            "freeze.renderer",
                            "error",
                            payload={"safe_mode_skip": True, "error": str(exc)},
                        )
                        renderer_env = None
                else:
                    with self._stage_timer("freeze.renderer"):
                        renderer_env = self._stage_renderer(winner_state, artifact_refs)
            else:
                if self._safe_mode:
                    try:
                        renderer_env = self._stage_renderer(winner_state, artifact_refs)
                    except Exception as exc:
                        self._event(
                            "freeze.renderer",
                            "error",
                            payload={"safe_mode_skip": True, "error": str(exc)},
                        )
                        renderer_env = None
                else:
                    renderer_env = self._stage_renderer(winner_state, artifact_refs)
            if renderer_env is not None:
                artifact_refs["markdown_render"] = renderer_env.artifact_id
                routing_state_render = dict(routing_state_base)
                routing_state_render.update({"winner_id": winner_id, "judge_artifacts": judge_artifacts})
                self._checkpoint("freeze.renderer", artifact_refs, routing_state=routing_state_render)

        total_duration = time.perf_counter() - run_start
        repaired_count = sum(1 for c in candidates.values() if self._candidate_version(c.plan_ref) > 1)
        validator_pass = sum(1 for c in candidates.values() if c.validator_status == "PASS")
        metrics_payload = RunMetricsPayload(
            run_id=str(self.run_id),
            stage_durations_sec={},
            candidate_count=len(candidates),
            repaired_candidate_count=repaired_count,
            validator_pass_count=validator_pass,
            validator_fail_count=max(0, len(candidates) - validator_pass),
            frozen_count=1,
            total_duration_sec=round(total_duration, 3),
        )
        metrics_env = ArtifactEnvelope(
            artifact_type="run_metrics",
            artifact_id="run_metrics_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("freeze_record", "")],
            payload=metrics_payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), metrics_env, stage="freeze.freeze")
        frozen_artifact_id = artifact_refs.get("frozen_plan", winner_state.plan_ref)
        _ = store
        return RunResult(run_id=self.run_id, run_root=self.run_root, frozen_artifact_id=frozen_artifact_id)

    def resume(
        self,
        checkpoint_id: str | None = None,
        *,
        feedback_config: FeedbackConfig | None = None,
        feedback_provider: HumanInputProvider | None = None,
    ) -> RunResult:
        checkpoint = (
            load_checkpoint(self.run_root, checkpoint_id)
            if checkpoint_id
            else latest_checkpoint(self.run_root)
        )
        frozen_id = checkpoint.artifact_refs.get("frozen_plan", "")
        if frozen_id:
            return RunResult(run_id=self.run_id, run_root=self.run_root, frozen_artifact_id=frozen_id)
        config_snapshot = self.run_root / "config.snapshot.yml"
        brief_snapshot = self.run_root / "brief.snapshot.md"
        if not config_snapshot.exists() or not brief_snapshot.exists():
            raise FileNotFoundError("Run snapshots missing: config.snapshot.yml and brief.snapshot.md are required")
        return self.run(
            brief_snapshot,
            feedback_config=feedback_config,
            feedback_provider=feedback_provider,
            _resume_checkpoint=checkpoint,
            _skip_manifest=True,
        )

    def status(self) -> dict[str, object]:
        checkpoint = latest_checkpoint(self.run_root)
        last_event = read_last_event(self.run_root)
        checkpoints = list_checkpoints(self.run_root)
        total = len(self._stage_order)
        return {
            "run_id": str(self.run_id),
            "stage_name": checkpoint.stage_name,
            "stage_index": checkpoint.stage_index,
            "total_stages": total,
            "stage_progress": f"{checkpoint.stage_index + 1}/{total}" if total else None,
            "is_complete": "frozen_plan" in checkpoint.artifact_refs,
            "checkpoint_id": checkpoint.checkpoint_id,
            "last_event": last_event.model_dump(by_alias=True) if last_event else None,
            "checkpoints": [
                {
                    "checkpoint_id": item.checkpoint_id,
                    "created_at": item.created_at.isoformat(),
                    "stage_name": item.stage_name,
                    "stage_index": item.stage_index,
                }
                for item in checkpoints
            ],
        }

    def _create_manifest(self) -> object:
        workflow = self._workflow
        orchestrator_cfg = self._runtime_cfg.get("orchestrator", {})
        judge_rules = (
            dict(self._judge_policy.get("rules", {}))
            if isinstance(self._judge_policy.get("rules", {}), dict)
            else {}
        )
        judge_panel_size = judge_rules.get("panel_size", self._judge_policy.get("panel_size"))
        manifest = create_manifest(
            self.run_root,
            ManifestInput(
                run_id=self.run_id,
                code_version=_git_code_version(),
                schema_versions={"artifacts": self._schema_version},
                run_config_version=str(self.config.get("version", "")),
                stage_machine_version=str(self.config.get("version", "1.0.0")),
                validator_suite_version=str(self.config.get("version", "1.0.0")),
                selection_protocol_version=str(self.config.get("version", "1.0.0")),
                model_portfolio=_model_portfolio(self.config),
                branching_policy={
                    "branch_count": workflow.get("branches_max"),
                    "keep_count": workflow.get("branches_kept_after_prune"),
                },
                ensemble_policy={
                    "synthesizers_per_branch": workflow.get("candidates_per_branch"),
                    "judge_panel_size": judge_panel_size,
                },
                iteration_caps={
                    "max_full_cycles_without_pass": orchestrator_cfg.get("max_full_cycles_without_pass"),
                    "max_pod_reruns": orchestrator_cfg.get("max_pod_reruns"),
                    "max_branch_rebuilds": orchestrator_cfg.get("max_branch_rebuilds"),
                    "max_candidate_repairs": orchestrator_cfg.get("max_repairs_per_candidate"),
                },
                tool_policy_version="1.0.0",
                tools_enabled=False,
                consent_profile="trusted_user",
                determinism_disclaimer="LLM outputs are stochastic; orchestrator routing is deterministic.",
                prompt_pack_hash=self.prompt_library.pack_hash(),
                config_raw=self.config_raw,
            ),
        )
        return manifest

    def _write_execution_plan(self) -> None:
        stages: list[dict[str, object]] = []
        for stage_id in self._stage_order:
            stage = self.stage_map.get(stage_id)
            if not stage:
                continue
            stages.append(
                {
                    "id": stage.id,
                    "kind": stage.kind,
                    "prompt_id": stage.prompt_id,
                    "output_schema": stage.output_schema,
                    "schema_required": stage.schema_required,
                    "tooling_allowed": stage.tooling_allowed,
                    "max_attempts": stage.max_attempts,
                    "map_over": stage.map_over,
                    "policy": stage.policy,
                    "selection": stage.selection,
                    "calls": [
                        {
                            "name": call.name,
                            "model": call.model_key,
                            "profile": call.profile,
                            "max_output_tokens": call.max_output_tokens,
                            "overrides": call.overrides,
                            "enabled": call.enabled,
                        }
                        for call in stage.calls
                    ],
                    "fallbacks": [
                        {
                            "name": call.name,
                            "model": call.model_key,
                            "profile": call.profile,
                            "max_output_tokens": call.max_output_tokens,
                            "overrides": call.overrides,
                            "enabled": call.enabled,
                        }
                        for call in stage.fallbacks
                    ],
                }
            )
        plan = {
            "run_id": str(self.run_id),
            "created_at": _now().isoformat(),
            "config_name": str(self.config.get("name", "")),
            "run_config_version": str(self.config.get("version", "")),
            "schemas_version": self._schema_version,
            "prompt_pack_hash": self.prompt_library.pack_hash(),
            "workflow": dict(self._workflow),
            "judge_policy": dict(self._judge_policy),
            "stages": stages,
        }
        (self.run_root / "execution_plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _stage_capsule(self, brief: str, artifact_refs: dict[str, str]) -> ArtifactEnvelope:
        stage = self.stage_map["intake.capsule_normalize"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "BRIEF_JSON_STRING": json.dumps(brief, ensure_ascii=True),
                "CAPSULE_SCHEMA_JSON": schema_json,
            }
        )
        payload = self._call_stage_single(stage, prompt_vars, artifact_refs)
        env = ArtifactEnvelope(
            artifact_type="project_capsule",
            artifact_id="capsule_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_capsule_drafts(
        self, brief: str, artifact_refs: dict[str, str]
    ) -> dict[str, ArtifactEnvelope]:
        stage = self.stage_map["intake.capsule_normalize"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "BRIEF_JSON_STRING": json.dumps(brief, ensure_ascii=True),
                "CAPSULE_SCHEMA_JSON": schema_json,
            }
        )
        outputs = self._call_stage_multi(stage, prompt_vars, artifact_refs)
        if len(outputs) < 2:
            raise RuntimeError("Capsule normalization produced fewer than 2 drafts")
        draft_envs: dict[str, ArtifactEnvelope] = {}
        for idx, payload in enumerate(outputs[:2]):
            label = chr(ord("a") + idx)
            env = ArtifactEnvelope(
                artifact_type="project_capsule",
                artifact_id=f"capsule_draft_{label}_v1",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[],
                payload=payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), env, stage=stage.id)
            draft_envs[label] = env
        return draft_envs

    def _stage_capsule_reconcile(
        self,
        brief: str,
        draft_a: dict[str, object],
        draft_b: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["intake.capsule_reconcile"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "BRIEF_JSON_STRING": json.dumps(brief, ensure_ascii=True),
                "CAPSULE_DRAFT_A_JSON": draft_a,
                "CAPSULE_DRAFT_B_JSON": draft_b,
                "CAPSULE_SCHEMA_JSON": schema_json,
            }
        )
        output = self._call_stage_single(stage, prompt_vars, artifact_refs)
        env = ArtifactEnvelope(
            artifact_type="project_capsule",
            artifact_id="capsule_canonical_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[
                ref
                for ref in (
                    artifact_refs.get("capsule_draft_a", ""),
                    artifact_refs.get("capsule_draft_b", ""),
                )
                if ref
            ],
            payload=output.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_clarification(self, capsule: dict[str, object], artifact_refs: dict[str, str]) -> ArtifactEnvelope:
        stage = self.stage_map["clarify.clarification_gate"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "CLARIFY_SCHEMA_JSON": schema_json,
            }
        )
        outputs = self._call_stage_multi(stage, prompt_vars, artifact_refs)
        merged = _merge_clarifications(outputs)
        env = ArtifactEnvelope(
            artifact_type="clarification_plan",
            artifact_id="clarification_plan_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("capsule", "")],
            payload=merged.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_pods(self, capsule: dict[str, object], artifact_refs: dict[str, str]) -> dict[str, str]:
        outputs: dict[str, str] = {}
        prompt_vars_base = self._base_prompt_vars("pods", artifact_refs)

        def run_pod_stage(
            stage: StageConfig,
            build_vars: Callable[[int, dict[str, object]], dict[str, object]],
            artifact_type: str,
            artifact_prefix: str,
        ) -> dict[str, str]:
            with self._stage_timer(stage.id):
                schema_json = self._schema_json(stage.output_schema)
                tasks: list[tuple[int, Callable[[], object]]] = []
                stage_outputs: dict[str, str] = {}
                store = self._store()
                for idx, call in enumerate(stage.calls):
                    if not call.enabled:
                        continue
                    artifact_id = f"{artifact_prefix}_{idx + 1}_v1"
                    if self._read_artifact(store, artifact_id) is not None:
                        stage_outputs[f"{artifact_prefix}_{idx + 1}"] = artifact_id
                        continue
                    prompt_vars = dict(prompt_vars_base)
                    prompt_vars.update(build_vars(idx, schema_json))
                    tasks.append(
                        (
                            idx,
                            lambda call=call, prompt_vars=prompt_vars: self._call_stage_call(
                                stage,
                                call,
                                prompt_vars,
                                artifact_refs,
                            ),
                        )
                    )
                results, errors = self._run_parallel_tasks(tasks)
                if errors:
                    errors.sort(key=lambda item: item[0])
                    raise errors[0][1]
                for idx in sorted(results):
                    payload = results[idx]
                    artifact_id = f"{artifact_prefix}_{idx + 1}_v1"
                    env = ArtifactEnvelope(
                        artifact_type=artifact_type,
                        artifact_id=artifact_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=_now(),
                        source_run_id=self.run_id,
                        parents=[artifact_refs.get("capsule", "")],
                        payload=payload.model_dump(by_alias=True),
                    )
                    self._write_artifact(self._store(), env, stage=stage.id)
                    stage_outputs[f"{artifact_prefix}_{idx + 1}"] = artifact_id
                return stage_outputs

        def build_requirements_vars(idx: int, schema_json: dict[str, object]) -> dict[str, object]:
            prefix = f"REQ_{chr(ord('A') + idx)}"
            return {
                "CAPSULE_JSON": capsule,
                "REQ_DRAFT_PREFIX": prefix,
                "REQ_DRAFT_SCHEMA_JSON": schema_json,
            }

        def build_arch_vars(idx: int, schema_json: dict[str, object]) -> dict[str, object]:
            opt_prefix = f"OPT_{chr(ord('A') + idx)}"
            comp_prefix = f"CMP_{chr(ord('A') + idx)}"
            if_prefix = f"IF_{chr(ord('A') + idx)}"
            return {
                "CAPSULE_JSON": capsule,
                "OPT_PREFIX": opt_prefix,
                "COMP_PREFIX": comp_prefix,
                "IF_PREFIX": if_prefix,
                "ARCH_DRAFT_SCHEMA_JSON": schema_json,
            }

        def build_qa_vars(_: int, schema_json: dict[str, object]) -> dict[str, object]:
            return {
                "CAPSULE_JSON": capsule,
                "QA_TEMPLATES_SCHEMA_JSON": schema_json,
            }

        def build_risk_vars(idx: int, schema_json: dict[str, object]) -> dict[str, object]:
            prefix = f"RISK_{chr(ord('A') + idx)}"
            return {
                "CAPSULE_JSON": capsule,
                "RISK_DRAFT_PREFIX": prefix,
                "RISK_DRAFT_SCHEMA_JSON": schema_json,
            }

        requirements_stage = self.stage_map["pods.requirements_draft"]
        arch_stage = self.stage_map["pods.architecture_options_draft"]
        qa_stage = self.stage_map["pods.qa_templates_draft"]
        risk_stage = self.stage_map["pods.risk_gov_draft"]

        pod_tasks: list[tuple[int, Callable[[], object]]] = [
            (
                0,
                lambda: run_pod_stage(
                    requirements_stage,
                    build_requirements_vars,
                    "requirements_draft",
                    "requirements_draft",
                ),
            ),
            (
                1,
                lambda: run_pod_stage(
                    arch_stage,
                    build_arch_vars,
                    "architecture_options_draft",
                    "architecture_draft",
                ),
            ),
            (
                2,
                lambda: run_pod_stage(
                    qa_stage,
                    build_qa_vars,
                    "qa_templates_draft",
                    "qa_templates_draft",
                ),
            ),
            (
                3,
                lambda: run_pod_stage(
                    risk_stage,
                    build_risk_vars,
                    "risk_gov_draft",
                    "risk_gov_draft",
                ),
            ),
        ]
        pod_results, pod_errors = self._run_parallel_tasks(pod_tasks)
        if pod_errors:
            pod_errors.sort(key=lambda item: item[0])
            raise pod_errors[0][1]
        for idx in sorted(pod_results):
            outputs.update(pod_results[idx])
        return outputs
    def _stage_requirements_merge(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["merge.requirements_merge"]
        draft_payloads = [
            self._store().read_artifact(drafts[key]).payload
            for key in sorted(k for k in drafts if k.startswith("requirements_draft_"))
        ]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "REQ_POD_DRAFTS_JSON_ARRAY": draft_payloads,
                "REQ_MERGE_SCHEMA_JSON": schema_json,
            }
        )
        payload = self._call_stage_single(stage, prompt_vars, artifact_refs)
        env = ArtifactEnvelope(
            artifact_type="requirements_merge",
            artifact_id="requirements_merge_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=list(drafts.values()),
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_requirements_canonical(
        self,
        merge_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["canonicalize.requirements"]
        merge = RequirementsMergePayload.model_validate(merge_payload)
        requirements = []
        id_map: dict[str, str] = {}
        for idx, unit in enumerate(merge.units, start=1):
            new_id = f"R{idx}"
            id_map[unit.id] = new_id
            requirements.append(
                {
                    "id": new_id,
                    "priority": unit.priority,
                    "text": unit.text,
                    "rationale": unit.rationale,
                }
            )
        if not requirements:
            requirements = [
                {
                    "id": "R1",
                    "priority": "MUST",
                    "text": "Deliver the requested planning workflow.",
                    "rationale": "Baseline requirement when drafts are empty.",
                }
            ]
            id_map["R1"] = "R1"
        payload = RequirementsCanonicalPayload(
            requirements=RequirementsDraftPayload.model_validate({"requirements": requirements}).requirements,
            id_map=id_map,
            merge_log=merge.merge_log,
        )
        env = ArtifactEnvelope(
            artifact_type="requirements_canonical",
            artifact_id="requirements_canonical_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("requirements_merge", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_arch_merge(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["merge.architecture_merge"]
        draft_payloads = [
            self._store().read_artifact(drafts[key]).payload
            for key in sorted(k for k in drafts if k.startswith("architecture_draft_"))
        ]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "ARCH_POD_DRAFTS_JSON_ARRAY": draft_payloads,
                "ARCH_MERGE_SCHEMA_JSON": schema_json,
            }
        )
        payload = self._call_stage_single(stage, prompt_vars, artifact_refs)
        env = ArtifactEnvelope(
            artifact_type="architecture_merge",
            artifact_id="architecture_merge_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=list(drafts.values()),
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_arch_canonical(
        self,
        merge_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["canonicalize.architecture"]
        merge = ArchitectureMergePayload.model_validate(merge_payload)
        option_id_map: dict[str, str] = {}
        component_id_map: dict[str, str] = {}
        interface_id_map: dict[str, str] = {}
        options: list[ArchitectureOption] = []
        comp_counter = 1
        if_counter = 1

        for opt_idx, option in enumerate(merge.options, start=1):
            new_opt_id = f"O{opt_idx}"
            option_id_map[option.id] = new_opt_id
            components: list[Component] = []
            for comp in option.components:
                mapped = component_id_map.get(comp.id)
                if mapped is None:
                    mapped = f"C{comp_counter}"
                    comp_counter += 1
                    component_id_map[comp.id] = mapped
                components.append(Component(id=mapped, name=comp.name, responsibilities=comp.responsibilities))
            interfaces: list[Interface] = []
            for iface in option.interfaces:
                mapped_if = interface_id_map.get(iface.id)
                if mapped_if is None:
                    mapped_if = f"IF{if_counter}"
                    if_counter += 1
                    interface_id_map[iface.id] = mapped_if
                from_comp = component_id_map.get(iface.from_component, iface.from_component)
                to_comp = component_id_map.get(iface.to, iface.to)
                interfaces.append(
                    Interface(id=mapped_if, **{"from": from_comp}, to=to_comp, contract=iface.contract)
                )
            options.append(
                ArchitectureOption(
                    id=new_opt_id,
                    summary=option.summary,
                    components=components,
                    interfaces=interfaces,
                    tradeoffs=Tradeoffs(pros=option.tradeoffs.pros, cons=option.tradeoffs.cons),
                    risks=option.risks,
                )
            )

        if not options:
            options.append(
                ArchitectureOption(
                    id="O1",
                    summary="Placeholder architecture option",
                    components=[Component(id="C1", name="Core", responsibilities=["Deliver plan"])],
                    interfaces=[Interface(id="IF1", **{"from": "C1"}, to="C1", contract="internal")],
                    tradeoffs=Tradeoffs(pros=["Deterministic fallback"], cons=["Needs refinement"]),
                    risks=[],
                )
            )
            option_id_map["O1"] = "O1"
            component_id_map["C1"] = "C1"
            interface_id_map["IF1"] = "IF1"

        payload = ArchitectureCanonicalPayload(
            options=options,
            option_id_map=option_id_map,
            component_id_map=component_id_map,
            interface_id_map=interface_id_map,
            merge_log=merge.merge_log,
        )
        env = ArtifactEnvelope(
            artifact_type="architecture_canonical",
            artifact_id="architecture_canonical_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("architecture_merge", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_risk_merge(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        requirements: dict[str, object],
        architecture: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["merge.risk_gov_merge"]
        draft_payloads = [
            self._store().read_artifact(drafts[key]).payload
            for key in sorted(k for k in drafts if k.startswith("risk_gov_draft_"))
        ]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "CANONICAL_REQUIREMENTS_JSON": requirements,
                "CANONICAL_ARCHITECTURE_JSON": architecture,
                "RISK_POD_DRAFTS_JSON_ARRAY": draft_payloads,
                "RISK_MERGE_SCHEMA_JSON": schema_json,
            }
        )
        payload = self._call_stage_single(stage, prompt_vars, artifact_refs)
        env = ArtifactEnvelope(
            artifact_type="risk_gov_merge",
            artifact_id="risk_gov_merge_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=list(drafts.values()),
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_risk_canonical(
        self,
        merge_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["canonicalize.risk_gov"]
        merge = RiskGovMergePayload.model_validate(merge_payload)
        risk_id_map: dict[str, str] = {}
        risk_register: list[RiskItem] = []
        for idx, risk in enumerate(merge.risk_register, start=1):
            new_id = f"K{idx}"
            risk_id_map[risk.id] = new_id
            risk_register.append(
                RiskItem(
                    id=new_id,
                    severity=risk.severity,
                    description=risk.description,
                    mitigation=risk.mitigation,
                    acceptance=risk.acceptance,
                )
            )
        tool_policy = merge.tool_policy
        if not tool_policy.stage_policies:
            tool_policy = ToolPolicy(
                stage_policies=[
                    ToolPolicyStage(
                        stage="PLANNING",
                        allowlisted_tools=[],
                        restrictions=[],
                        hitl_triggers=[
                            "network_access",
                            "write_outside_workspace",
                            "shell_exec",
                            "secrets_access",
                        ],
                    )
                ]
            )
        tool_policy = self._normalize_tool_policy(tool_policy)
        hitl_policy = merge.hitl_policy
        if not hitl_policy.when_to_interrupt:
            hitl_policy = HitlPolicy(when_to_interrupt=["blocking_unknowns"], approval_roles=["user"])
        taxonomy = merge.taxonomy_categories or ["risk_closure", "scope", "data_quality"]
        payload = RiskGovCanonicalPayload(
            risk_register=risk_register,
            tool_policy=tool_policy,
            hitl_policy=hitl_policy,
            taxonomy_categories=taxonomy,
            risk_id_map=risk_id_map,
            merge_log=merge.merge_log,
        )
        env = ArtifactEnvelope(
            artifact_type="risk_gov_canonical",
            artifact_id="risk_gov_canonical_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("risk_gov_merge", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_qa_strategy(
        self,
        capsule: dict[str, object],
        drafts: dict[str, str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["merge.qa_strategy"]
        qa_payloads = [
            self._store().read_artifact(drafts[key]).payload
            for key in sorted(k for k in drafts if k.startswith("qa_templates_draft_"))
        ]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "QA_DRAFTS_JSON_ARRAY": qa_payloads,
                "QA_STRATEGY_SCHEMA_JSON": schema_json,
            }
        )
        try:
            payload = self._call_stage_single(stage, prompt_vars, artifact_refs)
            qa_payload = QaStrategyCanonicalPayload.model_validate(payload.model_dump(by_alias=True))
        except Exception:
            if qa_payloads:
                draft = QaTemplatesDraftPayload.model_validate(qa_payloads[0])
                qa_payload = QaStrategyCanonicalPayload(
                    templates=draft.templates,
                    coverage_guidance=draft.coverage_guidance,
                    definition_of_done=draft.definition_of_done,
                    merge_log=["fallback: used first QA draft"],
                )
            else:
                qa_payload = QaStrategyCanonicalPayload(
                    templates=[],
                    coverage_guidance=[],
                    definition_of_done=[],
                    merge_log=["fallback: no QA drafts"],
                )
        env = ArtifactEnvelope(
            artifact_type="qa_strategy_canonical",
            artifact_id="qa_strategy_canonical_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("capsule", "")],
            payload=qa_payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_acceptance_generate(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        architecture: dict[str, object],
        qa_strategy: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["acceptance.generate"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "CANONICAL_REQUIREMENTS_JSON": requirements,
                "QA_TEMPLATES_JSON": qa_strategy,
                "CANONICAL_ARCHITECTURE_JSON": architecture,
                "ACCEPTANCE_DRAFT_SCHEMA_JSON": schema_json,
            }
        )
        must_ids = {r["id"] for r in requirements.get("requirements", []) if r.get("priority") == "MUST"}
        best_payload: AcceptanceDraftPayload | None = None
        best_coverage = -1
        attempts = max(1, stage.max_attempts)
        for _ in range(attempts):
            outputs = self._call_stage_multi(stage, prompt_vars, artifact_refs)
            for output in outputs:
                candidate = AcceptanceDraftPayload.model_validate(output.model_dump(by_alias=True))
                mapped = {rid for at in candidate.acceptance_tests for rid in at.maps_to_requirements}
                coverage = len(must_ids.intersection(mapped))
                if coverage > best_coverage:
                    best_payload = candidate
                    best_coverage = coverage
                if must_ids.issubset(mapped):
                    env = ArtifactEnvelope(
                        artifact_type="acceptance_draft",
                        artifact_id="acceptance_draft_v1",
                        schema_version=SCHEMA_VERSION,
                        created_at=_now(),
                        source_run_id=self.run_id,
                        parents=[artifact_refs.get("requirements_canonical", "")],
                        payload=candidate.model_dump(by_alias=True),
                    )
                    self._write_artifact(self._store(), env, stage=stage.id)
                    return env
        if best_payload is None:
            raise RuntimeError("Acceptance generation produced no outputs")
        raise RuntimeError("Acceptance generation missing MUST coverage after retries")

    def _stage_acceptance_canonical(
        self,
        acceptance_payload: dict[str, object],
        requirements_payload: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["acceptance.canonicalize"]
        acceptance = AcceptanceDraftPayload.model_validate(acceptance_payload)
        valid_requirement_ids = {r["id"] for r in requirements_payload.get("requirements", [])}
        canonical: list[AcceptanceTest] = []
        id_map: dict[str, str] = {}
        counter = 1
        for test in acceptance.acceptance_tests:
            mapped = [rid for rid in test.maps_to_requirements if rid in valid_requirement_ids]
            new_id = f"AT{counter}"
            counter += 1
            id_map[test.id] = new_id
            canonical.append(
                AcceptanceTest(
                    id=new_id,
                    maps_to_requirements=mapped,
                    type=test.type,
                    procedure=test.procedure,
                    pass_criteria=test.pass_criteria,
                )
            )
        payload = AcceptanceCanonicalPayload(acceptance_tests=canonical, id_map=id_map)
        env = ArtifactEnvelope(
            artifact_type="acceptance_canonical",
            artifact_id="acceptance_canonical_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("acceptance_draft", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_alignment(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["audit.alignment"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "CANONICAL_REQUIREMENTS_JSON": requirements,
                "CANONICAL_ACCEPTANCE_JSON": acceptance,
                "CANONICAL_ARCHITECTURE_JSON": architecture,
                "CANONICAL_RISK_GOV_JSON": risk_gov,
                "ALIGNMENT_WARNINGS_SCHEMA_JSON": schema_json,
            }
        )
        outputs = self._call_stage_multi(stage, prompt_vars, artifact_refs)
        merged = _merge_alignment(outputs)
        enabled_calls = [call for call in stage.calls if call.enabled]
        call_outputs: dict[str, AlignmentWarningsPayload] = {}
        for call, output in zip(enabled_calls, outputs, strict=False):
            call_outputs[call.name] = AlignmentWarningsPayload.model_validate(output.model_dump(by_alias=True))
        anchor_output = call_outputs.get("alignment_anchor")
        if anchor_output:
            primary_output = call_outputs.get("alignment_primary")
            control_output = call_outputs.get("alignment_control")
            other_warnings: list[AlignmentWarning] = []
            for item in (primary_output, control_output):
                if item:
                    other_warnings.extend(item.warnings)

            def high_keys(warnings: list[AlignmentWarning]) -> set[str]:
                keys: set[str] = set()
                for warning in warnings:
                    if warning.severity not in {"high", "critical"}:
                        continue
                    pointers = sorted({e.json_pointer for e in warning.evidence})
                    keys.add(f"{warning.summary.strip().lower()}|{','.join(pointers)}")
                return keys

            anchor_keys = high_keys(anchor_output.warnings)
            other_keys = high_keys(other_warnings)
            if anchor_keys.symmetric_difference(other_keys):
                self._event(
                    stage.id,
                    "decision",
                    payload={
                        "anchor_disagreement": True,
                        "anchor_only": sorted(anchor_keys - other_keys),
                        "others_only": sorted(other_keys - anchor_keys),
                    },
                )
        env = ArtifactEnvelope(
            artifact_type="alignment_warnings",
            artifact_id="alignment_warnings_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("acceptance_canonical", "")],
            payload=merged.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_branch_build(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        alignment: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["branch.build"]
        schema_json = self._schema_json(stage.output_schema)
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "CAPSULE_JSON": capsule,
                "CANONICAL_REQUIREMENTS_JSON": requirements,
                "CANONICAL_ACCEPTANCE_JSON": acceptance,
                "CANONICAL_ARCHITECTURE_JSON": architecture,
                "CANONICAL_RISK_GOV_JSON": risk_gov,
                "ALIGNMENT_WARNINGS_JSON_OR_EMPTY": alignment or {"warnings": []},
                "BRANCH_SCHEMA_JSON": schema_json,
            }
        )
        outputs = self._call_stage_multi(stage, prompt_vars, artifact_refs)
        keep_count = int(self._workflow.get("branches_kept_after_prune", 3))
        branches_max = int(self._workflow.get("branches_max", keep_count))
        merged = _merge_branches(outputs, keep_count=keep_count, branches_max=branches_max)
        env = ArtifactEnvelope(
            artifact_type="branch_set",
            artifact_id="branch_set_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("alignment_warnings", "")],
            payload=merged.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_candidate_flow(
        self,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        branch_set: dict[str, object],
        artifact_refs: dict[str, str],
        existing_candidates: dict[str, CandidateState] | None = None,
        existing_judge_context: dict[str, object] | None = None,
        expected_candidate_ids: list[str] | None = None,
        rerun_candidates: set[str] | None = None,
        candidate_cycle: int | None = None,
    ) -> tuple[dict[str, CandidateState], dict[str, object]]:
        stage = self.stage_map["synthesis.candidate_delta"]
        store = self._store()
        branch_payload = BranchSetPayload.model_validate(branch_set)
        branches = branch_payload.branches
        candidates: dict[str, CandidateState] = dict(existing_candidates or {})
        judge_context: dict[str, object] = dict(existing_judge_context or {"summaries": []})
        summaries: list[dict[str, object]] = []
        if isinstance(judge_context.get("summaries"), list):
            summaries = list(judge_context.get("summaries", []))
        judge_context["summaries"] = summaries
        summary_index: dict[str, int] = {}
        for idx, item in enumerate(summaries):
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id", "")).strip()
            if candidate_id:
                summary_index[candidate_id] = idx
        rerun_candidates = set(rerun_candidates or [])
        cycle_value = int(candidate_cycle or 1)
        if cycle_value < 1:
            cycle_value = 1
        draft_refs = [ref for key, ref in artifact_refs.items() if key.endswith("_draft") or "draft" in key]
        candidate_tasks: list[tuple[int, Callable[[], object]]] = []
        candidate_index = 0

        def load_existing_delta(candidate_id: str) -> tuple[CandidateDeltaPayload, str, int] | None:
            delta_ref = self._find_candidate_artifact_ref(
                store,
                "candidate_delta",
                candidate_id=candidate_id,
                prefix="candidate_delta",
            )
            if not delta_ref:
                return None
            delta_env = self._read_artifact(store, delta_ref)
            if delta_env is None or delta_env.artifact_type != "candidate_delta":
                return None
            try:
                delta_payload = CandidateDeltaPayload.model_validate(delta_env.payload)
            except Exception:
                return None
            if delta_payload.candidate_id != candidate_id:
                return None
            return delta_payload, delta_ref, self._artifact_version(delta_ref)

        def run_candidate(
            *,
            branch: BranchSpec,
            call: ModelCall,
            candidate_id: str,
            synthesizer_id: str,
            base_version: int,
        ) -> tuple[CandidateState, str]:
            with self._stage_timer(stage.id, detail=f"candidate={candidate_id}"):
                prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
                prompt_vars.update(
                    {
                        "CANDIDATE_ID": candidate_id,
                        "BRANCH_JSON": branch.model_dump(by_alias=True),
                        "CAPSULE_JSON": capsule,
                        "CANONICAL_REQUIREMENTS_JSON": requirements,
                        "CANONICAL_ACCEPTANCE_JSON": acceptance,
                        "CANONICAL_ARCHITECTURE_JSON": architecture,
                        "CANONICAL_RISK_GOV_JSON": risk_gov,
                        "CANDIDATE_DELTA_SCHEMA_JSON": self._schema_json(stage.output_schema),
                    }
                )
                payload = self._call_stage_call(stage, call, prompt_vars, artifact_refs)
            delta_env = ArtifactEnvelope(
                artifact_type="candidate_delta",
                artifact_id=f"candidate_delta_{candidate_id}_v{base_version}",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[artifact_refs.get("branch_set", "")],
                payload=payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), delta_env, stage=stage.id)
            candidate_refs = dict(artifact_refs)
            candidate_refs[f"candidate_delta_{candidate_id}"] = delta_env.artifact_id

            candidate_state = self._evaluate_candidate(
                candidate_id=candidate_id,
                branch=branch,
                synthesizer_id=synthesizer_id,
                author_model_key=call.model_key,
                delta_payload=payload.model_dump(by_alias=True),
                base_version=base_version,
                candidate_cycle=cycle_value,
                capsule=capsule,
                requirements=requirements,
                acceptance=acceptance,
                architecture=architecture,
                risk_gov=risk_gov,
                draft_refs=draft_refs,
                artifact_refs=candidate_refs,
                )
            return candidate_state, delta_env.artifact_id

        def run_candidate_from_delta(
            *,
            branch: BranchSpec,
            call: ModelCall,
            candidate_id: str,
            synthesizer_id: str,
            base_version: int,
            delta_payload: CandidateDeltaPayload,
            delta_ref: str,
        ) -> tuple[CandidateState, str]:
            candidate_refs = dict(artifact_refs)
            candidate_refs[f"candidate_delta_{candidate_id}"] = delta_ref
            candidate_state = self._evaluate_candidate(
                candidate_id=candidate_id,
                branch=branch,
                synthesizer_id=synthesizer_id,
                author_model_key=call.model_key,
                delta_payload=delta_payload.model_dump(by_alias=True),
                base_version=base_version,
                candidate_cycle=cycle_value,
                capsule=capsule,
                requirements=requirements,
                acceptance=acceptance,
                architecture=architecture,
                risk_gov=risk_gov,
                draft_refs=draft_refs,
                artifact_refs=candidate_refs,
            )
            return candidate_state, delta_ref

        def upsert_summary(candidate_state: CandidateState) -> None:
            summary = {
                "candidate_id": candidate_state.candidate_id,
                "candidate_ref": candidate_state.plan_ref,
                "branch_id": candidate_state.branch_id,
                "author_model": candidate_state.author_model,
                "validator_status": candidate_state.validator_status,
                "triage_blocked": candidate_state.triage_blocked,
                "failure_mode_pass": candidate_state.failure_mode_pass,
            }
            existing_idx = summary_index.get(candidate_state.candidate_id)
            if existing_idx is None:
                summary_index[candidate_state.candidate_id] = len(summaries)
                summaries.append(summary)
            else:
                summaries[existing_idx] = summary

        if expected_candidate_ids is None:
            expected_candidate_ids = []
        for branch in branches:
            for call_idx, call in enumerate(stage.calls):
                if not call.enabled:
                    continue
                synthesizer_id = call.name or f"SYN_{call_idx + 1}"
                candidate_id = f"{branch.branch_id}{chr(ord('A') + call_idx)}"
                if candidate_id not in expected_candidate_ids:
                    expected_candidate_ids.append(candidate_id)
                should_rerun = candidate_id in rerun_candidates
                if candidate_id in candidates and not should_rerun:
                    artifact_refs.setdefault(f"candidate_delta_{candidate_id}", candidates[candidate_id].delta_ref)
                    upsert_summary(candidates[candidate_id])
                    continue
                if not should_rerun:
                    existing_delta = load_existing_delta(candidate_id)
                    if existing_delta is not None:
                        delta_payload, delta_ref, base_version = existing_delta
                        artifact_refs.setdefault(f"candidate_delta_{candidate_id}", delta_ref)
                        candidate_tasks.append(
                            (
                                candidate_index,
                                lambda branch=branch,
                                call=call,
                                candidate_id=candidate_id,
                                synthesizer_id=synthesizer_id,
                                base_version=base_version,
                                delta_payload=delta_payload,
                                delta_ref=delta_ref: run_candidate_from_delta(
                                    branch=branch,
                                    call=call,
                                    candidate_id=candidate_id,
                                    synthesizer_id=synthesizer_id,
                                    base_version=base_version,
                                    delta_payload=delta_payload,
                                    delta_ref=delta_ref,
                                ),
                            )
                        )
                        candidate_index += 1
                        continue
                base_version = self._next_candidate_version(candidates, candidate_id)
                candidate_tasks.append(
                    (
                        candidate_index,
                        lambda branch=branch,
                        call=call,
                        candidate_id=candidate_id,
                        synthesizer_id=synthesizer_id,
                        base_version=base_version: run_candidate(
                            branch=branch,
                            call=call,
                            candidate_id=candidate_id,
                            synthesizer_id=synthesizer_id,
                            base_version=base_version,
                        ),
                    )
                )
                candidate_index += 1

        def _routing_state(candidates_complete: bool) -> dict[str, object]:
            return {
                "candidate_count": len(candidates),
                "branch_count": len(branches),
                "branch_ids": [b.branch_id for b in branches],
                "candidates": [self._candidate_metadata(c) for c in candidates.values()],
                "expected_candidates": list(expected_candidate_ids),
                "candidates_complete": candidates_complete,
                "candidate_cycle": cycle_value,
            }

        def checkpoint_partial() -> None:
            if not expected_candidate_ids:
                return
            if len(candidates) >= len(expected_candidate_ids):
                return
            self._checkpoint("synthesis.candidate_delta", artifact_refs, routing_state=_routing_state(False))

        def checkpoint_candidate_done() -> None:
            if not self._checkpoint_candidates:
                return
            if not expected_candidate_ids:
                return
            candidates_complete = len(candidates) >= len(expected_candidate_ids)
            self._checkpoint(
                "analysis.failure_modes",
                artifact_refs,
                routing_state=_routing_state(candidates_complete),
            )

        if candidate_tasks:
            max_workers = min(self._max_parallel_llm_calls, len(candidate_tasks))
            if max_workers <= 1:
                for _, task in candidate_tasks:
                    candidate_state, delta_ref = task()
                    candidates[candidate_state.candidate_id] = candidate_state
                    artifact_refs[f"candidate_delta_{candidate_state.candidate_id}"] = delta_ref
                    upsert_summary(candidate_state)
                    if self._checkpoint_candidates:
                        checkpoint_candidate_done()
                    else:
                        checkpoint_partial()
            else:
                errors: list[tuple[int, Exception]] = []
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {executor.submit(task): idx for idx, task in candidate_tasks}
                    for future in as_completed(future_map):
                        idx = future_map[future]
                        try:
                            candidate_state, delta_ref = future.result()
                        except Exception as exc:
                            errors.append((idx, exc))
                            continue
                        candidates[candidate_state.candidate_id] = candidate_state
                        artifact_refs[f"candidate_delta_{candidate_state.candidate_id}"] = delta_ref
                        upsert_summary(candidate_state)
                        if self._checkpoint_candidates:
                            checkpoint_candidate_done()
                        else:
                            checkpoint_partial()
                if errors:
                    errors.sort(key=lambda item: item[0])
                    raise errors[0][1]
        return candidates, judge_context

    def _evaluate_candidate(
        self,
        *,
        candidate_id: str,
        branch: BranchSpec,
        synthesizer_id: str,
        author_model_key: str,
        delta_payload: dict[str, object],
        base_version: int,
        candidate_cycle: int,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        draft_refs: list[str],
        artifact_refs: dict[str, str],
    ) -> CandidateState:
        with self._stage_timer("assemble.plan_candidate", detail=f"candidate={candidate_id}"):
            plan_candidate = self._assemble_plan_candidate(
                candidate_id=candidate_id,
                branch=branch,
                synthesizer_id=synthesizer_id,
                delta_payload=delta_payload,
                version=base_version,
                capsule=capsule,
                requirements=requirements,
                acceptance=acceptance,
                architecture=architecture,
                risk_gov=risk_gov,
                draft_refs=draft_refs,
                artifact_refs=artifact_refs,
            )
        plan_ref = plan_candidate.artifact_id
        current_version = base_version
        consistency_stage = self.stage_map["audit.consistency"]
        trace_stage = self.stage_map["audit.trace_graph"]

        def run_consistency() -> tuple[ConsistencyFindingsPayload, str]:
            with self._stage_timer(consistency_stage.id, detail=f"candidate={candidate_id}"):
                consistency_vars = self._base_prompt_vars(consistency_stage.id, artifact_refs)
                consistency_vars.update(
                    {
                        "CANDIDATE_REF": plan_ref,
                        "PLAN_CANDIDATE_JSON": plan_candidate.payload,
                        "CONSISTENCY_SCHEMA_JSON": self._schema_json(consistency_stage.output_schema),
                    }
                )
                consistency_outputs = self._call_stage_multi(consistency_stage, consistency_vars, artifact_refs)
                consistency_payload = _merge_consistency(consistency_outputs, candidate_ref=plan_ref)
                consistency_env = ArtifactEnvelope(
                    artifact_type="consistency_findings",
                    artifact_id=f"consistency_{candidate_id}_v1",
                    schema_version=SCHEMA_VERSION,
                    created_at=_now(),
                    source_run_id=self.run_id,
                    parents=[plan_ref],
                    payload=consistency_payload.model_dump(by_alias=True),
                )
                self._write_artifact(self._store(), consistency_env, stage=consistency_stage.id)
            return consistency_payload, consistency_env.artifact_id

        def run_trace() -> tuple[BaseModel, str]:
            with self._stage_timer(trace_stage.id, detail=f"candidate={candidate_id}"):
                trace_vars = self._base_prompt_vars(trace_stage.id, artifact_refs)
                trace_vars.update(
                    {
                        "CANDIDATE_REF": plan_ref,
                        "PLAN_CANDIDATE_JSON": plan_candidate.payload,
                        "TRACE_SAMPLE_CHECKS_JSON": _trace_sample_checks(plan_candidate.payload),
                        "TRACE_AUDIT_SCHEMA_JSON": self._schema_json(trace_stage.output_schema),
                    }
                )
                call_map = {call.name: call for call in trace_stage.calls if call.enabled}
                primary_call = call_map.get("trace_primary") or next(
                    (call for call in trace_stage.calls if call.enabled), None
                )
                anchor_call = call_map.get("trace_anchor")
                fallback_call = call_map.get("trace_fallback")
                if primary_call is None:
                    raise RuntimeError("Trace graph stage has no enabled primary call")

                def call_trace(call: ModelCall, label: str) -> tuple[TraceGraphFindingsPayload, str]:
                    payload = self._call_stage_call(trace_stage, call, trace_vars, artifact_refs)
                    trace_payload = TraceGraphFindingsPayload.model_validate(payload.model_dump(by_alias=True))
                    trace_env = ArtifactEnvelope(
                        artifact_type="trace_graph_findings",
                        artifact_id=f"trace_graph_{candidate_id}_{label}_v{current_version}",
                        schema_version=SCHEMA_VERSION,
                        created_at=_now(),
                        source_run_id=self.run_id,
                        parents=[plan_ref],
                        payload=trace_payload.model_dump(by_alias=True),
                    )
                    self._write_artifact(self._store(), trace_env, stage=trace_stage.id)
                    return trace_payload, trace_env.artifact_id

                primary_payload: TraceGraphFindingsPayload | None = None
                anchor_payload: TraceGraphFindingsPayload | None = None
                fallback_payload: TraceGraphFindingsPayload | None = None
                primary_ref = ""
                anchor_ref = ""
                fallback_ref = ""
                primary_error: Exception | None = None

                try:
                    primary_payload, primary_ref = call_trace(primary_call, "primary")
                except Exception as exc:
                    primary_error = exc

                if anchor_call is None:
                    raise RuntimeError("Trace graph stage missing trace_anchor call")
                anchor_payload, anchor_ref = call_trace(anchor_call, "anchor")

                def high_keys(payload: TraceGraphFindingsPayload) -> set[str]:
                    keys: set[str] = set()
                    for finding in payload.findings:
                        if finding.severity not in {"high", "critical"}:
                            continue
                        pointers = sorted({e.json_pointer for e in finding.evidence})
                        keys.add(f"{finding.summary.strip().lower()}|{','.join(pointers)}")
                    return keys

                needs_fallback = primary_error is not None
                if primary_payload is not None and anchor_payload is not None:
                    if high_keys(primary_payload).symmetric_difference(high_keys(anchor_payload)):
                        needs_fallback = True

                if needs_fallback:
                    self._event(
                        trace_stage.id,
                        "decision",
                        payload={
                            "fallback_triggered": True,
                            "reason": "primary_error" if primary_error else "anchor_disagreement",
                        },
                    )
                    if fallback_call is None:
                        if primary_error:
                            raise primary_error
                        raise RuntimeError("Trace graph disagreement without fallback call configured")
                    fallback_payload, fallback_ref = call_trace(fallback_call, "fallback")

                outputs_to_merge: list[TraceGraphFindingsPayload] = []
                parents: list[str] = []
                if primary_payload is not None:
                    outputs_to_merge.append(primary_payload)
                    if primary_ref:
                        parents.append(primary_ref)
                if anchor_payload is not None:
                    outputs_to_merge.append(anchor_payload)
                    if anchor_ref:
                        parents.append(anchor_ref)
                if fallback_payload is not None:
                    outputs_to_merge.append(fallback_payload)
                    if fallback_ref:
                        parents.append(fallback_ref)

                merged_payload = _merge_trace_findings(outputs_to_merge, candidate_ref=plan_ref)
                trace_env = ArtifactEnvelope(
                    artifact_type="trace_graph_findings",
                    artifact_id=f"trace_graph_{candidate_id}_v{current_version}",
                    schema_version=SCHEMA_VERSION,
                    created_at=_now(),
                    source_run_id=self.run_id,
                    parents=parents or [plan_ref],
                    payload=merged_payload.model_dump(by_alias=True),
                )
                self._write_artifact(self._store(), trace_env, stage=trace_stage.id)
            return merged_payload, trace_env.artifact_id

        def run_validator() -> tuple[ValidatorReportPayload, str]:
            with self._stage_timer("validate.plan_candidate", detail=f"candidate={candidate_id}"):
                validator_report = validate_candidate(
                    plan_ref,
                    PlanPackage.model_validate(plan_candidate.payload["plan_package"]),
                )
                validator_env = ArtifactEnvelope(
                    artifact_type="validator_report",
                    artifact_id=f"validator_{candidate_id}_v{current_version}",
                    schema_version=SCHEMA_VERSION,
                    created_at=_now(),
                    source_run_id=self.run_id,
                    parents=[plan_ref],
                    payload=validator_report.model_dump(by_alias=True),
                )
                self._write_artifact(self._store(), validator_env, stage="validate.plan_candidate")
            return validator_report, validator_env.artifact_id

        initial_tasks: list[tuple[int, Callable[[], object]]] = [
            (0, run_consistency),
            (1, run_trace),
            (2, run_validator),
        ]
        initial_results, initial_errors = self._run_parallel_tasks(initial_tasks)
        if initial_errors:
            initial_errors.sort(key=lambda item: item[0])
            raise initial_errors[0][1]
        consistency_payload, _ = initial_results[0]
        trace_payload, _ = initial_results[1]
        validator_report, validator_ref = initial_results[2]

        triage_stage = self.stage_map["triage.critics"]
        with self._stage_timer(triage_stage.id, detail=f"candidate={candidate_id}"):
            triage_vars = self._base_prompt_vars(triage_stage.id, artifact_refs)
            triage_vars.update(
                {
                    "CANDIDATE_REF": plan_ref,
                    "PLAN_CANDIDATE_JSON": plan_candidate.payload,
                    "VALIDATOR_REPORT_JSON": validator_report.model_dump(by_alias=True),
                    "CONSISTENCY_FINDINGS_JSON_OR_EMPTY": consistency_payload.model_dump(by_alias=True),
                    "TRIAGE_SCHEMA_JSON": self._schema_json(triage_stage.output_schema),
                }
            )
            triage_outputs = self._call_stage_multi(triage_stage, triage_vars, artifact_refs)
            triage_payload = _merge_triage(triage_outputs, candidate_ref=plan_ref)
            triage_env = ArtifactEnvelope(
                artifact_type="triage_report",
                artifact_id=f"triage_{candidate_id}_v{current_version}",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[plan_ref, validator_ref],
                payload=triage_payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), triage_env, stage=triage_stage.id)

        plan_payload = plan_candidate.payload
        patched = False
        max_repairs = int(self._runtime_cfg.get("orchestrator", {}).get("max_repairs_per_candidate", 1))
        repair_round = 0
        reroute_requested = False
        while (
            validator_report.overall_status != "PASS" or triage_payload.verdict != "approve"
        ) and repair_round < max_repairs:
            if self._checkpoint_repair_rounds:
                self._checkpoint(
                    "repair.patch_generate",
                    artifact_refs,
                    routing_state={
                        "candidate_id": candidate_id,
                        "candidate_cycle": candidate_cycle,
                        "repair_round": repair_round + 1,
                        "plan_ref": plan_ref,
                        "validator_ref": validator_ref,
                        "triage_ref": triage_env.artifact_id,
                    },
                )
            base_plan_ref = plan_ref
            patch_stage = self.stage_map["repair.patch_generate"]
            with self._stage_timer(
                patch_stage.id,
                detail=f"candidate={candidate_id} round={repair_round + 1}",
            ):
                patch_vars = self._base_prompt_vars(patch_stage.id, artifact_refs)
                patch_vars.update(
                    {
                        "CANDIDATE_REF": plan_ref,
                        "PLAN_CANDIDATE_JSON": plan_payload,
                        "VALIDATOR_REPORT_JSON": validator_report.model_dump(by_alias=True),
                        "TRIAGE_REPORT_JSON": triage_payload.model_dump(by_alias=True),
                        "FAILURE_MODE_FINDINGS_JSON_OR_EMPTY": {},
                        "TRACE_GRAPH_FINDINGS_JSON_OR_EMPTY": trace_payload.model_dump(by_alias=True),
                        "PATCH_OR_REROUTE_SCHEMA_JSON": self._schema_json(patch_stage.output_schema),
                    }
                )
                patch_outputs = self._call_stage_multi(patch_stage, patch_vars, artifact_refs)
            patch_refs: list[str] = []
            for idx, patch_output in enumerate(patch_outputs, start=1):
                patch_env = ArtifactEnvelope(
                    artifact_type="patch_or_reroute",
                    artifact_id=f"patch_{candidate_id}_{idx}_v{repair_round + 1}",
                    schema_version=SCHEMA_VERSION,
                    created_at=_now(),
                    source_run_id=self.run_id,
                    parents=[plan_ref],
                    payload=patch_output.model_dump(by_alias=True),
                )
                self._write_artifact(self._store(), patch_env, stage=patch_stage.id)
                patch_refs.append(patch_env.artifact_id)

            with self._stage_timer(
                "repair.patch_apply_and_select",
                detail=f"candidate={candidate_id} round={repair_round + 1}",
            ):
                patch_apply_stage = self.stage_map["repair.patch_apply_and_select"]
                patch_policy = dict(patch_apply_stage.policy or {})
                max_patch_ops = patch_policy.get("max_patch_ops")
                forbidden_paths = patch_policy.get("forbidden_paths")
                forbidden_ops = patch_policy.get("forbidden_ops")
                selection_order = patch_policy.get("selection_criteria_order")
                patch_apply_payload, patched_payload, _ = _apply_and_select_patch(
                    candidate_ref=plan_ref,
                    plan_payload=plan_payload,
                    patch_outputs=[
                        PatchOrReroutePayload.model_validate(p.model_dump(by_alias=True)) for p in patch_outputs
                    ],
                    patch_refs=patch_refs,
                    forbid_prefixes=FORBIDDEN_PATCH_PREFIXES,
                    max_patch_ops=int(max_patch_ops) if max_patch_ops is not None else None,
                    forbidden_paths=list(forbidden_paths) if isinstance(forbidden_paths, list) else None,
                    forbidden_ops=list(forbidden_ops) if isinstance(forbidden_ops, list) else None,
                    selection_criteria_order=list(selection_order)
                    if isinstance(selection_order, list)
                    else None,
                    reroute_target_fallback=triage_payload.reroute_target,
                )

            if patched_payload is not None:
                patched = True
                new_version = current_version + 1
                with self._stage_timer("validate.plan_candidate", detail=f"candidate={candidate_id}"):
                    if (
                        isinstance(patched_payload, dict)
                        and isinstance(patched_payload.get("plan_package"), dict)
                        and isinstance(patched_payload["plan_package"].get("meta"), dict)
                    ):
                        patched_payload["plan_package"]["meta"]["version"] = f"v{new_version}"
                    patched_env = ArtifactEnvelope(
                        artifact_type="plan_candidate",
                        artifact_id=f"plan_candidate_{candidate_id}_v{new_version}",
                        schema_version=SCHEMA_VERSION,
                        created_at=_now(),
                        source_run_id=self.run_id,
                        parents=[base_plan_ref],
                        payload=patched_payload,
                    )
                    self._write_artifact(self._store(), patched_env, stage="assemble.plan_candidate")
                    plan_payload = patched_env.payload
                    plan_ref = patched_env.artifact_id
                    validator_report = validate_candidate(
                        plan_ref,
                        PlanPackage.model_validate(plan_payload["plan_package"]),
                    )
                    validator_env = ArtifactEnvelope(
                        artifact_type="validator_report",
                        artifact_id=f"validator_{candidate_id}_v{new_version}",
                        schema_version=SCHEMA_VERSION,
                        created_at=_now(),
                        source_run_id=self.run_id,
                        parents=[plan_ref],
                        payload=validator_report.model_dump(by_alias=True),
                    )
                    self._write_artifact(self._store(), validator_env, stage="validate.plan_candidate")
                    validator_ref = validator_env.artifact_id
                    current_version = new_version
                    patch_apply_payload.applied_candidate_ref = plan_ref

            patch_apply_env = ArtifactEnvelope(
                artifact_type="patch_apply_result",
                artifact_id=f"patch_apply_{candidate_id}_v{repair_round + 1}",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[base_plan_ref] + patch_refs,
                payload=patch_apply_payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), patch_apply_env, stage="repair.patch_apply_and_select")
            if self._checkpoint_repair_rounds:
                self._checkpoint(
                    "repair.patch_apply_and_select",
                    artifact_refs,
                    routing_state={
                        "candidate_id": candidate_id,
                        "candidate_cycle": candidate_cycle,
                        "repair_round": repair_round + 1,
                        "plan_ref": plan_ref,
                        "validator_ref": validator_ref,
                        "triage_ref": triage_env.artifact_id,
                        "patch_apply_ref": patch_apply_env.artifact_id,
                    },
                )
            if patch_apply_payload.selected_patch_ref is None:
                if patch_apply_payload.reroute_target:
                    reroute_requested = True
                    self._event(
                        "repair.patch_apply_and_select",
                        "decision",
                        payload={
                            "reroute_requested": True,
                            "reroute_target": patch_apply_payload.reroute_target,
                        },
                    )
                    break
                repair_round += 1
                continue
            repair_round += 1
            if validator_report.overall_status == "PASS":
                break

        failure_stage = self.stage_map["analysis.failure_modes"]
        with self._stage_timer(failure_stage.id, detail=f"candidate={candidate_id}"):
            failure_vars = self._base_prompt_vars(failure_stage.id, artifact_refs)
            failure_vars.update(
                {
                    "TAXONOMY_CATEGORIES_JSON": risk_gov.get("taxonomy_categories", []),
                    "CANDIDATE_REF": plan_ref,
                    "PLAN_CANDIDATE_JSON": plan_payload,
                    "FAILURE_MODE_SCHEMA_JSON": self._schema_json(failure_stage.output_schema),
                }
            )
            failure_tasks: list[tuple[int, Callable[[], object]]] = []
            for idx, call in enumerate(failure_stage.calls):
                if not call.enabled:
                    continue
                failure_tasks.append(
                    (
                        idx,
                        lambda call=call: self._call_stage_call(
                            failure_stage,
                            call,
                            failure_vars,
                            artifact_refs,
                        ),
                    )
                )
            failure_results, _ = self._run_parallel_tasks(failure_tasks)
            failure_outputs = [failure_results[idx] for idx in sorted(failure_results)]
            failure_payload = _merge_failure_modes(failure_outputs, candidate_ref=plan_ref)
            failure_env = ArtifactEnvelope(
                artifact_type="failure_mode_findings",
                artifact_id=f"failure_modes_{candidate_id}_v{current_version}",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[plan_ref],
                payload=failure_payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), failure_env, stage=failure_stage.id)

        triage_blocked = triage_payload.verdict == "reject" or any(d.label == "blocker" for d in triage_payload.defects)
        if reroute_requested:
            triage_blocked = True
        if patched and validator_report.overall_status == "PASS":
            triage_blocked = False

        state = CandidateState(
            candidate_id=candidate_id,
            branch_id=branch.branch_id,
            synthesizer_id=synthesizer_id,
            author_model=str(self._models.get(author_model_key, {}).get("model_id", author_model_key)),
            author_family=self._model_family(author_model_key),
            delta_ref=artifact_refs.get(f"candidate_delta_{candidate_id}", ""),
            plan_ref=plan_ref,
            payload=PlanCandidatePayload.model_validate(plan_payload),
            validator_ref=validator_ref,
            triage_ref=triage_env.artifact_id,
            failure_mode_ref=failure_env.artifact_id,
            validator_report=validator_report,
            triage_report=triage_payload,
            failure_mode=failure_payload,
            validator_status=validator_report.overall_status,
            triage_blocked=triage_blocked,
            failure_mode_pass=failure_payload.closure_pass,
        )
        return state

    def _stage_judge_tournament(
        self,
        candidates: dict[str, CandidateState],
        judge_context: dict[str, object],
        artifact_refs: dict[str, str],
        *,
        allow_fallback: bool = False,
    ) -> tuple[str, dict[str, object]]:
        with self._stage_timer("judge.pairwise"):
            viable = [
                c.candidate_id
                for c in candidates.values()
                if c.validator_status == "PASS" and not c.triage_blocked and c.failure_mode_pass
            ]
            if not viable:
                if not allow_fallback:
                    raise RuntimeError("No candidates passed validators, triage, and failure-mode gates")
                fallback_reason = "no_unblocked_pass"
                viable = [
                    c.candidate_id
                    for c in candidates.values()
                    if c.validator_status == "PASS" and c.failure_mode_pass
                ]
                if not viable:
                    fallback_reason = "no_pass_with_failure_mode_pass"
                    viable = [
                        c.candidate_id
                        for c in candidates.values()
                        if c.validator_status in {"PASS", "FAIL_REPAIRABLE"} and c.failure_mode_pass
                    ]
                if not viable:
                    fallback_reason = "no_failure_mode_pass"
                    viable = [c.candidate_id for c in candidates.values()]
                viable = sorted(viable)
                self._event(
                    "judge.pairwise",
                    "decision",
                    payload={
                        "fallback_reason": fallback_reason,
                        "eligible_candidates": viable,
                    },
                )
            bracket = list(viable)
            pairwise_refs: list[str] = []
            pairwise_results: list[dict[str, object]] = []
            arbitrator_ref: str | None = None

            while len(bracket) > 1:
                slots: list[tuple[str, int | str]] = []
                pairs: list[tuple[int, str, str]] = []
                pair_index = 0
                for idx in range(0, len(bracket), 2):
                    if idx + 1 >= len(bracket):
                        slots.append(("bye", bracket[idx]))
                        continue
                    a_id = bracket[idx]
                    b_id = bracket[idx + 1]
                    slots.append(("pair", pair_index))
                    pairs.append((pair_index, a_id, b_id))
                    pair_index += 1

                pair_tasks: list[tuple[int, Callable[[], object]]] = []
                for pair_idx, a_id, b_id in pairs:
                    pair_tasks.append(
                        (
                            pair_idx,
                            lambda a_id=a_id, b_id=b_id: self._judge_pair(
                                candidates[a_id],
                                candidates[b_id],
                                judge_context,
                                artifact_refs,
                            ),
                        )
                    )
                pair_results, pair_errors = self._run_parallel_tasks(pair_tasks)
                if pair_errors:
                    pair_errors.sort(key=lambda item: item[0])
                    raise pair_errors[0][1]

                pair_lookup = {pair_idx: (a_id, b_id) for pair_idx, a_id, b_id in pairs}
                next_round: list[str] = []
                for slot_kind, slot_value in slots:
                    if slot_kind == "bye":
                        next_round.append(str(slot_value))
                        continue
                    pair_idx = int(slot_value)
                    a_id, b_id = pair_lookup[pair_idx]
                    winner, refs, memo = pair_results[pair_idx]
                    pairwise_refs.extend(refs)
                    pairwise_results.append(
                        {
                            "pair": [a_id, b_id],
                            "winner": winner,
                            "judge_refs": refs,
                            "arbitrator_ref": memo,
                        }
                    )
                    if memo:
                        arbitrator_ref = memo
                    next_round.append(winner)
                bracket = next_round

            if not bracket:
                raise RuntimeError("No candidates available for judging")
            judge_artifacts = {
                "pairwise_refs": pairwise_refs,
                "pairwise_results": pairwise_results,
                "arbitrator_ref": arbitrator_ref,
            }
            return bracket[0], judge_artifacts

    def _judge_pair(
        self,
        candidate_a: CandidateState,
        candidate_b: CandidateState,
        judge_context: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> tuple[str, list[str], str | None]:
        stage = self.stage_map["judge.pairwise"]
        judge_models = self._select_judges_for_pair(candidate_a, candidate_b)
        refs: list[str] = []
        results: list[JudgePairwisePayload] = []
        weights = self._judge_policy.get("weights") or {"quality": 5, "clarity": 3, "risk": 2}
        prompt_base = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_base.update(
            {
                "JUDGE_MATCH_CONTEXT_JSON": {
                    "candidate_a_id": candidate_a.candidate_id,
                    "candidate_b_id": candidate_b.candidate_id,
                    "candidate_a_branch": candidate_a.branch_id,
                    "candidate_b_branch": candidate_b.branch_id,
                    "candidate_a_author": candidate_a.author_model,
                    "candidate_b_author": candidate_b.author_model,
                },
                "WEIGHTS_JSON": weights,
                "CANDIDATE_A_REF": candidate_a.plan_ref,
                "CANDIDATE_A_JSON": candidate_a.payload.model_dump(by_alias=True),
                "CANDIDATE_B_REF": candidate_b.plan_ref,
                "CANDIDATE_B_JSON": candidate_b.payload.model_dump(by_alias=True),
                "JUDGE_SUMMARIES_JSON": judge_context.get("summaries", []),
                "JUDGE_PAIRWISE_SCHEMA_JSON": self._schema_json(stage.output_schema),
            }
        )

        def run_judge(idx: int, model_key: str) -> JudgePairwisePayload:
            call = ModelCall(
                name=f"judge_{idx}",
                model_key=model_key,
                profile="AUDIT_JUDGE",
                max_output_tokens=None,
                overrides={},
            )
            prompt_vars = dict(prompt_base)
            output = self._call_stage_call(stage, call, prompt_vars, artifact_refs)
            return JudgePairwisePayload.model_validate(output.model_dump(by_alias=True))

        judge_tasks: list[tuple[int, Callable[[], object]]] = []
        for idx, model_key in enumerate(judge_models, start=1):
            judge_tasks.append((idx, lambda idx=idx, model_key=model_key: run_judge(idx, model_key)))

        judge_results, judge_errors = self._run_parallel_tasks(judge_tasks)
        failed_slots: list[int] = []
        if judge_errors:
            failed_slots = [idx for idx, _ in judge_errors]
            self._event(
                "judge.pairwise",
                "decision",
                payload={
                    "action": "judge_retry_with_fallback",
                    "failed_slots": failed_slots,
                    "failed_models": [
                        {
                            "slot": idx,
                            "model": judge_models[idx - 1] if 0 <= idx - 1 < len(judge_models) else "unknown",
                            "error": str(exc),
                        }
                        for idx, exc in judge_errors
                    ],
                },
            )

        if failed_slots:
            pool = [str(m) for m in self._judge_policy.get("preferred_judge_pool", [])]
            used_models = set(judge_models)
            fallback_models = [m for m in pool if m not in used_models]
            for slot in sorted(failed_slots):
                replacement_found = False
                while fallback_models:
                    candidate_model = fallback_models.pop(0)
                    try:
                        judge_results[slot] = run_judge(slot, candidate_model)
                        used_models.add(candidate_model)
                        self._event(
                            "judge.pairwise",
                            "decision",
                            payload={
                                "action": "judge_fallback_used",
                                "slot": slot,
                                "model": candidate_model,
                            },
                        )
                        replacement_found = True
                        break
                    except Exception as exc:
                        self._event(
                            "judge.pairwise",
                            "decision",
                            payload={
                                "action": "judge_fallback_failed",
                                "slot": slot,
                                "model": candidate_model,
                                "error": str(exc),
                            },
                        )
                        continue
                if not replacement_found:
                    if judge_errors:
                        judge_errors.sort(key=lambda item: item[0])
                        raise judge_errors[0][1]
                    raise RuntimeError("Judge fallback exhausted without successful replacements")
        for idx in sorted(judge_results):
            payload = judge_results[idx]
            result_env = ArtifactEnvelope(
                artifact_type="judge_pairwise_result",
                artifact_id=f"judge_pair_{candidate_a.candidate_id}_{candidate_b.candidate_id}_{idx}_v1",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[candidate_a.plan_ref, candidate_b.plan_ref],
                payload=payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), result_env, stage=stage.id)
            refs.append(result_env.artifact_id)
            results.append(payload)

        wins = {"a": 0, "b": 0}
        for res in results:
            wins[res.winner] += 1
        if wins["a"] > wins["b"]:
            return candidate_a.candidate_id, refs, None
        if wins["b"] > wins["a"]:
            return candidate_b.candidate_id, refs, None

        arbitrator_ref, winner_id = self._run_arbitrator(candidate_a, candidate_b, results, artifact_refs)
        return winner_id, refs, arbitrator_ref

    def _run_arbitrator(
        self,
        candidate_a: CandidateState,
        candidate_b: CandidateState,
        judge_outputs: list[JudgePairwisePayload],
        artifact_refs: dict[str, str],
    ) -> tuple[str, str]:
        with self._stage_timer("judge.arbitrator"):
            stage = self.stage_map["judge.arbitrator"]
            prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
            prompt_vars.update(
                {
                    "DISPUTE_CONTEXT_JSON": {
                        "candidate_a": candidate_a.candidate_id,
                        "candidate_b": candidate_b.candidate_id,
                        "reason": "tie",
                    },
                    "CANDIDATES_REFS_AND_JSON": [
                        {"ref": candidate_a.plan_ref, "payload": candidate_a.payload.model_dump(by_alias=True)},
                        {"ref": candidate_b.plan_ref, "payload": candidate_b.payload.model_dump(by_alias=True)},
                    ],
                    "JUDGE_OUTPUTS_JSON": [j.model_dump(by_alias=True) for j in judge_outputs],
                    "ARBITRATOR_SCHEMA_JSON": self._schema_json(stage.output_schema),
                }
            )
            output = self._call_stage_single(stage, prompt_vars, artifact_refs)
            payload = ArbitrationDecisionPayload.model_validate(output.model_dump(by_alias=True))
            env = ArtifactEnvelope(
                artifact_type="arbitration_decision",
                artifact_id=f"arbitration_{candidate_a.candidate_id}_{candidate_b.candidate_id}_v1",
                schema_version=SCHEMA_VERSION,
                created_at=_now(),
                source_run_id=self.run_id,
                parents=[candidate_a.plan_ref, candidate_b.plan_ref],
                payload=payload.model_dump(by_alias=True),
            )
            self._write_artifact(self._store(), env, stage=stage.id)
            if payload.decision != "select_winner" or not payload.winner_ref:
                raise RuntimeError("Arbitrator requested HITL or returned no winner")
            winner_id = (
                candidate_a.candidate_id if payload.winner_ref == candidate_a.plan_ref else candidate_b.candidate_id
            )
            return env.artifact_id, winner_id

    def _stage_freeze(
        self,
        winner_state: CandidateState,
        judge_artifacts: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        remaining_questions = [q.id for q in winner_state.payload.plan_package.project_capsule.open_questions]
        accepted_risks = [r.id for r in winner_state.payload.plan_package.risk_register if r.acceptance is not None]
        payload = FreezeRecordPayload(
            winner_candidate_ref=winner_state.plan_ref,
            frozen_plan_ref=f"frozen_{winner_state.candidate_id}_vFinal",
            why_winner=["Passed validators", "Won tournament"],
            tradeoffs=[FreezeTradeoff(topic="tradeoff", winner_reason="Higher score", runner_up_reason="Lower score")],
            judge_summary_refs=list(judge_artifacts.get("pairwise_refs", [])),
            validator_summary_ref=winner_state.validator_ref or "",
            remaining_open_questions=remaining_questions,
            accepted_risks=accepted_risks,
        )
        env = ArtifactEnvelope(
            artifact_type="freeze_record",
            artifact_id="freeze_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[winner_state.plan_ref],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage="freeze.freeze")

        frozen_payload = PlanFrozenPayload(
            candidate_id=winner_state.payload.candidate_id,
            branch_id=winner_state.payload.branch_id,
            synthesizer_id=winner_state.payload.synthesizer_id,
            inputs=winner_state.payload.inputs,
            plan_package=winner_state.payload.plan_package,
            plan_status="FROZEN",
            freeze_record_ref=env.artifact_id,
        )
        frozen_payload.plan_package.meta.version = "vFinal"
        frozen_payload.plan_package.meta.plan_status = "FROZEN"
        frozen_env = ArtifactEnvelope(
            artifact_type="plan_frozen",
            artifact_id=f"frozen_{winner_state.candidate_id}_vFinal",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[winner_state.plan_ref],
            payload=frozen_payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), frozen_env, stage="freeze.freeze")
        artifact_refs["frozen_plan"] = frozen_env.artifact_id
        return env

    def _stage_decision_record(
        self,
        winner_state: CandidateState,
        candidates: dict[str, CandidateState],
        judge_artifacts: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        stage = self.stage_map["freeze.decision_record"]
        runner_ups = [c.plan_ref for cid, c in candidates.items() if cid != winner_state.candidate_id]
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "WINNER_REF": winner_state.plan_ref,
                "WINNER_PLAN_JSON": winner_state.payload.model_dump(by_alias=True),
                "RUNNER_UPS_JSON_OR_EMPTY": runner_ups,
                "TOURNAMENT_RECORD_JSON": judge_artifacts.get("pairwise_results", []),
                "DECISION_RECORD_SCHEMA_JSON": self._schema_json(stage.output_schema),
            }
        )
        output = self._call_stage_single(stage, prompt_vars, artifact_refs)
        payload = DecisionRecordPayload.model_validate(output.model_dump(by_alias=True))
        env = ArtifactEnvelope(
            artifact_type="decision_record",
            artifact_id="decision_record_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[winner_state.plan_ref, artifact_refs.get("freeze_record", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        return env

    def _stage_renderer(self, winner_state: CandidateState, artifact_refs: dict[str, str]) -> ArtifactEnvelope | None:
        if "freeze.renderer" not in self.stage_map:
            return None
        stage = self.stage_map["freeze.renderer"]
        final_payload: object = winner_state.payload.model_dump(by_alias=True)
        frozen_ref = artifact_refs.get("frozen_plan")
        if frozen_ref:
            try:
                final_payload = self._store().read_artifact(frozen_ref).payload
            except Exception:
                pass
        if isinstance(final_payload, dict) and "plan_package" in final_payload:
            final_payload = final_payload["plan_package"]
        prompt_vars = self._base_prompt_vars(stage.id, artifact_refs)
        prompt_vars.update(
            {
                "FINAL_PLAN_JSON": final_payload,
                "RENDER_SCHEMA_JSON": self._schema_json(stage.output_schema),
            }
        )
        output = self._call_stage_single(stage, prompt_vars, artifact_refs)
        payload = MarkdownRenderPayload.model_validate(output.model_dump(by_alias=True))
        env = ArtifactEnvelope(
            artifact_type="markdown_render",
            artifact_id="markdown_render_v1",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get("freeze_record", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage=stage.id)
        (self.run_root / "frozen_plan.md").write_text(payload.markdown, encoding="utf-8")
        return env

    def _assemble_plan_candidate(
        self,
        *,
        candidate_id: str,
        branch: BranchSpec,
        synthesizer_id: str,
        delta_payload: dict[str, object],
        version: int = 1,
        capsule: dict[str, object],
        requirements: dict[str, object],
        acceptance: dict[str, object],
        architecture: dict[str, object],
        risk_gov: dict[str, object],
        draft_refs: list[str],
        artifact_refs: dict[str, str],
    ) -> ArtifactEnvelope:
        delta = CandidateDeltaPayload.model_validate(delta_payload)
        capsule_model = ProjectCapsulePayload.model_validate(capsule)
        req_model = RequirementsCanonicalPayload.model_validate(requirements)
        acceptance_model = AcceptanceCanonicalPayload.model_validate(acceptance)
        arch_model = ArchitectureCanonicalPayload.model_validate(architecture)
        risk_model = RiskGovCanonicalPayload.model_validate(risk_gov)

        meta = PlanMeta(
            plan_id=uuid4(),
            version=f"v{version}",
            created_at=_now(),
            source_run_id=self.run_id,
            schema_version=SCHEMA_VERSION,
            candidate_id=candidate_id,
            branch_id=branch.branch_id,
            plan_status="CANDIDATE",
        )
        arch_section = ArchitectureSection(options=arch_model.options, chosen=delta.architecture_choice)
        governance = GovernanceSection(
            failure_modes=[],
            tool_policy=risk_model.tool_policy,
            hitl_policy=risk_model.hitl_policy,
        )
        appendix = None
        if capsule_model.glossary:
            appendix = PlanPackage.Appendix(glossary=capsule_model.glossary)
        plan_package = PlanPackage(
            meta=meta,
            project_capsule=capsule_model,
            executive_summary=delta.executive_summary,
            requirements=req_model.requirements,
            acceptance_tests=acceptance_model.acceptance_tests,
            architecture=arch_section,
            milestones=delta.milestones,
            risk_register=risk_model.risk_register,
            governance=governance,
            decision_log=delta.decision_log,
            supplemental_evaluations=delta.supplemental_evaluations,
            synthesis_warnings=delta.synthesis_warnings,
            appendix=appendix,
        )
        inputs = CandidateInputs(
            capsule_ref=artifact_refs.get("capsule", ""),
            draft_refs=draft_refs,
            branch_ref=branch.branch_id,
        )
        payload = PlanCandidatePayload(
            candidate_id=candidate_id,
            branch_id=branch.branch_id,
            synthesizer_id=synthesizer_id,
            inputs=inputs,
            plan_package=plan_package,
        )
        env = ArtifactEnvelope(
            artifact_type="plan_candidate",
            artifact_id=f"plan_candidate_{candidate_id}_v{version}",
            schema_version=SCHEMA_VERSION,
            created_at=_now(),
            source_run_id=self.run_id,
            parents=[artifact_refs.get(f"candidate_delta_{candidate_id}", "")],
            payload=payload.model_dump(by_alias=True),
        )
        self._write_artifact(self._store(), env, stage="assemble.plan_candidate")
        return env

    def _select_judges_for_pair(self, candidate_a: CandidateState, candidate_b: CandidateState) -> list[str]:
        pool = [str(m) for m in self._judge_policy.get("preferred_judge_pool", [])]
        rules_raw = self._judge_policy.get("rules", {})
        rules = dict(rules_raw) if isinstance(rules_raw, dict) else {}
        panel_size = int(rules.get("panel_size", self._judge_policy.get("panel_size", 3)))
        disallow_same_family = bool(self._judge_policy.get("disallow_same_family_as_candidate_author", True))
        require_non_author = bool(self._judge_policy.get("require_non_author_majority", True))
        min_outside = rules.get("min_outside_candidate_families")
        allow_same_family_if_needed = bool(rules.get("allow_same_family_if_needed", True))
        mark_same_family = bool(rules.get("mark_same_family_in_audit_log", False))

        author_families = {candidate_a.author_family, candidate_b.author_family}
        non_author_pool = [m for m in pool if self._model_family(m) not in author_families]
        author_pool = [m for m in pool if self._model_family(m) in author_families]

        if min_outside is None:
            if disallow_same_family:
                min_outside = panel_size
            elif require_non_author:
                min_outside = panel_size // 2 + 1
            else:
                min_outside = 0
        min_outside = int(min_outside)

        selected: list[str] = []
        selected.extend(non_author_pool[:min_outside])
        if len(selected) < min_outside and not allow_same_family_if_needed:
            return selected
        for model_key in non_author_pool[min_outside:]:
            if model_key in selected:
                continue
            selected.append(model_key)
            if len(selected) >= panel_size:
                break
        if len(selected) < panel_size and allow_same_family_if_needed:
            for model_key in author_pool:
                if model_key in selected:
                    continue
                selected.append(model_key)
                if len(selected) >= panel_size:
                    break
        if mark_same_family:
            same_family = [m for m in selected if self._model_family(m) in author_families]
            if same_family:
                self._event(
                    "judge.pairwise",
                    "decision",
                    payload={
                        "same_family_judges": same_family,
                        "author_families": sorted(author_families),
                    },
                )
        return selected

    def _model_family(self, model_key: str) -> str:
        model_cfg = self._models.get(model_key, {})
        if "family" in model_cfg:
            return str(model_cfg["family"])
        families = self.config.get("model_families", {})
        for family, patterns in families.items():
            if not isinstance(patterns, list):
                continue
            for pattern in patterns:
                pattern_str = str(pattern)
                if pattern_str.endswith("/*"):
                    if model_key.startswith(pattern_str[:-2]):
                        return str(family)
                elif model_key == pattern_str:
                    return str(family)
        return str(model_cfg.get("provider", ""))

    def _is_preview_model(self, model_key: str) -> bool:
        return "preview" in model_key

    def _schema_json(self, schema_path: str) -> dict[str, object]:
        schema_file = Path(schema_path)
        if not schema_file.is_absolute():
            schema_file = (self.config_path.parent / schema_path).resolve()
        return json.loads(schema_file.read_text(encoding="utf-8"))

    def _base_prompt_vars(self, stage: str, artifact_refs: dict[str, str]) -> dict[str, object]:
        return {
            "RUN_CONTEXT_JSON": {
                "run_id": str(self.run_id),
                "stage": stage,
                "timestamp": _now().isoformat(),
                "config": str(self.config.get("name", "")),
            },
            "ARTIFACT_REFS_JSON": artifact_refs,
        }

    def _prompt_input_hash(self, prompt_vars: dict[str, object]) -> str:
        def default(value: object) -> object:
            if isinstance(value, BaseModel):
                return value.model_dump(by_alias=True)
            return PromptLibrary._json_default(value)  # type: ignore[attr-defined]

        raw = json.dumps(prompt_vars, sort_keys=True, ensure_ascii=False, default=default)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _render_prompt(self, prompt_id: str, prompt_vars: dict[str, object]) -> list[dict[str, str]]:
        if prompt_id not in PROMPT_LABELS:
            raise KeyError(f"Prompt id not mapped: {prompt_id}")
        system_label, user_label = PROMPT_LABELS[prompt_id]
        system_text = self.prompt_library.render_label(system_label, **prompt_vars)
        user_text = self.prompt_library.render_label(user_label, **prompt_vars)
        messages: list[dict[str, str]] = []
        if self._guardrails.get("json", {}).get("disallow_markdown", False):
            messages.append({"role": "system", "content": "Output JSON only. Do not wrap in markdown."})
        messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _call_stage_single(
        self,
        stage: StageConfig,
        prompt_vars: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> BaseModel:
        errors: list[Exception] = []
        for call in stage.calls + stage.fallbacks:
            if not call.enabled:
                continue
            try:
                return self._call_stage_call(stage, call, prompt_vars, artifact_refs)
            except QuotaExceededError as exc:
                self._raise_quota_circuit(stage.id, artifact_refs, exc)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[-1]
        raise RuntimeError(f"No calls executed for stage {stage.id}")

    def _call_stage_multi(
        self,
        stage: StageConfig,
        prompt_vars: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> list[BaseModel]:
        outputs: list[BaseModel] = []
        errors: list[tuple[int, Exception]] = []
        call_tasks: list[tuple[int, Callable[[], object]]] = []
        for idx, call in enumerate(stage.calls):
            if not call.enabled:
                continue
            call_tasks.append(
                (
                    idx,
                    lambda call=call: self._call_stage_call(stage, call, prompt_vars, artifact_refs),
                )
            )
        preview_anchor = None
        if (
            call_tasks
            and bool(self._workflow.get("preview_models_require_anchor", False))
            and not bool(stage.selection.get("disable_preview_anchor", False))
        ):
            anchor_model = str(self._workflow.get("preview_anchor_default", "")).strip()
            if anchor_model:
                has_preview = any(
                    call.enabled and self._is_preview_model(call.model_key) for call in stage.calls
                )
                has_anchor = any(call.model_key == anchor_model for call in stage.calls)
                if has_preview and not has_anchor:
                    primary_call = next((c for c in stage.calls if c.enabled), None)
                    preview_anchor = ModelCall(
                        name="preview_anchor",
                        model_key=anchor_model,
                        profile=primary_call.profile if primary_call else None,
                        max_output_tokens=primary_call.max_output_tokens if primary_call else None,
                        overrides={},
                        enabled=True,
                    )
                    call_tasks.append(
                        (
                            len(stage.calls),
                            lambda call=preview_anchor: self._call_stage_call(
                                stage, call, prompt_vars, artifact_refs
                            ),
                        )
                    )
        results, call_errors = self._run_parallel_tasks(call_tasks)
        errors.extend(call_errors)
        for _, err in call_errors:
            if isinstance(err, QuotaExceededError):
                self._raise_quota_circuit(stage.id, artifact_refs, err)
        if results:
            outputs = [results[idx] for idx in sorted(results)]
            return outputs
        fallback_tasks: list[tuple[int, Callable[[], object]]] = []
        fallback_offset = len(stage.calls)
        for idx, call in enumerate(stage.fallbacks, start=fallback_offset):
            if not call.enabled:
                continue
            fallback_tasks.append(
                (
                    idx,
                    lambda call=call: self._call_stage_call(stage, call, prompt_vars, artifact_refs),
                )
            )
        fallback_results, fallback_errors = self._run_parallel_tasks(fallback_tasks)
        errors.extend(fallback_errors)
        for _, err in fallback_errors:
            if isinstance(err, QuotaExceededError):
                self._raise_quota_circuit(stage.id, artifact_refs, err)
        if fallback_results:
            outputs = [fallback_results[idx] for idx in sorted(fallback_results)]
            return outputs
        if errors:
            errors.sort(key=lambda item: item[0])
            raise errors[-1][1]
        raise RuntimeError(f"No outputs for stage {stage.id}")

    def _call_stage_call(
        self,
        stage: StageConfig,
        call: ModelCall,
        prompt_vars: dict[str, object],
        artifact_refs: dict[str, str],
    ) -> BaseModel:
        if stage.prompt_id is None:
            raise RuntimeError(f"Stage {stage.id} missing prompt_id")
        prompt_metadata = {
            "prompt_id": stage.prompt_id,
            "prompt_input_hash": self._prompt_input_hash(prompt_vars),
            "call_name": call.name,
        }
        messages = self._render_prompt(stage.prompt_id, prompt_vars)
        output_model = schema_model_for(stage.output_schema)
        return self._invoke_call(stage, call, messages, output_model, prompt_metadata=prompt_metadata)

    def _invoke_call(
        self,
        stage: StageConfig,
        call: ModelCall,
        messages: list[dict[str, str]],
        output_model: type[BaseModel],
        *,
        prompt_metadata: dict[str, object] | None = None,
    ) -> BaseModel:
        model_cfg = self._models.get(call.model_key)
        if model_cfg is None:
            raise KeyError(f"Unknown model key: {call.model_key}")
        provider_name = str(model_cfg.get("provider"))
        runtime = self.runtimes[provider_name]
        params = self._resolve_params(call.model_key, call.profile, call.overrides, call.max_output_tokens)
        temperature = float(params.get("temperature", 0.0) or 0.0)
        top_p = params.get("top_p")
        max_tokens = int(
            params.get("max_output_tokens", call.max_output_tokens or model_cfg.get("max_output_tokens", 2000))
        )

        model_structured = bool(model_cfg.get("structured_outputs", False))
        guard_structured = bool(
            self._guardrails.get("structured_outputs", {}).get("require_for_schema_required_stages", False)
        )
        if stage.schema_required and guard_structured and not model_structured:
            raise RuntimeError(f"Model {call.model_key} lacks structured outputs for schema stage {stage.id}")

        reasoning_supported = model_cfg.get("reasoning_effort_supported")
        reasoning_effort = params.get("reasoning_effort")
        if reasoning_supported is not None:
            if reasoning_effort is None:
                reasoning_effort = "none" if "none" in reasoning_supported else None
            if reasoning_effort is None or str(reasoning_effort) not in reasoning_supported:
                raise RuntimeError(f"Reasoning effort {reasoning_effort} not supported by {call.model_key}")
        if str(reasoning_effort) == "none":
            forbidden = self._guardrails.get("openai", {}).get("forbid_reasoning_effort_none_models", [])
            if call.model_key.split("/")[-1] in forbidden or call.model_key in forbidden:
                raise RuntimeError(f"Reasoning effort 'none' forbidden for model {call.model_key}")

        role = RoleConfig(
            role_name=f"{stage.id}:{call.name}",
            model_provider=provider_name,
            model_name=str(model_cfg.get("model_id", call.model_key)),
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_version_hash=stage.prompt_id or stage.id,
            tools_allowed=[],
            output_schema=stage.output_schema,
            top_p=float(top_p) if top_p is not None else None,
        )

        extra_params: dict[str, object] = {}
        response_format_override: dict[str, object] | None | object = _UNSET_SENTINEL
        if provider_name == "openai":
            if reasoning_effort is not None:
                extra_params["reasoning_effort"] = reasoning_effort
            if "text_verbosity" in params:
                extra_params["text_verbosity"] = params["text_verbosity"]
            if "logprobs" in params:
                extra_params["logprobs"] = params["logprobs"]
            if not model_structured:
                response_format_override = None
        elif provider_name == "anthropic":
            if not model_structured:
                response_format_override = None
        elif provider_name == "vertex_gemini":
            if "thinking_level" in params:
                extra_params["thinking_level"] = params["thinking_level"]
            schema_json = _strict_json_schema(output_model.model_json_schema())
            extra_params["response_schema"] = schema_json
            extra_params["response_mime_type"] = "application/json"
            response_format_override = None
        timeout_sec = self._llm_timeout_sec
        if timeout_sec is not None:
            extra_params["timeout_sec"] = timeout_sec

        if provider_name == "vertex_gemini" and str(model_cfg.get("model_id", "")).startswith("gemini-3"):
            if self._guardrails.get("google", {}).get("gemini3", {}).get("enforce_temperature_default"):
                role = RoleConfig(
                    role_name=role.role_name,
                    model_provider=role.model_provider,
                    model_name=role.model_name,
                    temperature=1.0,
                    max_tokens=role.max_tokens,
                    prompt_version_hash=role.prompt_version_hash,
                    tools_allowed=role.tools_allowed,
                    output_schema=role.output_schema,
                    top_p=role.top_p,
                )

        max_repair_attempts = int(self._guardrails.get("structured_outputs", {}).get("max_schema_repair_attempts", 2))

        strict_json = bool(self._guardrails.get("json", {}).get("require_valid_json", False))
        hard_fail = bool(self._guardrails.get("structured_outputs", {}).get("hard_fail_on_non_json", False))
        provider_cfg = self._providers_cfg.get(provider_name, {})
        adapter = str(provider_cfg.get("adapter", "")) if isinstance(provider_cfg, dict) else ""
        if adapter == "openrouter":
            hard_fail = False

        invoke_kwargs: dict[str, object] = {
            "stage": stage.id,
            "max_attempts": stage.max_attempts,
            "max_repair_attempts": max_repair_attempts,
            "strict_json": strict_json,
            "hard_fail_on_non_json": hard_fail,
            "extra_params": extra_params,
        }
        if response_format_override is not _UNSET_SENTINEL:
            invoke_kwargs["response_format_override"] = response_format_override

        with self._llm_semaphore:
            result = runtime.invoke_role(
                role,
                messages,
                output_model,  # type: ignore[arg-type]
                prompt_metadata=prompt_metadata,
                **invoke_kwargs,  # type: ignore[arg-type]
            )
        return result.parsed

    def _resolve_feedback_provider(
        self,
        feedback_cfg: FeedbackConfig,
        feedback_provider: HumanInputProvider | None,
    ) -> HumanInputProvider:
        if feedback_provider is not None:
            return feedback_provider
        provider_key = (feedback_cfg.provider or "auto").lower()
        if provider_key == "cli":
            return CLIProvider()
        if provider_key == "file":
            raise ValueError("feedback provider 'file' requires a response file; pass feedback_provider")
        return AutoProvider()

    def _resolve_clarification_responses(
        self,
        questions: ClarificationQuestions,
        responses: ClarificationResponses,
        feedback_gate: HumanFeedbackGate,
    ) -> ClarificationResolutions:
        response_map = {resp.question_id: resp.response for resp in responses.responses}
        resolved_items: list[dict[str, object]] = []
        for question in questions.questions:
            raw = response_map.get(question.id, "")
            if raw.strip() == "":
                resolved_value = question.default_resolution.value
                source = "default"
                feedback_gate.log_event(
                    feedback_event_log.new_event(
                        run_id=feedback_gate.run_id,
                        stage="planning",
                        event_type="HUMAN_FEEDBACK_SKIPPED",
                        gate_type="clarify_intent",
                        message=question.id,
                    )
                )
            else:
                fmt = question.answer_format
                if fmt.type == "boolean":
                    normalized = raw.strip().lower()
                    if normalized in {"true", "yes", "y", "1"}:
                        resolved_value = True
                    elif normalized in {"false", "no", "n", "0"}:
                        resolved_value = False
                    else:
                        raise ValueError(f"Invalid boolean response for {question.id}: {raw}")
                elif fmt.type == "enum":
                    accepted = [v.lower() for v in fmt.accepted]
                    if raw.strip().lower() not in accepted:
                        raise ValueError(f"Invalid enum response for {question.id}: {raw}")
                    resolved_value = raw.strip()
                else:
                    resolved_value = raw
                source = "user"
            resolved_items.append(
                {
                    "question_id": question.id,
                    "resolved_value": resolved_value,
                    "source": source,
                    "raw_response": raw,
                }
            )
        return ClarificationResolutions(schema_version="1.0", resolutions=resolved_items)

    def _feedback_invoke_json(
        self,
        messages: list[dict[str, str]],
        output_model: type[BaseModel],
        stage: str,
        prompt_metadata: dict[str, object],
    ) -> BaseModel:
        model_key = next(iter(self._models), None)
        if not model_key:
            raise RuntimeError("No models configured for feedback LLM calls")
        model_cfg = self._models[model_key]
        provider_name = str(model_cfg.get("provider"))
        runtime = self.runtimes[provider_name]
        max_tokens = int(model_cfg.get("max_output_tokens", 2000))
        role = RoleConfig(
            role_name=f"feedback:{stage}",
            model_provider=provider_name,
            model_name=str(model_cfg.get("model_id", model_key)),
            temperature=0.0,
            max_tokens=max_tokens,
            prompt_version_hash="feedback",
            tools_allowed=[],
            output_schema=output_model.__name__,
        )
        extra_params: dict[str, object] = {}
        timeout_sec = self._llm_timeout_sec
        if timeout_sec is not None:
            extra_params["timeout_sec"] = timeout_sec
        invoke_kwargs: dict[str, object] = {
            "stage": stage,
            "prompt_metadata": prompt_metadata,
            "strict_json": True,
        }
        if extra_params:
            invoke_kwargs["extra_params"] = extra_params
        result = runtime.invoke_role(role, messages, output_model, **invoke_kwargs)  # type: ignore[arg-type]
        return result.parsed

    def _resolve_params(
        self,
        model_key: str,
        profile_name: str | None,
        overrides: dict[str, object],
        max_output_tokens: int | None,
    ) -> dict[str, object]:
        model_cfg = self._models.get(model_key, {})
        provider_name = str(model_cfg.get("provider", ""))
        provider_key = self._profile_provider_key(provider_name)
        params: dict[str, object] = {}
        if profile_name and profile_name in self._profiles:
            profile = self._profiles[profile_name]
            if isinstance(profile, dict) and provider_key in profile:
                params.update(dict(profile.get(provider_key, {})))
        if overrides and provider_key in overrides:
            params.update(dict(overrides.get(provider_key, {})))
        if max_output_tokens is not None:
            params["max_output_tokens"] = max_output_tokens
        else:
            if "max_output_tokens" not in params:
                model_max = model_cfg.get("max_output_tokens")
                if model_max is not None:
                    params["max_output_tokens"] = int(model_max)
        model_max = model_cfg.get("max_output_tokens")
        if model_max is not None and "max_output_tokens" in params:
            params["max_output_tokens"] = min(int(params["max_output_tokens"]), int(model_max))
        return params

    def _profile_provider_key(self, provider_name: str) -> str:
        if provider_name == "google_vertex_ai":
            return "google"
        return provider_name


def _merge_clarifications(outputs: list[BaseModel]) -> ClarificationPlanPayload:
    if not outputs:
        return ClarificationPlanPayload(questions=[], assumptions=[], notes=[])
    questions: list[ClarificationItem] = []
    assumptions: list[Assumption] = []
    notes: list[str] = []
    seen: set[str] = set()
    for output in outputs:
        payload = ClarificationPlanPayload.model_validate(output.model_dump(by_alias=True))
        for question in payload.questions:
            key = question.text.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            questions.append(question)
        assumptions.extend(payload.assumptions)
        notes.extend(payload.notes)
    questions.sort(key=lambda q: (not q.blocking, IMPACT_ORDER.get(q.impact, 3)))
    return ClarificationPlanPayload(questions=questions, assumptions=assumptions, notes=notes)


def _merge_alignment(outputs: list[BaseModel]) -> AlignmentWarningsPayload:
    if not outputs:
        return AlignmentWarningsPayload(warnings=[])
    warnings: list[AlignmentWarning] = []
    seen: set[str] = set()
    for output in outputs:
        payload = AlignmentWarningsPayload.model_validate(output.model_dump(by_alias=True))
        for warning in payload.warnings:
            pointers = sorted({e.json_pointer for e in warning.evidence})
            key = f"{warning.summary.strip().lower()}|{','.join(pointers)}"
            if key in seen:
                continue
            seen.add(key)
            warnings.append(warning)
    return AlignmentWarningsPayload(warnings=warnings)


def _merge_trace_findings(
    outputs: list[TraceGraphFindingsPayload], *, candidate_ref: str
) -> TraceGraphFindingsPayload:
    if not outputs:
        return TraceGraphFindingsPayload(candidate_ref=candidate_ref, findings=[])
    findings: list[TraceGraphFinding] = []
    seen: set[str] = set()
    for output in outputs:
        for finding in output.findings:
            pointers = sorted({e.json_pointer for e in finding.evidence})
            key = f"{finding.summary.strip().lower()}|{','.join(pointers)}"
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)
    return TraceGraphFindingsPayload(candidate_ref=candidate_ref, findings=findings)


def _merge_branches(outputs: list[BaseModel], *, keep_count: int, branches_max: int) -> BranchSetPayload:
    branches: list[BranchSpec] = []
    for output in outputs:
        payload = BranchSetPayload.model_validate(output.model_dump(by_alias=True))
        branches.extend(payload.branches)
    if not branches:
        branches = [
            BranchSpec(
                branch_id="B1",
                selected_arch_option_id="O1",
                milestone_strategy=BranchSpec.MilestoneStrategy(style="hybrid", notes="fallback"),
                acceptance_strategy=BranchSpec.AcceptanceStrategy(notes="fallback"),
                governance_posture=BranchSpec.GovernancePosture(tooling_level="none", hitl_strictness="high"),
                risk_posture=BranchSpec.RiskPosture(notes="fallback"),
                rationale="Fallback branch",
            )
        ]
    unique: list[BranchSpec] = []
    seen: set[tuple[str, str, str, str]] = set()
    for branch in branches:
        key = _branch_key(branch)
        if key in seen:
            continue
        seen.add(key)
        unique.append(branch)

    option_counts: dict[str, int] = {}
    for branch in unique:
        option_counts[branch.selected_arch_option_id] = option_counts.get(branch.selected_arch_option_id, 0) + 1

    def score(branch: BranchSpec) -> tuple[float, int]:
        rarity = 1.0 / option_counts.get(branch.selected_arch_option_id, 1)
        length_bonus = min(len(branch.rationale or "") / 200.0, 1.0)
        hash_bonus = _hash_score(branch.branch_id) / 10_000.0
        return (rarity + length_bonus + hash_bonus, _hash_score(branch.branch_id))

    unique.sort(key=score, reverse=True)
    if branches_max:
        unique = unique[:branches_max]
    if keep_count:
        unique = unique[:keep_count]
    return BranchSetPayload(branches=unique)


def _merge_consistency(outputs: list[BaseModel], *, candidate_ref: str) -> ConsistencyFindingsPayload:
    findings: list[ConsistencyFinding] = []
    seen: set[str] = set()
    for output in outputs:
        payload = ConsistencyFindingsPayload.model_validate(output.model_dump(by_alias=True))
        for finding in payload.findings:
            key = finding.summary.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)
    return ConsistencyFindingsPayload(candidate_ref=candidate_ref, findings=findings)


def _merge_triage(outputs: list[BaseModel], *, candidate_ref: str) -> TriageFindingsPayload:
    if not outputs:
        return TriageFindingsPayload(candidate_ref=candidate_ref, critic_id="MERGED", verdict="approve", defects=[])
    defects: list[TriageDefect] = []
    verdict = "approve"
    reroute_target: str | None = None
    reroute_reason: str | None = None
    seen: set[str] = set()
    for output in outputs:
        payload = TriageFindingsPayload.model_validate(output.model_dump(by_alias=True))
        if payload.verdict == "reject":
            verdict = "reject"
        elif payload.verdict == "approve_with_fixes" and verdict != "reject":
            verdict = "approve_with_fixes"
        for defect in payload.defects:
            key = defect.summary.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            defects.append(defect)
        if reroute_target is None and payload.reroute_target:
            reroute_target = payload.reroute_target
            reroute_reason = payload.reroute_reason
    if any(d.label == "blocker" for d in defects):
        verdict = "reject"
    return TriageFindingsPayload(
        candidate_ref=candidate_ref,
        critic_id="MERGED",
        verdict=verdict,
        defects=defects,
        reroute_target=reroute_target,
        reroute_reason=reroute_reason,
    )


def _merge_failure_modes(outputs: list[BaseModel], *, candidate_ref: str) -> FailureModeFindingsPayload:
    if not outputs:
        return FailureModeFindingsPayload(
            candidate_ref=candidate_ref,
            taxonomy="none",
            critical_findings=[],
            closure_pass=True,
        )
    findings: list[FailureModeFindingItem] = []
    seen: set[str] = set()
    closure_pass = True
    taxonomy = ""
    for output in outputs:
        payload = FailureModeFindingsPayload.model_validate(output.model_dump(by_alias=True))
        taxonomy = taxonomy or payload.taxonomy
        closure_pass = closure_pass and payload.closure_pass
        for item in payload.critical_findings:
            key = item.finding.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            findings.append(item)
    return FailureModeFindingsPayload(
        candidate_ref=candidate_ref,
        taxonomy=taxonomy or "mixed",
        critical_findings=findings,
        closure_pass=closure_pass,
    )


def _apply_and_select_patch(
    *,
    candidate_ref: str,
    plan_payload: dict[str, object],
    patch_outputs: list[PatchOrReroutePayload],
    patch_refs: list[str],
    forbid_prefixes: tuple[str, ...],
    max_patch_ops: int | None = None,
    forbidden_paths: list[str] | None = None,
    forbidden_ops: list[dict[str, object]] | None = None,
    selection_criteria_order: list[str] | None = None,
    reroute_target_fallback: str | None = None,
) -> tuple[PatchApplyResultPayload, dict[str, object] | None, ValidatorReportPayload | None]:
    results: list[PatchApplyCandidateResult] = []
    selected_patch_ref: str | None = None
    selected_payload: dict[str, object] | None = None
    selected_validator: ValidatorReportPayload | None = None
    reroute_target: str | None = None
    policy_forbidden_paths = list(forbidden_paths or [])
    policy_forbidden_ops = list(forbidden_ops or [])
    criteria_order = list(
        selection_criteria_order
        or ["passes_validators", "passes_forbidden_path_checks", "min_op_count", "min_touched_paths"]
    )
    selection_candidates: list[tuple[tuple[int, ...], str, dict[str, object], ValidatorReportPayload]] = []
    policy_ok_by_ref: dict[str, bool] = {}

    for patch_output, patch_ref in zip(patch_outputs, patch_refs, strict=True):
        if patch_output.action == "reroute":
            if reroute_target is None:
                reroute_target = patch_output.reroute_target
            results.append(
                PatchApplyCandidateResult(
                    patch_ref=patch_ref,
                    applied=False,
                    validator_status="FAIL_REPAIRABLE",
                    op_count=0,
                    paths_touched=0,
                    reason="reroute",
                )
            )
            continue

        patch_ops = [op.model_dump(by_alias=True) for op in patch_output.patch]
        policy_ok = True
        if max_patch_ops is not None and len(patch_ops) > max_patch_ops:
            results.append(
                PatchApplyCandidateResult(
                    patch_ref=patch_ref,
                    applied=False,
                    validator_status="FAIL_REPAIRABLE",
                    op_count=len(patch_ops),
                    paths_touched=_paths_touched(patch_ops),
                    reason="max_patch_ops_exceeded",
                )
            )
            policy_ok = False
        if policy_ok and any(_path_forbidden(op, forbid_prefixes) for op in patch_ops):
            results.append(
                PatchApplyCandidateResult(
                    patch_ref=patch_ref,
                    applied=False,
                    validator_status="FAIL_REPAIRABLE",
                    op_count=len(patch_ops),
                    paths_touched=_paths_touched(patch_ops),
                    reason="forbidden_patch",
                )
            )
            policy_ok = False
        if policy_ok and policy_forbidden_paths:
            if any(_path_matches_any(op, policy_forbidden_paths) for op in patch_ops):
                results.append(
                    PatchApplyCandidateResult(
                        patch_ref=patch_ref,
                        applied=False,
                        validator_status="FAIL_REPAIRABLE",
                        op_count=len(patch_ops),
                        paths_touched=_paths_touched(patch_ops),
                        reason="forbidden_path_policy",
                    )
                )
                policy_ok = False
        if policy_ok and policy_forbidden_ops:
            if any(_op_forbidden(op, policy_forbidden_ops) for op in patch_ops):
                results.append(
                    PatchApplyCandidateResult(
                        patch_ref=patch_ref,
                        applied=False,
                        validator_status="FAIL_REPAIRABLE",
                        op_count=len(patch_ops),
                        paths_touched=_paths_touched(patch_ops),
                        reason="forbidden_op_policy",
                    )
                )
                policy_ok = False
        if not policy_ok:
            policy_ok_by_ref[patch_ref] = False
            continue
        try:
            patched_payload = jsonpatch.apply_patch(plan_payload, patch_ops, in_place=False)
        except Exception as exc:
            results.append(
                PatchApplyCandidateResult(
                    patch_ref=patch_ref,
                    applied=False,
                    validator_status="FAIL_REPAIRABLE",
                    op_count=len(patch_ops),
                    paths_touched=_paths_touched(patch_ops),
                    reason=str(exc),
                )
            )
            continue

        try:
            patched_candidate = PlanCandidatePayload.model_validate(patched_payload)
        except Exception as exc:
            results.append(
                PatchApplyCandidateResult(
                    patch_ref=patch_ref,
                    applied=False,
                    validator_status="FAIL_STRUCTURAL",
                    op_count=len(patch_ops),
                    paths_touched=_paths_touched(patch_ops),
                    reason=f"schema_error: {exc}",
                )
            )
            continue

        validator_report = validate_candidate(candidate_ref, patched_candidate.plan_package)
        result = PatchApplyCandidateResult(
            patch_ref=patch_ref,
            applied=True,
            validator_status=validator_report.overall_status,
            op_count=len(patch_ops),
            paths_touched=_paths_touched(patch_ops),
        )
        results.append(result)
        policy_ok_by_ref[patch_ref] = True
        if validator_report.overall_status == "PASS":
            score: list[int] = []
            for criterion in criteria_order:
                if criterion == "passes_validators":
                    score.append(0)
                elif criterion == "passes_forbidden_path_checks":
                    score.append(0 if policy_ok_by_ref.get(patch_ref, False) else 1)
                elif criterion == "min_op_count":
                    score.append(result.op_count)
                elif criterion == "min_touched_paths":
                    score.append(result.paths_touched)
            selection_candidates.append((tuple(score), patch_ref, patched_payload, validator_report))

    if selection_candidates:
        selection_candidates.sort(key=lambda item: item[0])
        _, selected_patch_ref, selected_payload, selected_validator = selection_candidates[0]

    if selected_patch_ref is None and reroute_target is None and reroute_target_fallback:
        reroute_target = reroute_target_fallback

    result_payload = PatchApplyResultPayload(
        candidate_ref=candidate_ref,
        selected_patch_ref=selected_patch_ref,
        reroute_target=reroute_target,
        applied_candidate_ref=None,
        candidates=results,
    )
    return result_payload, selected_payload, selected_validator


def _trace_sample_checks(plan_candidate_payload: dict[str, object]) -> list[dict[str, object]]:
    plan = plan_candidate_payload.get("plan_package", {}) if isinstance(plan_candidate_payload, dict) else {}
    requirements = plan.get("requirements", []) if isinstance(plan, dict) else []
    acceptance = plan.get("acceptance_tests", []) if isinstance(plan, dict) else []
    milestones = plan.get("milestones", []) if isinstance(plan, dict) else []
    checks: list[dict[str, object]] = []
    for req in requirements[:3]:
        req_id = req.get("id")
        mapped = [at.get("id") for at in acceptance if req_id in at.get("maps_to_requirements", [])]
        checks.append(
            {
                "requirement_id": req_id,
                "requirement_text": req.get("text"),
                "acceptance_tests": mapped[:3],
                "milestones": [m.get("id") for m in milestones[:2]],
            }
        )
    return checks


def _branch_key(branch: BranchSpec) -> tuple[str, str, str, str]:
    return (
        branch.selected_arch_option_id,
        branch.governance_posture.tooling_level,
        branch.governance_posture.hitl_strictness,
        (branch.risk_posture.notes or "").strip().lower(),
    )


def _hash_score(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16)


def _path_forbidden(op: dict[str, object], prefixes: tuple[str, ...]) -> bool:
    for key in ("path", "from"):
        raw = op.get(key)
        if not isinstance(raw, str):
            continue
        if any(raw.startswith(prefix) for prefix in prefixes):
            return True
    return False


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if not pattern:
        return False
    if not path.startswith("/"):
        return False
    path_parts = path.strip("/").split("/")
    pattern_parts = pattern.strip("/").split("/")
    if len(path_parts) < len(pattern_parts):
        return False
    for start in range(0, len(path_parts) - len(pattern_parts) + 1):
        matched = True
        for offset, part in enumerate(pattern_parts):
            if part == "*":
                continue
            if path_parts[start + offset] != part:
                matched = False
                break
        if matched:
            return True
    return False


def _path_matches_any(op: dict[str, object], patterns: list[str]) -> bool:
    for key in ("path", "from"):
        raw = op.get(key)
        if not isinstance(raw, str):
            continue
        for pattern in patterns:
            if _path_matches_pattern(raw, str(pattern)):
                return True
    return False


def _op_forbidden(op: dict[str, object], forbidden_ops: list[dict[str, object]]) -> bool:
    op_kind = op.get("op")
    if not isinstance(op_kind, str):
        return False
    for rule in forbidden_ops:
        if not isinstance(rule, dict):
            continue
        rule_op = str(rule.get("op", "")).strip()
        path_prefix = str(rule.get("path_prefix", "")).strip()
        if rule_op and rule_op != op_kind:
            continue
        if path_prefix and _path_matches_pattern(str(op.get("path", "")), path_prefix):
            return True
    return False


def _paths_touched(patch_ops: list[dict[str, object]]) -> int:
    touched: set[str] = set()
    for op in patch_ops:
        for key in ("path", "from"):
            raw = op.get(key)
            if not isinstance(raw, str):
                continue
            parts = raw.strip("/").split("/")
            if parts and parts[0]:
                touched.add(parts[0])
            else:
                touched.add(raw)
    return len(touched)


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _resolve_config_path(config: dict[str, object], path: str) -> object | None:
    parts = [p for p in path.split(".") if p]
    current: object = config
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _now() -> datetime:
    return datetime.now(UTC)


def _git_code_version() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _model_portfolio(config: dict[str, object]) -> dict[str, dict[str, object]]:
    models = config.get("models", {})
    portfolio: dict[str, dict[str, object]] = {}
    if isinstance(models, dict):
        for key, cfg in models.items():
            if not isinstance(cfg, dict):
                continue
            portfolio[str(key)] = {
                "provider": str(cfg.get("provider", "")),
                "model": str(cfg.get("model_id", key)),
                "temperature": 0.0,
                "max_tokens": int(cfg.get("max_output_tokens", 0) or 0),
                "prompt_version_hash": "hq_pipeline",
            }
    return portfolio

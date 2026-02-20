from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict

from council_os.agents.prompts.library import PromptLibrary
from council_os.agents.providers.base import Provider
from council_os.agents.providers.mock import MockProvider
from council_os.agents.providers.openrouter import OpenRouterProvider
from council_os.agents.roles import RoleConfig, load_role_configs
from council_os.agents.runtime import AgentRuntime
from council_os.agents.schemas import (
    SCHEMA_VERSION,
    AcceptanceDraftPayload,
    AcceptanceTest,
    ArchitectureChosen,
    ArchitectureOption,
    ArchitectureOptionsPayload,
    ArchitectureSection,
    ArtifactEnvelope,
    Assumption,
    BranchBundlePayload,
    Component,
    DecisionLogItem,
    EvidencePointer,
    FailureModeClosure,
    FailureModeFindingsPayload,
    FailureModeItem,
    GovernanceDraftPayload,
    GovernanceSection,
    HitlPolicy,
    Interface,
    JudgePairwisePayload,
    Milestone,
    MilestoneDeliverable,
    Mitigation,
    OpenQuestion,
    PlanCandidatePayload,
    PlanMeta,
    PlanPackage,
    ProjectCapsulePayload,
    QaStrategyPayload,
    Requirement,
    RequirementsDraftPayload,
    RiskItem,
    RunMetricsPayload,
    ToolPolicy,
    ToolPolicyStage,
    Tradeoffs,
    TriageFindingsPayload,
)
from council_os.artifacts.store import ArtifactExistsError, ArtifactStore
from council_os.audit.event_log import EventType, StageName, append_event, new_event, read_events, read_last_event
from council_os.audit.manifest import ManifestInput, create_manifest
from council_os.audit.tracing import configure_tracing, trace_span
from council_os.orchestrator.checkpoints import (
    Checkpoint,
    CheckpointState,
    fork_from_checkpoint,
    latest_checkpoint,
    list_checkpoints,
    load_checkpoint,
    write_checkpoint,
)
from council_os.orchestrator.hq_pipeline import HQPipeline
from council_os.orchestrator.locks import run_lock, update_run_lock
from council_os.orchestrator.policies import (
    HitlPolicyConfig,
    ToolPolicyConfig,
    enforce_tool_policy,
    hitl_should_interrupt,
    parse_hitl_policy,
    parse_tool_policy,
    tool_call_requires_approval,
    tools_enabled,
)
from council_os.orchestrator.selection import deterministic_seed, run_tournament
from council_os.orchestrator.stages import (
    acceptance_draft,
    architecture_options,
    branch_bundle,
    freeze_record,
    governance_draft,
    plan_candidate,
    plan_frozen,
    project_capsule,
    qa_strategy,
    requirements_draft,
)
from council_os.orchestrator.feedback.config import FeedbackConfig
from council_os.orchestrator.feedback.gates import HumanFeedbackGate, NeedsUserInput
from council_os.orchestrator.feedback.human_input import AutoProvider, CLIProvider, FileProvider, HumanInputProvider
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
from council_os.orchestrator.handoff.finalize import build_config_snapshot, finalize_and_write_handoff
from council_os.orchestrator.tools import (
    EchoTool,
    IdempotencyLedger,
    LedgerEntry,
    SideEffectCounterTool,
    ToolRegistry,
    compute_idempotency_key,
    normalize_args,
)
from council_os.orchestrator.triage import apply_repairs, critic_findings, merge_findings, triage_repair_loop
from council_os.render import render_plan_markdown
from council_os.validation.failure_modes import analyze_failure_modes, close_open_findings
from council_os.validation.validators import validate_candidate

STAGES = [
    "intake",
    "clarify",
    "draft_pods",
    "branch",
    "synthesize",
    "validate",
    "triage",
    "failure_mode",
    "judge",
    "freeze",
]


def _is_hq_config(config: object) -> bool:
    if not isinstance(config, dict):
        return False
    return bool(config.get("stages") and config.get("models") and config.get("providers"))


@dataclass(frozen=True)
class RunResult:
    run_id: UUID
    run_root: Path
    frozen_artifact_id: str


@dataclass
class CandidateState:
    artifact_id: str
    payload: PlanCandidatePayload
    validator_artifact_id: str = ""
    validator_status: str = "FAIL_STRUCTURAL"
    triage_artifact_id: str = ""
    triage_blocked: bool = True
    failure_mode_artifact_id: str = ""
    failure_mode_pass: bool = False


@dataclass(frozen=True)
class DraftBundle:
    requirements: RequirementsDraftPayload
    acceptance: AcceptanceDraftPayload
    architecture: ArchitectureOptionsPayload
    governance: GovernanceDraftPayload


class RuntimeAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    message_count: int
    seed: int
    tools_allowed: list[str]


TModel = TypeVar("TModel", bound=BaseModel)


class Engine:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._tool_registry = ToolRegistry()
        self._tool_registry.register(EchoTool())
        self._tool_registry.register(SideEffectCounterTool())
        self._tool_policy = ToolPolicyConfig(stage_policies=[])
        self._tools_active = False
        self._hitl_policy = HitlPolicyConfig(when_to_interrupt=[], approval_roles=[])
        self._prompts = PromptLibrary()
        self._max_parallel_llm_calls = 1
        self._llm_semaphore: threading.BoundedSemaphore | None = None
        self._run_lock_path: Path | None = None
        self._llm_timeout_sec: float | None = None

    def _git_code_version(self) -> str:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
            dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
            return f"{commit}:{'dirty' if dirty else 'clean'}"
        except Exception:
            return "unknown:dirty"

    def _event(
        self,
        run_root: Path,
        run_id: UUID,
        stage: StageName,
        event_type: EventType,
        refs: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
        actor: dict[str, str] | None = None,
    ) -> str:
        ev = new_event(
            run_id=run_id,
            stage=stage,
            actor=actor or {"kind": "orchestrator", "role": "engine"},
            event_type=event_type,
            refs=refs,
            payload=payload,
        )
        append_event(run_root, ev)
        return str(ev.event_id)

    def _load_interrupt_payload(self, run_root: Path) -> dict[str, object]:
        interrupt_file = run_root / "interrupt_response.json"
        if not interrupt_file.exists():
            return {}
        try:
            return json.loads(interrupt_file.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def _load_tool_calls(self, run_root: Path) -> list[dict[str, Any]]:
        tool_call_file = run_root / "tool_calls.json"
        if not tool_call_file.exists():
            return []
        try:
            raw = json.loads(tool_call_file.read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        if isinstance(raw, dict):
            calls = raw.get("calls", [])
        elif isinstance(raw, list):
            calls = raw
        else:
            return []
        return [call for call in calls if isinstance(call, dict)]

    def _execute_stage_tool_calls(
        self,
        run_id: UUID,
        run_root: Path,
        ledger: IdempotencyLedger,
        stage: str,
    ) -> None:
        calls = self._load_tool_calls(run_root)
        if not calls:
            return
        for call in calls:
            if str(call.get("stage", "")).strip() != stage:
                continue
            tool_name = str(call.get("tool_name") or call.get("tool") or "").strip()
            if not tool_name:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="error",
                    payload={"phase": "tool_call", "error": "Missing tool_name", "call": call},
                )
                continue
            args_raw = call.get("args", {})
            args = dict(args_raw) if isinstance(args_raw, dict) else {}
            approved = bool(call.get("approved", False))
            try:
                self._execute_tool(
                    run_id,
                    run_root,
                    ledger,
                    stage,
                    tool_name=tool_name,
                    args=args,
                    approved=approved,
                )
            except Exception as exc:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="error",
                    payload={"phase": "tool_call", "tool_name": tool_name, "error": str(exc)},
                )

    def _enforce_portfolio_diversity(self, config: dict[str, Any]) -> None:
        if config.get("portfolio_diversity", True) is False:
            return
        roles = config.get("roles", {})
        if not isinstance(roles, dict):
            raise ValueError("roles config must be a mapping")

        def provider_family(role: dict[str, object]) -> str:
            provider = str(role.get("model_provider", ""))
            model = str(role.get("model_name", ""))
            if provider == "openrouter":
                if "/" in model:
                    return model.split("/", 1)[0]
                return "openrouter"
            return provider

        synth_roles = {k: v for k, v in roles.items() if k.startswith("synthesizer_") and isinstance(v, dict)}
        judge_roles = {k: v for k, v in roles.items() if k.startswith("judge_") and isinstance(v, dict)}

        if len(synth_roles) < 2:
            raise ValueError("Portfolio diversity requires at least two synthesizer roles")
        if not judge_roles:
            raise ValueError("Portfolio diversity requires at least one judge role")

        synth_families = {(provider_family(v), str(v.get("model_name", ""))) for v in synth_roles.values()}
        synth_providers = {provider_family(v) for v in synth_roles.values()}
        if len(synth_providers) < 2:
            raise ValueError("Synthesizer A/B must use different providers")
        if len(synth_families) < 2:
            raise ValueError("Synthesizer A/B must use different provider/model families")

        judge_families = {(provider_family(v), str(v.get("model_name", ""))) for v in judge_roles.values()}
        judge_providers = {provider_family(v) for v in judge_roles.values()}
        if judge_providers.issubset(synth_providers):
            raise ValueError("At least one judge provider/family must differ from synthesizers")
        if any(judge in synth_families for judge in judge_families):
            raise ValueError("No judge may use the same exact provider/model as any synthesizer")

    def _schema_version_from_config(self, config: dict[str, Any]) -> str:
        configured = str(config.get("schemas_version", "")).strip()
        if not configured:
            raise ValueError("schemas_version must be configured")
        if configured != SCHEMA_VERSION:
            raise ValueError(
                f"schemas_version mismatch: config={configured}, runtime={SCHEMA_VERSION}. "
                "Update config or runtime schema version."
            )
        return configured

    def _write_execution_plan(
        self, run_root: Path, config: dict[str, Any], role_configs: dict[str, RoleConfig]
    ) -> None:
        roles: dict[str, dict[str, object]] = {}
        for name, role in role_configs.items():
            roles[name] = {
                "provider": role.model_provider,
                "model": role.model_name,
                "temperature": role.temperature,
                "max_tokens": role.max_tokens,
                "prompt_version_hash": role.prompt_version_hash,
                "tools_allowed": list(role.tools_allowed),
                "output_schema": role.output_schema,
            }
        plan = {
            "run_id": str(run_root.name),
            "created_at": datetime.now(UTC).isoformat(),
            "run_config_version": str(config.get("version") or config.get("stage_machine_version") or ""),
            "schemas_version": str(config.get("schemas_version", "")),
            "stage_machine_version": str(config.get("stage_machine_version", "")),
            "selection_protocol_version": str(config.get("selection_protocol_version", "")),
            "validator_suite_version": str(config.get("validator_suite_version", "")),
            "branching_policy": dict(config.get("branching_policy", {})),
            "ensemble_policy": dict(config.get("ensemble_policy", {})),
            "iteration_caps": dict(config.get("iteration_caps", {})),
            "stages": list(STAGES),
            "roles": roles,
            "prompt_pack_hash": self._prompts.pack_hash(),
        }
        (run_root / "execution_plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _verify_checkpoint_artifacts(
        self,
        store: ArtifactStore,
        artifact_refs: dict[str, str],
        schema_version: str,
    ) -> None:
        missing: list[str] = []
        mismatched: list[str] = []
        for key, artifact_id in artifact_refs.items():
            try:
                env = store.read_artifact(artifact_id)
            except Exception:
                missing.append(f"{key}={artifact_id}")
                continue
            if env.schema_version != schema_version:
                mismatched.append(f"{key}={artifact_id} (schema={env.schema_version}, expected={schema_version})")
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

    def _manifest_model_portfolio(self, role_configs: dict[str, RoleConfig]) -> dict[str, dict[str, object]]:
        portfolio: dict[str, dict[str, object]] = {}
        for role_name in sorted(role_configs.keys()):
            role = role_configs[role_name]
            portfolio[role_name] = {
                "provider": role.model_provider,
                "model": role.model_name,
                "temperature": role.temperature,
                "max_tokens": role.max_tokens,
                "prompt_version_hash": role.prompt_version_hash,
            }
        return portfolio

    def _resolve_max_parallel_llm_calls(self, config: dict[str, object]) -> int:
        runtime_cfg = config.get("runtime", {})
        if isinstance(runtime_cfg, dict):
            orchestrator_cfg = runtime_cfg.get("orchestrator", {})
        else:
            orchestrator_cfg = {}
        if isinstance(orchestrator_cfg, dict):
            concurrency_cfg = orchestrator_cfg.get("concurrency", {})
        else:
            concurrency_cfg = {}
        if isinstance(concurrency_cfg, dict):
            value = concurrency_cfg.get("max_parallel_llm_calls")
            if isinstance(value, int) and value > 0:
                return value
        return max(1, min(8, (os.cpu_count() or 4)))

    def _resolve_llm_timeout_sec(self, config: dict[str, object]) -> float | None:
        runtime_cfg = config.get("runtime", {})
        if isinstance(runtime_cfg, dict):
            orchestrator_cfg = runtime_cfg.get("orchestrator", {})
        else:
            orchestrator_cfg = {}
        if isinstance(orchestrator_cfg, dict):
            raw = orchestrator_cfg.get("llm_timeout_sec")
        else:
            raw = None
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    def _invoke_role(
        self,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        role_name: str,
        prompt: str,
        stage: str,
        prompt_metadata: dict[str, object] | None = None,
    ) -> None:
        if not role_configs:
            raise ValueError("No role configs available for runtime invocation")
        selected = role_configs.get(role_name)
        if selected is None:
            selected = next(iter(role_configs.values()))
        runtime = runtimes.get(selected.model_provider)
        if runtime is None:
            runtime = next(iter(runtimes.values()))
        if prompt_metadata is None:
            prompt_metadata = {"prompt": prompt}
        invoke_kwargs: dict[str, object] = {"stage": stage, "prompt_metadata": prompt_metadata}
        if self._llm_timeout_sec is not None:
            invoke_kwargs["extra_params"] = {"timeout_sec": self._llm_timeout_sec}
        if self._llm_semaphore is None:
            runtime.invoke_role(
                selected,
                [{"role": "user", "content": prompt}],
                RuntimeAck,
                **invoke_kwargs,  # type: ignore[arg-type]
            )
            return
        with self._llm_semaphore:
            runtime.invoke_role(
                selected,
                [{"role": "user", "content": prompt}],
                RuntimeAck,
                **invoke_kwargs,  # type: ignore[arg-type]
            )

    def _invoke_role_json(
        self,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        role_name: str,
        messages: list[dict[str, str]],
        output_model: type[TModel],
        stage: str,
        prompt_metadata: dict[str, object] | None = None,
    ) -> TModel:
        if not role_configs:
            raise ValueError("No role configs available for runtime invocation")
        selected = role_configs.get(role_name)
        if selected is None:
            selected = next(iter(role_configs.values()))
        if selected.output_schema and selected.output_schema not in {"dynamic", output_model.__name__}:
            raise ValueError(
                f"Role {selected.role_name} expects output_schema={selected.output_schema}, "
                f"but invocation requested {output_model.__name__}"
            )
        runtime = runtimes.get(selected.model_provider)
        if runtime is None:
            runtime = next(iter(runtimes.values()))
        invoke_kwargs: dict[str, object] = {"stage": stage, "prompt_metadata": prompt_metadata}
        if self._llm_timeout_sec is not None:
            invoke_kwargs["extra_params"] = {"timeout_sec": self._llm_timeout_sec}
        if self._llm_semaphore is None:
            result = runtime.invoke_role(
                selected,
                messages,
                output_model,
                **invoke_kwargs,  # type: ignore[arg-type]
            )
        else:
            with self._llm_semaphore:
                result = runtime.invoke_role(
                    selected,
                    messages,
                    output_model,
                    **invoke_kwargs,  # type: ignore[arg-type]
                )
        if isinstance(result.parsed, output_model):
            return result.parsed
        return output_model.model_validate(result.parsed)

    def _llm_generation_enabled(self, role_configs: dict[str, RoleConfig]) -> bool:
        pod = role_configs.get("requirements_pod")
        synth_a = role_configs.get("synthesizer_a")
        synth_b = role_configs.get("synthesizer_b")
        required = [pod, synth_a, synth_b]
        return all(
            r is not None
            and not r.model_provider.lower().startswith("mock")
            and "mock" not in r.model_name.lower()
            for r in required
        )

    def _prompt_task(self, name: str, fallback: str) -> str:
        try:
            return self._prompts.task(name)
        except Exception:
            return fallback

    def _resolve_clarification_responses(
        self,
        questions: ClarificationQuestions,
        responses: ClarificationResponses,
        feedback_gate: HumanFeedbackGate,
    ) -> ClarificationResolutions:
        response_map = {resp.question_id: resp.response for resp in responses.responses}
        resolved_items = []
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

    def _prompt_input_hash(self, payload: dict[str, object]) -> str:
        def default(value: object) -> object:
            if isinstance(value, BaseModel):
                return value.model_dump(by_alias=True)
            return PromptLibrary._json_default(value)  # type: ignore[attr-defined]

        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=default)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _json_schema_prompt(
        self,
        output_model: type[BaseModel],
        task: str,
        context: str,
        task_name: str | None = None,
    ) -> tuple[list[dict[str, str]], dict[str, object]]:
        schema = json.dumps(output_model.model_json_schema(), indent=2)
        prompt_meta: dict[str, object] = {
            "task": task,
            "context": context,
            "schema_json": schema,
        }
        if task_name:
            prompt_meta["task_name"] = task_name
        prompt_meta["prompt_input_hash"] = self._prompt_input_hash(prompt_meta)
        try:
            system_text = self._prompts.render("json_system_v1.txt")
            user_text = self._prompts.render("json_user_v1.txt", task=task, context=context, schema_json=schema)
            prompt_meta["template"] = {"system": "json_system_v1.txt", "user": "json_user_v1.txt"}
        except Exception:
            system_text = (
                "You produce strict JSON that must validate against the provided JSON Schema. "
                "No markdown, no explanations."
            )
            user_text = (
                f"Task:\n{task}\n\n"
                f"Context:\n{context}\n\n"
                f"Output JSON Schema:\n{schema}\n\n"
                "Return only one JSON object that satisfies this schema."
            )
            prompt_meta["template"] = {"system": "inline", "user": "inline"}
        return (
            [
            {
                "role": "system",
                "content": system_text,
            },
            {
                "role": "user",
                "content": user_text,
            },
            ],
            prompt_meta,
        )

    def _build_provider(
        self,
        provider_name: str,
        mock_cassette_path: Path | None = None,
        force_mock: bool = False,
    ) -> Provider:
        key = provider_name.lower()
        if force_mock or key.startswith("mock"):
            digest = hashlib.sha256(provider_name.encode()).hexdigest()
            seed = int(digest, 16) % 10_000
            return MockProvider(seed=seed, cassette_path=mock_cassette_path)
        return OpenRouterProvider()

    def _write_artifact(self, store: ArtifactStore, run_root: Path, envelope: ArtifactEnvelope, stage: str) -> None:
        skipped = False
        try:
            store.write_artifact(envelope)
        except ArtifactExistsError:
            existing = store.read_artifact(envelope.artifact_id)
            if existing.artifact_type != envelope.artifact_type:
                raise
            skipped = True
        self._event(
            run_root,
            envelope.source_run_id,
            stage=stage,
            event_type="artifact_write",
            refs={"artifact_id": envelope.artifact_id},
            payload={"artifact_type": envelope.artifact_type, "skipped_existing": skipped},
        )

    def _execute_tool(
        self,
        run_id: UUID,
        run_root: Path,
        ledger: IdempotencyLedger,
        stage: str,
        tool_name: str,
        args: dict[str, Any],
        approved: bool,
    ) -> dict[str, Any]:
        approval_required = tool_call_requires_approval(self._tool_policy, stage, args)
        self._event(
            run_root,
            run_id,
            stage=stage,
            event_type="decision",
            payload={
                "decision": "tool_approval",
                "tool_name": tool_name,
                "approval_required": approval_required,
                "approved": approved,
            },
        )
        if approval_required and not approved and hitl_should_interrupt(self._hitl_policy, "tool_approval_required"):
            self._event(
                run_root,
                run_id,
                stage=stage,
                event_type="decision",
                payload={
                    "interrupt_required": True,
                    "reason": "tool_approval_required",
                    "tool_name": tool_name,
                },
            )
            raise RuntimeError("HITL interrupt required: tool approval")
        enforce_tool_policy(self._tool_policy, stage, tool_name, args, approved=approved)

        normalized_args = normalize_args(args)
        target_resource = str(args.get("target_resource", ""))
        key = compute_idempotency_key(run_id, stage, tool_name, normalized_args, target_resource)
        prior = ledger.get(key)
        if prior is not None:
            self._event(
                run_root,
                run_id,
                stage=stage,
                event_type="tool_result",
                payload={"tool_name": tool_name, "skipped": True, "idempotency_key": key},
                actor={"kind": "tool", "role": tool_name},
            )
            return prior.result

        self._event(
            run_root,
            run_id,
            stage=stage,
            event_type="tool_call",
            payload={"tool_name": tool_name, "args": args, "approved": approved},
            actor={"kind": "tool", "role": tool_name},
        )
        result = self._tool_registry.execute(tool_name, args)
        if result.side_effect:
            ledger.put(
                LedgerEntry(
                    idempotency_key=key,
                    stage=stage,
                    tool_name=tool_name,
                    normalized_args=normalized_args,
                    result=result.output,
                )
            )
        self._event(
            run_root,
            run_id,
            stage=stage,
            event_type="tool_result",
            payload={"tool_name": tool_name, "skipped": False, "result": result.output, "idempotency_key": key},
            actor={"kind": "tool", "role": tool_name},
        )
        return result.output

    @staticmethod
    def _next_numeric_id(prefix: str, ids: list[str]) -> int:
        numbers: list[int] = []
        for value in ids:
            if value.startswith(prefix) and value[len(prefix) :].isdigit():
                numbers.append(int(value[len(prefix) :]))
        return (max(numbers) if numbers else 0) + 1

    def _safe_branch_payloads(
        self,
        store: ArtifactStore,
        branches: list[tuple[str, str]],
        run_root: Path,
        run_id: UUID,
        stage: str,
    ) -> dict[str, BranchBundlePayload]:
        payloads: dict[str, BranchBundlePayload] = {}
        for branch_id, branch_ref in branches:
            if not branch_ref:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="decision",
                    payload={"decision": "branch_payload_missing", "branch_id": branch_id},
                )
                continue
            try:
                art = store.read_artifact(branch_ref)
                payloads[branch_id] = BranchBundlePayload.model_validate(art.payload)
            except Exception as exc:
                self._event(
                    run_root,
                    run_id,
                    stage=stage,
                    event_type="error",
                    refs={"artifact_id": branch_ref, "candidate_id": branch_id},
                    payload={"phase": "branch_payload", "error": str(exc)},
                )
        return payloads

    def _build_llm_candidate(
        self,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        run_id: UUID,
        run_root: Path,
        branch_ref: str,
        branch_id: str,
        synth_id: str,
        shared_context: str,
        draft_refs: list[str],
        capsule_payload: ProjectCapsulePayload,
        draft_bundle: DraftBundle,
        branch_payload: BranchBundlePayload | None,
        candidate_suffix: str = "",
    ) -> ArtifactEnvelope:
        candidate_id = f"{branch_id}-{synth_id}{candidate_suffix}"
        synth_role = "synthesizer_a" if synth_id == "SA" else "synthesizer_b"
        task_text = self._prompt_task(
            "plan_candidate",
            "Build a complete plan package for this branch and candidate. "
            "Use stable IDs (R#, AT#, O#, C#, IF#, M#, DL#, K#, DEC#, FM#) and ensure MUST requirements "
            "have acceptance coverage.",
        )
        messages, prompt_meta = self._json_schema_prompt(
            PlanPackage,
            task=task_text,
            context=f"Candidate ID: {candidate_id}\nBranch ID: {branch_id}\n\n{shared_context}",
            task_name="plan_candidate",
        )
        try:
            plan = self._invoke_role_json(
                runtimes,
                role_configs,
                synth_role,
                messages,
                PlanPackage,
                stage="synthesize",
                prompt_metadata=prompt_meta,
            )
        except Exception as exc:
            plan = self._assemble_plan_from_drafts(
                run_id=run_id,
                candidate_id=candidate_id,
                branch_id=branch_id,
                branch_ref=branch_ref,
                capsule_payload=capsule_payload,
                draft_bundle=draft_bundle,
                branch_payload=branch_payload,
            )
            self._event(
                run_root,
                run_id,
                stage="synthesize",
                event_type="decision",
                refs={"candidate_id": candidate_id},
                payload={"decision": "fallback_plan_assembly", "reason": str(exc)},
            )
        plan.meta.plan_id = uuid4()
        plan.meta.version = "v1"
        created_at = datetime.now(UTC)
        plan.meta.created_at = created_at
        plan.meta.source_run_id = run_id
        plan.meta.schema_version = SCHEMA_VERSION
        plan.meta.candidate_id = candidate_id
        plan.meta.branch_id = branch_id
        plan.meta.plan_status = "CANDIDATE"
        plan.project_capsule = capsule_payload

        payload = PlanCandidatePayload(
            candidate_id=candidate_id,
            branch_id=branch_id,
            synthesizer_id=synth_id,
            inputs={"capsule_ref": "capsule_v1", "draft_refs": draft_refs, "branch_ref": branch_ref},
            plan_package=plan,
        )
        return ArtifactEnvelope(
            artifact_type="plan_candidate",
            artifact_id=f"candidate_{candidate_id}_v1",
            schema_version=SCHEMA_VERSION,
            created_at=created_at,
            source_run_id=run_id,
            parents=[branch_ref],
            payload=payload.model_dump(by_alias=True),
        )

    def _assemble_plan_from_drafts(
        self,
        run_id: UUID,
        candidate_id: str,
        branch_id: str,
        branch_ref: str,
        capsule_payload: ProjectCapsulePayload,
        draft_bundle: DraftBundle,
        branch_payload: BranchBundlePayload | None,
    ) -> PlanPackage:
        capsule = capsule_payload.model_copy(deep=True)
        if capsule.assumptions:
            normalized_assumptions: list[Assumption] = []
            next_assumption = 1
            for assumption in capsule.assumptions:
                normalized_assumptions.append(
                    Assumption(
                        id=f"A{next_assumption}",
                        text=assumption.text,
                        impact=assumption.impact,
                        confidence=assumption.confidence,
                        needs_confirmation=assumption.needs_confirmation,
                    )
                )
                next_assumption += 1
            capsule.assumptions = normalized_assumptions
        if capsule.open_questions:
            normalized_questions: list[OpenQuestion] = []
            next_question = 1
            for question in capsule.open_questions:
                normalized_questions.append(
                    OpenQuestion(
                        id=f"Q{next_question}",
                        text=question.text,
                        impact=question.impact,
                        blocking=question.blocking,
                    )
                )
                next_question += 1
            capsule.open_questions = normalized_questions

        requirements_payload = draft_bundle.requirements.model_copy(deep=True)
        acceptance_payload = draft_bundle.acceptance.model_copy(deep=True)
        architecture_payload = draft_bundle.architecture.model_copy(deep=True)
        governance_payload = draft_bundle.governance.model_copy(deep=True)

        requirements_raw = list(requirements_payload.requirements)
        if not requirements_raw:
            requirements = [
                Requirement(
                    id="R1",
                    priority="MUST",
                    text="Deliver a repeatable planning workflow for the project variant.",
                    rationale="Core objective for the pilot.",
                ),
                Requirement(
                    id="R2",
                    priority="SHOULD",
                    text="Produce measurable success metrics for the workflow.",
                    rationale="Needed to validate effectiveness.",
                ),
            ]
            req_map = {req.id: req.id for req in requirements}
        else:
            requirements = []
            req_map: dict[str, str] = {}
            next_req = 1
            for req in requirements_raw:
                new_id = f"R{next_req}"
                req_map[req.id] = new_id
                requirements.append(
                    Requirement(
                        id=new_id,
                        priority=req.priority,
                        text=req.text,
                        rationale=req.rationale,
                    )
                )
                next_req += 1

        acceptance_tests_raw = list(acceptance_payload.acceptance_tests)
        acceptance_tests: list[AcceptanceTest] = []
        next_at = 1
        for test in acceptance_tests_raw:
            mapped = [req_map.get(rid, "") for rid in test.maps_to_requirements]
            mapped = [rid for rid in mapped if rid]
            if not mapped and requirements:
                mapped = [requirements[0].id]
            if test.type in {"unit", "integration", "system", "human_eval", "metric"}:
                test_type = test.type
            else:
                test_type = "system"
            acceptance_tests.append(
                AcceptanceTest(
                    id=f"AT{next_at}",
                    maps_to_requirements=mapped,
                    type=test_type,
                    procedure=test.procedure,
                    pass_criteria=test.pass_criteria,
                )
            )
            next_at += 1

        must_req_ids = [req.id for req in requirements if req.priority == "MUST"]
        mapped = {rid for at in acceptance_tests for rid in at.maps_to_requirements}
        for rid in must_req_ids:
            if rid not in mapped:
                acceptance_tests.append(
                    AcceptanceTest(
                        id=f"AT{next_at}",
                        maps_to_requirements=[rid],
                        type="system",
                        procedure=f"Validate requirement {rid} using the workflow artifacts.",
                        pass_criteria=f"Requirement {rid} is satisfied with evidence.",
                    )
                )
                next_at += 1

        risk_register_raw = list(governance_payload.risk_register)
        risk_register: list[RiskItem] = []
        risk_map: dict[str, str] = {}
        next_risk = 1
        if not risk_register_raw:
            risk_register.append(
                RiskItem(
                    id="K1",
                    severity="high",
                    description="Workflow adoption may lag without clear ownership.",
                    mitigation=Mitigation(
                        text="Assign owner and run pilot training.",
                        owner="planning-lead",
                        status="planned",
                    ),
                    acceptance=None,
                )
            )
            risk_map["K1"] = "K1"
            next_risk = 2
        else:
            for risk in risk_register_raw:
                new_id = f"K{next_risk}"
                risk_map[risk.id] = new_id
                risk_register.append(
                    RiskItem(
                        id=new_id,
                        severity=risk.severity,
                        description=risk.description,
                        mitigation=risk.mitigation,
                        acceptance=risk.acceptance,
                    )
                )
                next_risk += 1

        options_raw = list(architecture_payload.options)
        options: list[ArchitectureOption] = []
        option_id_map: dict[str, str] = {}
        next_option = 1
        component_counter = 1
        interface_counter = 1
        if not options_raw:
            options_raw = [
                ArchitectureOption(
                    id="O1",
                    summary="Workflow-driven planning with lightweight integrations",
                    components=[
                        Component(
                            id="C1",
                            name="Workflow Orchestrator",
                            responsibilities=["Define planning steps", "Coordinate reviews"],
                        ),
                        Component(
                            id="C2",
                            name="Tracking System",
                            responsibilities=["Track milestones", "Store metrics"],
                        ),
                    ],
                    interfaces=[
                        Interface(
                            id="IF1",
                            **{"from": "C1"},
                            to="C2",
                            contract="Workflow outputs captured for tracking",
                        )
                    ],
                    tradeoffs=Tradeoffs(
                        pros=["Low overhead", "Aligns with existing tools"],
                        cons=["Limited automation until later phases"],
                    ),
                    risks=["K1"],
                )
            ]
        for option in options_raw:
            new_option_id = f"O{next_option}"
            option_id_map[option.id] = new_option_id
            next_option += 1

            comp_map: dict[str, str] = {}
            new_components: list[Component] = []
            for comp in option.components:
                new_id = f"C{component_counter}"
                component_counter += 1
                comp_map[comp.id] = new_id
                new_components.append(
                    Component(id=new_id, name=comp.name, responsibilities=comp.responsibilities)
                )
            if not new_components:
                new_id = f"C{component_counter}"
                component_counter += 1
                new_components.append(
                    Component(id=new_id, name="Primary Component", responsibilities=["Core responsibilities"])
                )

            new_interfaces: list[Interface] = []
            for iface in option.interfaces:
                from_id = comp_map.get(iface.from_component)
                to_id = comp_map.get(iface.to)
                if not from_id:
                    from_id = new_components[0].id
                if not to_id:
                    to_id = new_components[-1].id
                new_interfaces.append(
                    Interface(
                        id=f"IF{interface_counter}",
                        **{"from": from_id},
                        to=to_id,
                        contract=iface.contract,
                    )
                )
                interface_counter += 1

            normalized_risks: list[str] = []
            for risk_id in option.risks:
                if risk_id in risk_map:
                    normalized_risks.append(risk_map[risk_id])
                else:
                    new_risk_id = f"K{next_risk}"
                    next_risk += 1
                    risk_map[risk_id] = new_risk_id
                    risk_register.append(
                        RiskItem(
                            id=new_risk_id,
                            severity="medium",
                            description=f"Placeholder risk referenced by {new_option_id}.",
                            mitigation=Mitigation(
                                text="Define mitigation plan for this risk.",
                                owner="planning-lead",
                                status="planned",
                            ),
                            acceptance=None,
                        )
                    )
                    normalized_risks.append(new_risk_id)

            options.append(
                ArchitectureOption(
                    id=new_option_id,
                    summary=option.summary,
                    components=new_components,
                    interfaces=new_interfaces,
                    tradeoffs=option.tradeoffs,
                    risks=normalized_risks,
                )
            )

        for risk in risk_register:
            if risk.severity in {"high", "critical"}:
                has_mitigation = (
                    risk.mitigation is not None
                    and bool(risk.mitigation.text.strip())
                    and bool(risk.mitigation.owner.strip())
                    and risk.mitigation.status in {"planned", "in_progress", "done"}
                )
                has_acceptance = (
                    risk.acceptance is not None
                    and bool(risk.acceptance.rationale.strip())
                    and bool(risk.acceptance.signoff.strip())
                )
                if not (has_mitigation or has_acceptance):
                    risk.mitigation = Mitigation(
                        text="Define and track mitigation for this risk.",
                        owner="planning-lead",
                        status="planned",
                    )

        tool_policy = governance_payload.tool_policy
        if not tool_policy.stage_policies:
            tool_policy = ToolPolicy(
                stage_policies=[
                    ToolPolicyStage(
                        stage="PLANNING",
                        allowlisted_tools=[],
                        restrictions=[],
                        hitl_triggers=["network_access", "write_outside_workspace", "shell_exec", "secrets_access"],
                    )
                ]
            )
        hitl_policy = governance_payload.hitl_policy
        if not hitl_policy.when_to_interrupt or not hitl_policy.approval_roles:
            hitl_policy = HitlPolicy(
                when_to_interrupt=["high_severity_decision", "tool_approval_required"],
                approval_roles=["user", "human_reviewer"],
            )

        option_ids = [opt.id for opt in options]
        selected_option_id = option_ids[0]
        if branch_payload:
            mapped_option = option_id_map.get(branch_payload.selected_arch_option)
            if mapped_option in option_ids:
                selected_option_id = mapped_option

        decision_id = f"DEC{self._next_numeric_id('DEC', [])}"
        architecture_chosen = ArchitectureChosen(
            option_id=selected_option_id,
            rationale=branch_payload.rationale if branch_payload else "Selected based on feasibility and constraints.",
            high_level_dataflow="Brief -> capsule -> drafts -> plan -> validate -> freeze",
            key_design_decisions=[decision_id],
        )
        architecture_section = ArchitectureSection(options=options, chosen=architecture_chosen)

        deliverable_idx = self._next_numeric_id("DL", [])
        milestone_specs = [
            (
                "Workflow Definition",
                ["Draft workflow playbook", "Baseline requirements and acceptance"],
                ["Workflow playbook reviewed and approved", "Requirements baseline signed off"],
            ),
            (
                "Tooling Setup",
                ["Configure Jira templates", "Publish calendar cadence"],
                ["Jira templates verified in pilot project", "Calendar events visible to team"],
            ),
            (
                "Pilot Execution",
                ["Run planning pilot", "Publish metrics report"],
                ["Pilot completed within timeline", "Metrics report shared with stakeholders"],
            ),
        ]
        milestones: list[Milestone] = []
        for idx, (name, deliverables, exit_criteria) in enumerate(milestone_specs, start=1):
            milestone_id = f"M{idx}"
            milestone_deliverables: list[MilestoneDeliverable] = []
            for text in deliverables:
                milestone_deliverables.append(MilestoneDeliverable(id=f"DL{deliverable_idx}", text=text))
                deliverable_idx += 1
            milestones.append(
                Milestone(
                    id=milestone_id,
                    name=name,
                    deliverables=milestone_deliverables,
                    exit_criteria=exit_criteria,
                    depends_on=[f"M{idx - 1}"] if idx > 1 else [],
                )
            )

        evidence_pointers = [
            EvidencePointer(
                candidate_id=candidate_id,
                artifact_ref=branch_ref,
                artifact_id=selected_option_id,
                json_pointer="/plan_package/architecture/chosen",
                note="Selected architecture option",
            )
        ]
        if requirements:
            evidence_pointers.append(
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=branch_ref,
                    artifact_id=requirements[0].id,
                    json_pointer="/plan_package/requirements/0",
                    note="Requirement context",
                )
            )

        decision_log = [
            DecisionLogItem(
                id=decision_id,
                question="Which architecture option should be selected?",
                choice=f"Select {selected_option_id}.",
                rationale="Best balance of feasibility and alignment with constraints.",
                alternatives_considered=[opt_id for opt_id in option_ids if opt_id != selected_option_id],
                what_would_change_this_decision=["Material changes to constraints or risk posture."],
                evidence_pointers=evidence_pointers,
            )
        ]

        failure_modes: list[FailureModeItem] = []
        for idx, risk in enumerate(risk_register, start=1):
            closure_status = "mitigated" if risk.mitigation else "accepted"
            if risk.mitigation:
                closure_rationale = risk.mitigation.text
            elif risk.acceptance:
                closure_rationale = risk.acceptance.rationale
            else:
                closure_rationale = "Accepted for pilot scope."
            failure_modes.append(
                FailureModeItem(
                    id=f"FM{idx}",
                    taxonomy="RISK_REGISTER",
                    category="risk",
                    finding=risk.description or f"Risk {risk.id}",
                    severity=risk.severity,
                    closure=FailureModeClosure(
                        status=closure_status,
                        rationale=closure_rationale,
                        signoff="system",
                    ),
                )
            )

        governance_section = GovernanceSection(
            failure_modes=failure_modes,
            tool_policy=tool_policy,
            hitl_policy=hitl_policy,
        )

        appendix = PlanPackage.Appendix(glossary=capsule.glossary) if capsule.glossary else None
        plan_meta = PlanMeta(
            plan_id=uuid4(),
            version="v1",
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            schema_version=SCHEMA_VERSION,
            candidate_id=candidate_id,
            branch_id=branch_id,
            plan_status="CANDIDATE",
        )

        return PlanPackage(
            meta=plan_meta,
            project_capsule=capsule,
            requirements=requirements,
            acceptance_tests=acceptance_tests,
            architecture=architecture_section,
            milestones=milestones,
            risk_register=risk_register,
            governance=governance_section,
            decision_log=decision_log,
            appendix=appendix,
        )

    def _run_llm_draft_pods(
        self,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        capsule_payload: ProjectCapsulePayload,
        stage: str,
        clarification_context: str | None = None,
    ) -> tuple[
        RequirementsDraftPayload,
        AcceptanceDraftPayload,
        ArchitectureOptionsPayload,
        QaStrategyPayload,
        GovernanceDraftPayload,
    ]:
        capsule_ctx = json.dumps(capsule_payload.model_dump(by_alias=True), indent=2, default=str)
        if clarification_context:
            capsule_ctx = f"{capsule_ctx}\n\nClarification Resolutions:\n{clarification_context}"
        requirements_task = self._prompt_task(
            "requirements",
            "Draft requirements with IDs R# and realistic rationale.",
        )
        req_messages, req_meta = self._json_schema_prompt(
            RequirementsDraftPayload,
            task=requirements_task,
            context=capsule_ctx,
            task_name="requirements",
        )
        architecture_task = self._prompt_task(
            "architecture",
            "Draft architecture options with IDs O#, C#, IF# and associated risks K#.",
        )
        architecture_messages, architecture_meta = self._json_schema_prompt(
            ArchitectureOptionsPayload,
            task=architecture_task,
            context=capsule_ctx,
            task_name="architecture",
        )
        qa_task = self._prompt_task(
            "qa",
            "Draft a concise QA strategy and deterministic quality gates.",
        )
        qa_messages, qa_meta = self._json_schema_prompt(
            QaStrategyPayload,
            task=qa_task,
            context=capsule_ctx,
            task_name="qa",
        )
        governance_task = self._prompt_task(
            "governance",
            "Draft governance with risk_register K# entries and machine-readable "
            "tool_policy/hitl_policy.",
        )
        governance_messages, governance_meta = self._json_schema_prompt(
            GovernanceDraftPayload,
            task=governance_task,
            context=capsule_ctx,
            task_name="governance",
        )

        with ThreadPoolExecutor(max_workers=5) as exe:
            requirements_future = exe.submit(
                self._invoke_role_json,
                runtimes,
                role_configs,
                "requirements_pod",
                req_messages,
                RequirementsDraftPayload,
                stage,
                req_meta,
            )
            architecture_future = exe.submit(
                self._invoke_role_json,
                runtimes,
                role_configs,
                "requirements_pod",
                architecture_messages,
                ArchitectureOptionsPayload,
                stage,
                architecture_meta,
            )
            qa_future = exe.submit(
                self._invoke_role_json,
                runtimes,
                role_configs,
                "requirements_pod",
                qa_messages,
                QaStrategyPayload,
                stage,
                qa_meta,
            )
            governance_future = exe.submit(
                self._invoke_role_json,
                runtimes,
                role_configs,
                "requirements_pod",
                governance_messages,
                GovernanceDraftPayload,
                stage,
                governance_meta,
            )

            requirements_payload = requirements_future.result()
            acceptance_task = self._prompt_task(
                "acceptance",
                "Draft acceptance tests with IDs AT# and map each MUST requirement "
                "to at least one acceptance test.",
            )
            acceptance_messages, acceptance_meta = self._json_schema_prompt(
                AcceptanceDraftPayload,
                task=acceptance_task,
                context=(
                    f"{capsule_ctx}\n\n"
                    f"Requirements:\n{json.dumps(requirements_payload.model_dump(by_alias=True), indent=2)}"
                ),
                task_name="acceptance",
            )
            acceptance_future = exe.submit(
                self._invoke_role_json,
                runtimes,
                role_configs,
                "requirements_pod",
                acceptance_messages,
                AcceptanceDraftPayload,
                stage,
                acceptance_meta,
            )

            acceptance_payload = acceptance_future.result()
            architecture_payload = architecture_future.result()
            qa_payload = qa_future.result()
            governance_payload = governance_future.result()

        return (
            requirements_payload,
            acceptance_payload,
            architecture_payload,
            qa_payload,
            governance_payload,
        )

    def _reid_envelope(
        self,
        envelope: ArtifactEnvelope,
        artifact_id: str,
        parents: list[str] | None = None,
    ) -> ArtifactEnvelope:
        return ArtifactEnvelope(
            artifact_type=envelope.artifact_type,
            artifact_id=artifact_id,
            schema_version=envelope.schema_version,
            created_at=envelope.created_at,
            source_run_id=envelope.source_run_id,
            parents=envelope.parents if parents is None else parents,
            payload=envelope.payload,
        )

    def _artifact_exists(self, store: ArtifactStore, artifact_id: str) -> bool:
        try:
            store.read_artifact(artifact_id)
            return True
        except Exception:
            return False

    def _restore_candidate_states(
        self,
        store: ArtifactStore,
        checkpoint: Checkpoint,
    ) -> dict[str, CandidateState]:
        states: dict[str, CandidateState] = {}
        routing = checkpoint.routing_state
        candidate_artifacts_raw = routing.get("candidate_artifacts")
        if not isinstance(candidate_artifacts_raw, dict):
            return states

        for candidate_id, artifact_id_raw in candidate_artifacts_raw.items():
            artifact_id = str(artifact_id_raw)
            if not self._artifact_exists(store, artifact_id):
                continue
            env = store.read_artifact(artifact_id)
            if env.artifact_type != "plan_candidate":
                continue
            payload = PlanCandidatePayload.model_validate(env.payload)
            state = CandidateState(artifact_id=artifact_id, payload=payload)

            validator_id = f"validator_{candidate_id}_v1"
            if self._artifact_exists(store, validator_id):
                validator_art = store.read_artifact(validator_id)
                state.validator_artifact_id = validator_id
                state.validator_status = str(validator_art.payload.get("overall_status", state.validator_status))

            triage_id = f"triage_{candidate_id}_v1"
            if self._artifact_exists(store, triage_id):
                triage_art = store.read_artifact(triage_id)
                state.triage_artifact_id = triage_id
                defects = triage_art.payload.get("defects", [])
                state.triage_blocked = any(
                    isinstance(d, dict) and d.get("label") == "blocker"
                    for d in (defects if isinstance(defects, list) else [])
                )

            fm_id = f"failure_mode_{candidate_id}_v1"
            if self._artifact_exists(store, fm_id):
                fm_art = store.read_artifact(fm_id)
                state.failure_mode_artifact_id = fm_id
                state.failure_mode_pass = bool(fm_art.payload.get("closure_pass", False))

            states[str(candidate_id)] = state
        return states

    def _restore_branches(self, checkpoint: Checkpoint) -> list[tuple[str, str]]:
        routing = checkpoint.routing_state
        branch_artifacts_raw = routing.get("branch_artifacts")
        if isinstance(branch_artifacts_raw, dict):
            branches = [(str(branch_id), str(artifact_id)) for branch_id, artifact_id in branch_artifacts_raw.items()]
            return sorted(branches, key=lambda x: x[0])
        active = routing.get("active_branches")
        if isinstance(active, list):
            return [(str(branch_id), "") for branch_id in active]
        return []

    def _merge_failure_mode_findings(
        self,
        findings_list: list[FailureModeFindingsPayload],
    ) -> FailureModeFindingsPayload:
        if not findings_list:
            raise ValueError("No failure-mode findings to merge")
        merged = findings_list[0].model_copy(deep=True)
        merged_items = {item.id: item for item in merged.critical_findings}
        for other in findings_list[1:]:
            for item in other.critical_findings:
                if item.id not in merged_items:
                    merged_items[item.id] = item
                elif item.status == "open":
                    merged_items[item.id] = item
        merged.critical_findings = list(merged_items.values())
        merged.closure_pass = all(item.status != "open" for item in merged.critical_findings)
        return merged

    def _judge_role_names(self, role_configs: dict[str, RoleConfig], judge_panel_size: int) -> list[str]:
        roles = sorted([name for name in role_configs if name.startswith("judge_")])
        if not roles:
            return ["judge_1"] * judge_panel_size
        selected: list[str] = []
        for idx in range(judge_panel_size):
            selected.append(roles[idx % len(roles)])
        return selected

    def _triage_role_names(self, role_configs: dict[str, RoleConfig], triage_critics: int) -> list[str]:
        roles = sorted([name for name in role_configs if name.startswith("triage_critic_")])
        if not roles:
            roles = ["requirements_pod"]
        selected: list[str] = []
        for idx in range(triage_critics):
            selected.append(roles[idx % len(roles)])
        return selected

    def _llm_triage_findings(
        self,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        role_name: str,
        critic_id: str,
        candidate_ref: str,
        candidate: PlanCandidatePayload,
    ) -> TriageFindingsPayload:
        plan_json = json.dumps(candidate.plan_package.model_dump(by_alias=True), indent=2, default=str)
        task_text = self._prompt_task(
            "triage",
            "Review the plan package, identify defects, and propose JSON Patch operations that repair them. "
            "Use label blocker/fix/note and include evidence pointers (JSON Pointer) referencing plan_package.",
        )
        messages, prompt_meta = self._json_schema_prompt(
            TriageFindingsPayload,
            task=task_text,
            context=(
                f"Candidate ID: {candidate.candidate_id}\n"
                f"Candidate Ref: {candidate_ref}\n\n"
                f"Plan Package:\n{plan_json}"
            ),
            task_name="triage",
        )
        findings = self._invoke_role_json(
            runtimes,
            role_configs,
            role_name,
            messages,
            TriageFindingsPayload,
            stage="triage",
            prompt_metadata=prompt_meta,
        )
        findings.candidate_ref = candidate_ref
        findings.critic_id = critic_id  # enforce deterministic critic ids
        for defect in findings.defects:
            if not defect.evidence:
                defect.evidence = [
                    EvidencePointer(
                        candidate_id=candidate.candidate_id,
                        artifact_ref=candidate_ref,
                        artifact_id="DEC1",
                        json_pointer="/plan_package/decision_log/0",
                        note="fallback evidence pointer",
                    )
                ]
        return findings

    def _llm_pairwise_judging(
        self,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        candidate_a: PlanCandidatePayload,
        candidate_b: PlanCandidatePayload,
        judge_roles: list[str],
    ) -> tuple[str, list[JudgePairwisePayload], str | None]:
        results: list[JudgePairwisePayload] = []
        votes_a = 0
        votes_b = 0
        a_json = json.dumps(candidate_a.plan_package.model_dump(by_alias=True), indent=2, default=str)
        b_json = json.dumps(candidate_b.plan_package.model_dump(by_alias=True), indent=2, default=str)
        task_text = self._prompt_task(
            "judge",
            "Compare candidate a vs b using this rubric: feasibility, testability, governance, architecture, "
            "clarity (0-10 each). Select winner 'a' or 'b'. Include evidence pointers.",
        )

        def run_judge(index: int, judge_role: str) -> JudgePairwisePayload:
            judge_id = f"J{index}"
            role_cfg = role_configs.get(judge_role)
            messages, prompt_meta = self._json_schema_prompt(
                JudgePairwisePayload,
                task=task_text,
                context=(
                    f"Comparison input:\n"
                    f"a={candidate_a.candidate_id}\n"
                    f"b={candidate_b.candidate_id}\n\n"
                    f"Candidate a plan:\n{a_json}\n\n"
                    f"Candidate b plan:\n{b_json}"
                ),
                task_name="judge",
            )
            judged = self._invoke_role_json(
                runtimes,
                role_configs,
                judge_role,
                messages,
                JudgePairwisePayload,
                stage="judge",
                prompt_metadata=prompt_meta,
            )
            judged.comparison = {"a": candidate_a.candidate_id, "b": candidate_b.candidate_id}
            judged.judge_id = judge_id
            if role_cfg is not None:
                judged.judge_model = f"{role_cfg.model_provider}/{role_cfg.model_name}"
            if not judged.evidence:
                judged.evidence = [
                    EvidencePointer(
                        candidate_id=candidate_a.candidate_id,
                        artifact_ref="candidate",
                        artifact_id="DEC1",
                        json_pointer="/plan_package/decision_log/0",
                    )
                ]
            return judged

        judge_tasks: list[tuple[int, str]] = [
            (index, judge_role) for index, judge_role in enumerate(judge_roles, start=1)
        ]
        results_by_index: dict[int, JudgePairwisePayload] = {}
        errors: list[tuple[int, Exception]] = []
        max_workers = min(self._max_parallel_llm_calls, len(judge_tasks))
        if max_workers <= 1:
            for index, judge_role in judge_tasks:
                try:
                    results_by_index[index] = run_judge(index, judge_role)
                except Exception as exc:
                    errors.append((index, exc))
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(run_judge, index, judge_role): index
                    for index, judge_role in judge_tasks
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    try:
                        results_by_index[index] = future.result()
                    except Exception as exc:
                        errors.append((index, exc))
        if errors:
            errors.sort(key=lambda item: item[0])
            raise errors[0][1]

        for index in sorted(results_by_index):
            judged = results_by_index[index]
            results.append(judged)
            if judged.winner == "a":
                votes_a += 1
            else:
                votes_b += 1

        if votes_a > votes_b:
            return candidate_a.candidate_id, results, None
        if votes_b > votes_a:
            return candidate_b.candidate_id, results, None

        token = hashlib.sha256(
            f"{candidate_a.candidate_id}|{candidate_b.candidate_id}|arbitrator".encode()
        ).hexdigest()
        winner = candidate_a.candidate_id if int(token, 16) % 2 == 0 else candidate_b.candidate_id
        memo = f"Arbitrator selected {winner} after tied judge vote."
        return winner, results, memo

    def _arbitrator_evidence(self, state: CandidateState | None) -> list[dict[str, object]]:
        if state is None:
            return []
        plan = state.payload.plan_package
        candidate_id = state.payload.candidate_id
        candidate_ref = state.artifact_id

        if plan.decision_log:
            item = plan.decision_log[0]
            if item.evidence_pointers:
                return [ep.model_dump(by_alias=True) for ep in item.evidence_pointers]
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=item.id,
                    json_pointer="/plan_package/decision_log/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]

        if plan.requirements:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.requirements[0].id,
                    json_pointer="/plan_package/requirements/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        if plan.acceptance_tests:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.acceptance_tests[0].id,
                    json_pointer="/plan_package/acceptance_tests/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        if plan.milestones:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.milestones[0].id,
                    json_pointer="/plan_package/milestones/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        if plan.risk_register:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.risk_register[0].id,
                    json_pointer="/plan_package/risk_register/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        if plan.governance.failure_modes:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.governance.failure_modes[0].id,
                    json_pointer="/plan_package/governance/failure_modes/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        if plan.project_capsule.assumptions:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.project_capsule.assumptions[0].id,
                    json_pointer="/plan_package/project_capsule/assumptions/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        if plan.project_capsule.open_questions:
            return [
                EvidencePointer(
                    candidate_id=candidate_id,
                    artifact_ref=candidate_ref,
                    artifact_id=plan.project_capsule.open_questions[0].id,
                    json_pointer="/plan_package/project_capsule/open_questions/0",
                    note="arbitrator fallback evidence",
                ).model_dump(by_alias=True)
            ]
        return []

    def _llm_tournament(
        self,
        run_id: UUID,
        runtimes: dict[str, AgentRuntime],
        role_configs: dict[str, RoleConfig],
        candidates: dict[str, CandidateState],
        eligible_ids: list[str],
        judge_panel_size: int,
    ) -> tuple[str, list[JudgePairwisePayload], str | None]:
        if judge_panel_size < 3:
            raise ValueError("Judge panel must be at least 3 for majority voting")
        seed = deterministic_seed(str(run_id), eligible_ids)
        rng = random.Random(seed)
        bracket = sorted(eligible_ids)
        rng.shuffle(bracket)
        judge_roles = self._judge_role_names(role_configs, judge_panel_size)
        all_results: list[JudgePairwisePayload] = []
        arbitrator_memo: str | None = None

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

            pair_results: dict[int, tuple[str, list[JudgePairwisePayload], str | None]] = {}
            errors: list[tuple[int, Exception]] = []
            max_workers = min(self._max_parallel_llm_calls, len(pairs))
            if max_workers <= 1:
                for pair_idx, a_id, b_id in pairs:
                    try:
                        pair_results[pair_idx] = self._llm_pairwise_judging(
                            runtimes,
                            role_configs,
                            candidates[a_id].payload,
                            candidates[b_id].payload,
                            judge_roles,
                        )
                    except Exception as exc:
                        errors.append((pair_idx, exc))
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(
                            self._llm_pairwise_judging,
                            runtimes,
                            role_configs,
                            candidates[a_id].payload,
                            candidates[b_id].payload,
                            judge_roles,
                        ): pair_idx
                        for pair_idx, a_id, b_id in pairs
                    }
                    for future in as_completed(future_map):
                        pair_idx = future_map[future]
                        try:
                            pair_results[pair_idx] = future.result()
                        except Exception as exc:
                            errors.append((pair_idx, exc))
            if errors:
                errors.sort(key=lambda item: item[0])
                raise errors[0][1]

            pair_lookup = {pair_idx: (a_id, b_id) for pair_idx, a_id, b_id in pairs}
            next_round: list[str] = []
            for slot_kind, slot_value in slots:
                if slot_kind == "bye":
                    next_round.append(str(slot_value))
                    continue
                pair_idx = int(slot_value)
                _a_id, _b_id = pair_lookup[pair_idx]
                winner, results, memo = pair_results[pair_idx]
                all_results.extend(results)
                if memo is not None:
                    arbitrator_memo = memo
                next_round.append(winner)
            bracket = next_round
        return bracket[0], all_results, arbitrator_memo

    def run(
        self,
        brief_path: Path,
        config_path: Path,
        *,
        feedback_config: FeedbackConfig | None = None,
        feedback_provider: HumanInputProvider | None = None,
        _resume_run_id: UUID | None = None,
        _resume_run_root: Path | None = None,
        _skip_manifest: bool = False,
        _resume_checkpoint: Checkpoint | None = None,
    ) -> RunResult:
        config_raw = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_raw)
        if _is_hq_config(config):
            pipeline = HQPipeline(
                storage_root=self.storage_root,
                config_path=config_path,
                config_raw=config_raw,
                config=config,
                run_id=_resume_run_id,
                run_root=_resume_run_root,
            )
            return pipeline.run(
                brief_path=brief_path,
                feedback_config=feedback_config,
                feedback_provider=feedback_provider,
            )
        self._max_parallel_llm_calls = self._resolve_max_parallel_llm_calls(config)
        self._llm_timeout_sec = self._resolve_llm_timeout_sec(config)
        self._llm_semaphore = threading.BoundedSemaphore(self._max_parallel_llm_calls)
        tracing_cfg = config.get("tracing", {})
        if isinstance(tracing_cfg, dict):
            configure_tracing(
                enabled=bool(tracing_cfg.get("enabled", False)),
                provider=str(tracing_cfg.get("provider", "noop")),
            )
        self._enforce_portfolio_diversity(config)
        triage_critics = int(config["ensemble_policy"].get("triage_critics", 2))
        if triage_critics < 2:
            raise ValueError("ensemble_policy.triage_critics must be >= 2")
        failure_mode_analysts = int(config["ensemble_policy"].get("failure_mode_analysts", 2))
        if failure_mode_analysts < 1:
            raise ValueError("ensemble_policy.failure_mode_analysts must be >= 1")
        run_id = _resume_run_id if _resume_run_id is not None else uuid4()
        run_root = _resume_run_root if _resume_run_root is not None else (self.storage_root / str(run_id))
        run_root.mkdir(parents=True, exist_ok=True)
        lock_cm = run_lock(run_root)
        self._run_lock_path = lock_cm.__enter__()

        feedback_cfg = feedback_config or FeedbackConfig.from_config(config)
        if feedback_provider is not None:
            provider = feedback_provider
        else:
            provider_key = (feedback_cfg.provider or "auto").lower()
            if provider_key == "cli":
                provider = CLIProvider()
            elif provider_key == "file":
                raise ValueError("feedback provider 'file' requires a response file; pass feedback_provider")
            else:
                provider = AutoProvider()
        planning_store = PlanningArtifactStore(run_root)
        feedback_gate = HumanFeedbackGate(run_id=str(run_id), run_root=run_root, provider=provider)
        clarification_resolutions: ClarificationResolutions | None = None
        clarification_context = ""

        def _release_lock() -> None:
            if self._run_lock_path is None:
                return
            lock_cm.__exit__(None, None, None)
            self._run_lock_path = None
        self._tools_active = tools_enabled(config)
        tool_policy_raw = config.get("tool_policy")
        if tool_policy_raw is None:
            raise ValueError("tool_policy must be configured")
        self._tool_policy = parse_tool_policy(tool_policy_raw)
        if not self._tool_policy.stage_policies:
            raise ValueError("tool_policy.stage_policies must be non-empty")
        hitl_raw = config.get("hitl_policy")
        if hitl_raw is None:
            raise ValueError("hitl_policy must be configured")
        self._hitl_policy = parse_hitl_policy(hitl_raw)
        prompt_root_raw = config.get("prompt_root")
        if isinstance(prompt_root_raw, str) and prompt_root_raw.strip():
            prompt_root = Path(prompt_root_raw)
            if not prompt_root.is_absolute():
                prompt_root = (config_path.parent / prompt_root).resolve()
            self._prompts = PromptLibrary(prompt_root)
        config_version = str(config.get("version") or config.get("stage_machine_version") or "")
        prompt_pack_hash = self._prompts.pack_hash()
        store_full_prompts = bool(config.get("store_full_prompts", False))
        mock_cassette_path: Path | None = None
        mock_cassette_raw = config.get("mock_record_replay_file")
        if isinstance(mock_cassette_raw, str) and mock_cassette_raw.strip():
            candidate_path = Path(mock_cassette_raw)
            if not candidate_path.is_absolute():
                candidate_path = (config_path.parent / candidate_path).resolve()
            mock_cassette_path = candidate_path
        schema_version = self._schema_version_from_config(config)
        role_configs = load_role_configs(config["roles"])
        model_portfolio = self._manifest_model_portfolio(role_configs)
        runtimes: dict[str, AgentRuntime] = {}
        provider_roles: dict[str, list[RoleConfig]] = {}
        for role in role_configs.values():
            provider_roles.setdefault(role.model_provider, []).append(role)
        for provider_name, roles_for_provider in provider_roles.items():
            if provider_name in runtimes:
                continue
            force_mock = provider_name.lower().startswith("mock") or any(
                "mock" in r.model_name.lower() for r in roles_for_provider
            )
            runtimes[provider_name] = AgentRuntime(
                self._build_provider(provider_name, mock_cassette_path=mock_cassette_path, force_mock=force_mock),
                run_root,
                run_id,
                store_full_prompts=store_full_prompts,
            )
        llm_generation_enabled = self._llm_generation_enabled(role_configs)
        ledger = IdempotencyLedger(run_root)

        if not _skip_manifest:
            create_manifest(
                run_root,
                ManifestInput(
                    run_id=run_id,
                    code_version=self._git_code_version(),
                    schema_versions={"artifacts": schema_version},
                    run_config_version=config_version,
                    stage_machine_version=str(config["stage_machine_version"]),
                    validator_suite_version=str(config["validator_suite_version"]),
                    selection_protocol_version=str(config["selection_protocol_version"]),
                    model_portfolio=model_portfolio,
                    branching_policy=dict(config["branching_policy"]),
                    ensemble_policy=dict(config["ensemble_policy"]),
                    iteration_caps=dict(config["iteration_caps"]),
                    tool_policy_version="1.0.0",
                    tools_enabled=bool(config["tools_enabled"]),
                    consent_profile="trusted_user",
                    determinism_disclaimer="LLM outputs are stochastic; orchestrator routing is deterministic.",
                    prompt_pack_hash=prompt_pack_hash,
                    config_raw=config_raw,
                ),
            )
            (run_root / "config.snapshot.yml").write_text(config_raw, encoding="utf-8")
            (run_root / "brief.snapshot.md").write_text(brief_path.read_text(encoding="utf-8"), encoding="utf-8")
            self._write_execution_plan(run_root, config, role_configs)

        store = ArtifactStore(self.storage_root, run_id)
        if _resume_checkpoint is None:
            self._assert_no_orphaned_artifacts(store)
        else:
            self._verify_checkpoint_artifacts(store, _resume_checkpoint.artifact_refs, schema_version)
        brief = brief_path.read_text(encoding="utf-8")

        artifact_refs: dict[str, str] = {}
        stage_idx = 0
        stage_durations: dict[str, float] = {}
        candidate_count = 0
        repaired_candidate_count = 0
        validator_pass_count = 0
        validator_fail_count = 0
        run_start = time.perf_counter()

        branches: list[tuple[str, str]] = []
        candidates: dict[str, CandidateState] = {}
        winner_candidate_id = ""
        winner_artifact_id = ""
        judge_artifacts: list[str] = []

        if _resume_checkpoint is not None:
            artifact_refs = dict(_resume_checkpoint.artifact_refs)
            stage_idx = int(_resume_checkpoint.stage_index) + 1
            branches = self._restore_branches(_resume_checkpoint)
            candidates = self._restore_candidate_states(store, _resume_checkpoint)
            candidate_count = int(_resume_checkpoint.routing_state.get("candidate_count", len(candidates)))
            loop_counters = _resume_checkpoint.routing_state.get("loop_counters", {})
            if isinstance(loop_counters, dict):
                repaired_candidate_count = int(loop_counters.get("repairs_applied", 0))
            budget_counters = _resume_checkpoint.routing_state.get("budget_counters", {})
            if isinstance(budget_counters, dict):
                validator_pass_count = int(budget_counters.get("validator_pass_count", 0))
                validator_fail_count = int(budget_counters.get("validator_fail_count", 0))
            if "candidate" in artifact_refs:
                candidate_artifact = artifact_refs["candidate"]
                for cid, state in candidates.items():
                    if state.artifact_id == candidate_artifact:
                        winner_candidate_id = cid
                        winner_artifact_id = state.artifact_id
                        break
            judge_artifacts = [a.artifact_id for a in store.list_artifacts("judge_pairwise_result")]

        stages_to_run = STAGES[stage_idx:] if stage_idx > 0 else STAGES
        if not stages_to_run:
            frozen = artifact_refs.get("frozen", "")
            if frozen:
                _release_lock()
                return RunResult(run_id=run_id, run_root=run_root, frozen_artifact_id=frozen)
            raise RuntimeError("No stages left to run but run is not frozen")

        for stage in stages_to_run:
            update_run_lock(self._run_lock_path, stage=stage)
            print(f"==> [{stage_idx + 1}/{len(STAGES)}] {stage}", flush=True)
            stage_start = time.perf_counter()
            self._event(run_root, run_id, stage=stage, event_type="stage_transition")
            if self._tools_active:
                self._execute_stage_tool_calls(run_id, run_root, ledger, stage)
            if stage == "intake":
                with trace_span("stage:intake"):
                    if llm_generation_enabled:
                        task_text = self._prompt_task(
                            "capsule",
                            "Convert the project brief into a project capsule. "
                            "Include at least one assumption A#, open question Q#, and one constraint.",
                        )
                        messages, prompt_meta = self._json_schema_prompt(
                            ProjectCapsulePayload,
                            task=task_text,
                            context=brief,
                            task_name="capsule",
                        )
                        capsule_payload = self._invoke_role_json(
                            runtimes,
                            role_configs,
                            "requirements_pod",
                            messages,
                            ProjectCapsulePayload,
                            stage=stage,
                            prompt_metadata=prompt_meta,
                        )
                        capsule_payload.brief = brief
                        capsule = ArtifactEnvelope(
                            artifact_type="project_capsule",
                            artifact_id="capsule_v1",
                            schema_version=SCHEMA_VERSION,
                            created_at=datetime.now(UTC),
                            source_run_id=run_id,
                            parents=[],
                            payload=capsule_payload.model_dump(by_alias=True),
                        )
                    else:
                        capsule = project_capsule(run_id, brief)
                    self._write_artifact(store, run_root, capsule, stage=stage)
                    artifact_refs["capsule"] = capsule.artifact_id

            elif stage == "clarify":
                with trace_span("stage:clarify"):
                    # Pre-plan triage always runs (cheap), clarification gate optional.
                    if planning_store.exists("preplan_triage.json"):
                        triage_payload = PrePlanTriage.model_validate_json(
                            planning_store.path("preplan_triage.json").read_text(encoding="utf-8")
                        )
                    else:
                        if llm_generation_enabled:
                            triage_payload = run_preplan_triage(
                                invoke_json=lambda messages, model, stage, meta: self._invoke_role_json(
                                    runtimes,
                                    role_configs,
                                    "requirements_pod",
                                    messages,
                                    model,
                                    stage,
                                    meta,
                                ),
                                brief=brief,
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
                            if llm_generation_enabled:
                                questions_payload = build_clarification_questions(
                                    invoke_json=lambda messages, model, stage, meta: self._invoke_role_json(
                                        runtimes,
                                        role_configs,
                                        "requirements_pod",
                                        messages,
                                        model,
                                        stage,
                                        meta,
                                    ),
                                    brief=brief,
                                    triage=triage_payload,
                                    stage="clarify",
                                )
                            else:
                                questions_payload = ClarificationQuestions(schema_version="1.0", questions=[])
                            planning_store.write_json(
                                "clarification_questions.json", questions_payload.model_dump()
                            )
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
                                    "how_to_resume": f"rerun with --resume-run {run_id} --response-file <path>"
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
                        clarification_context = json.dumps(clarification_resolutions.model_dump(), indent=2)

                    capsule_art = store.read_artifact(artifact_refs["capsule"])
                    capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                    blocking_questions = [q.id for q in capsule_payload.open_questions if q.blocking]
                    if blocking_questions:
                        if hitl_should_interrupt(self._hitl_policy, "blocking_unknowns"):
                            approved = False
                            interrupt_payload = self._load_interrupt_payload(run_root)
                            approved = bool(interrupt_payload.get("approved", False))
                            if approved:
                                self._event(
                                    run_root,
                                    run_id,
                                    stage=stage,
                                    event_type="decision",
                                    payload={
                                        "mode": "hitl_override",
                                        "reason": "blocking_unknowns",
                                        "blocking_questions": blocking_questions,
                                    },
                                )
                            else:
                                self._event(
                                    run_root,
                                    run_id,
                                    stage=stage,
                                    event_type="decision",
                                    payload={
                                        "interrupt_required": True,
                                        "reason": "blocking_unknowns",
                                        "blocking_questions": blocking_questions,
                                    },
                                )
                                raise RuntimeError("HITL interrupt required: blocking unknowns")
                        else:
                            self._event(
                                run_root,
                                run_id,
                                stage=stage,
                                event_type="decision",
                                payload={
                                    "mode": "policy_override",
                                    "reason": "blocking_unknowns",
                                    "blocking_questions": blocking_questions,
                                },
                            )
                    else:
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="decision",
                            payload={"mode": "lock_assumptions", "status": "no_hitl_interrupt"},
                        )

            elif stage == "draft_pods":
                with trace_span("stage:draft_pods"):
                    if not llm_generation_enabled:
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            "requirements_pod",
                            "Generate requirements draft",
                            stage=stage,
                        )
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            "requirements_pod",
                            "Generate acceptance draft",
                            stage=stage,
                        )
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            "requirements_pod",
                            "Generate architecture options",
                            stage=stage,
                        )
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            "requirements_pod",
                            "Generate QA strategy",
                            stage=stage,
                        )
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            "requirements_pod",
                            "Generate governance draft",
                            stage=stage,
                        )
                    if llm_generation_enabled:
                        capsule_art = store.read_artifact(artifact_refs["capsule"])
                        capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                        (
                            requirements_payload,
                            acceptance_payload,
                            architecture_payload,
                            qa_payload,
                            governance_payload,
                        ) = self._run_llm_draft_pods(
                            runtimes,
                            role_configs,
                            capsule_payload,
                            stage,
                            clarification_context=clarification_context,
                        )

                        drafted = [
                            (
                                "requirements",
                                ArtifactEnvelope(
                                    artifact_type="requirements_draft",
                                    artifact_id="requirements_v1",
                                    schema_version=SCHEMA_VERSION,
                                    created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                    source_run_id=run_id,
                                    parents=[artifact_refs["capsule"]],
                                    payload=requirements_payload.model_dump(by_alias=True),
                                ),
                            ),
                            (
                                "acceptance",
                                ArtifactEnvelope(
                                    artifact_type="acceptance_draft",
                                    artifact_id="acceptance_v1",
                                    schema_version=SCHEMA_VERSION,
                                    created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                    source_run_id=run_id,
                                    parents=[artifact_refs["capsule"]],
                                    payload=acceptance_payload.model_dump(by_alias=True),
                                ),
                            ),
                            (
                                "architecture",
                                ArtifactEnvelope(
                                    artifact_type="architecture_options",
                                    artifact_id="architecture_v1",
                                    schema_version=SCHEMA_VERSION,
                                    created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                    source_run_id=run_id,
                                    parents=[artifact_refs["capsule"]],
                                    payload=architecture_payload.model_dump(by_alias=True),
                                ),
                            ),
                            (
                                "qa",
                                ArtifactEnvelope(
                                    artifact_type="qa_strategy",
                                    artifact_id="qa_v1",
                                    schema_version=SCHEMA_VERSION,
                                    created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                    source_run_id=run_id,
                                    parents=[artifact_refs["capsule"]],
                                    payload=qa_payload.model_dump(by_alias=True),
                                ),
                            ),
                            (
                                "governance",
                                ArtifactEnvelope(
                                    artifact_type="governance_draft",
                                    artifact_id="governance_v1",
                                    schema_version=SCHEMA_VERSION,
                                    created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                    source_run_id=run_id,
                                    parents=[artifact_refs["capsule"]],
                                    payload=governance_payload.model_dump(by_alias=True),
                                ),
                            ),
                        ]
                        for key, env in drafted:
                            self._write_artifact(store, run_root, env, stage=stage)
                            artifact_refs[key] = env.artifact_id
                    else:
                        with ThreadPoolExecutor(max_workers=5) as exe:
                            pod_futures = {
                                "requirements": exe.submit(requirements_draft, run_id, artifact_refs["capsule"]),
                                "acceptance": exe.submit(acceptance_draft, run_id, artifact_refs["capsule"]),
                                "architecture": exe.submit(architecture_options, run_id, artifact_refs["capsule"]),
                                "qa": exe.submit(qa_strategy, run_id, artifact_refs["capsule"]),
                                "governance": exe.submit(governance_draft, run_id, artifact_refs["capsule"]),
                            }
                        for key, fut in pod_futures.items():
                            env = fut.result()
                            self._write_artifact(store, run_root, env, stage=stage)
                            artifact_refs[key] = env.artifact_id

            elif stage == "branch":
                branch_count = int(config["branching_policy"]["branch_count"])
                keep_count = int(config["branching_policy"]["keep_count"])
                contrarian_rule = bool(config["branching_policy"].get("contrarian_rule", False))
                generated: list[tuple[str, str]] = []
                for idx in range(1, branch_count + 1):
                    branch_env = branch_bundle(
                        run_id,
                        artifact_refs["architecture"],
                        idx,
                        contrarian_rule=contrarian_rule,
                    )
                    self._write_artifact(store, run_root, branch_env, stage=stage)
                    branch_id = branch_env.payload["branch_id"]
                    generated.append((branch_id, branch_env.artifact_id))
                branches = sorted(generated, key=lambda x: x[0])[:keep_count]
                artifact_refs["branch"] = branches[0][1]

            elif stage == "synthesize":
                capsule_art = store.read_artifact(artifact_refs["capsule"])
                capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                synth_count = int(config["ensemble_policy"]["synthesizers_per_branch"])
                synth_ids = [f"S{chr(ord('A') + i)}" for i in range(synth_count)]

                work: list[tuple[str, str, str]] = []
                for branch_id, branch_ref in branches:
                    for synth_id in synth_ids:
                        work.append((branch_id, branch_ref, synth_id))

                with ThreadPoolExecutor(max_workers=max(1, len(work))) as exe:
                    if llm_generation_enabled:
                        requirements_art = store.read_artifact(artifact_refs["requirements"])
                        acceptance_art = store.read_artifact(artifact_refs["acceptance"])
                        architecture_art = store.read_artifact(artifact_refs["architecture"])
                        qa_art = store.read_artifact(artifact_refs["qa"])
                        governance_art = store.read_artifact(artifact_refs["governance"])
                        draft_bundle = DraftBundle(
                            requirements=RequirementsDraftPayload.model_validate(requirements_art.payload),
                            acceptance=AcceptanceDraftPayload.model_validate(acceptance_art.payload),
                            architecture=ArchitectureOptionsPayload.model_validate(architecture_art.payload),
                            governance=GovernanceDraftPayload.model_validate(governance_art.payload),
                        )
                        branch_payloads = self._safe_branch_payloads(store, branches, run_root, run_id, stage)
                        capsule_ctx = json.dumps(capsule_payload.model_dump(by_alias=True), indent=2, default=str)
                        requirements_ctx = json.dumps(requirements_art.payload, indent=2, default=str)
                        acceptance_ctx = json.dumps(acceptance_art.payload, indent=2, default=str)
                        architecture_ctx = json.dumps(architecture_art.payload, indent=2, default=str)
                        qa_ctx = json.dumps(qa_art.payload, indent=2, default=str)
                        governance_ctx = json.dumps(governance_art.payload, indent=2, default=str)
                        shared_context = "\n\n".join(
                            [
                                f"Project capsule:\n{capsule_ctx}",
                                f"Requirements draft:\n{requirements_ctx}",
                                f"Acceptance draft:\n{acceptance_ctx}",
                                f"Architecture options:\n{architecture_ctx}",
                                f"QA strategy:\n{qa_ctx}",
                                f"Governance draft:\n{governance_ctx}",
                            ]
                        )
                        if clarification_context:
                            shared_context = f"{shared_context}\n\nClarification Resolutions:\n{clarification_context}"
                        if clarification_context:
                            shared_context = f"{shared_context}\n\nClarification Resolutions:\n{clarification_context}"
                        synth_futures = [
                            exe.submit(
                                self._build_llm_candidate,
                                runtimes,
                                role_configs,
                                run_id,
                                run_root,
                                branch_ref,
                                branch_id,
                                synth_id,
                                shared_context,
                                [
                                    artifact_refs["requirements"],
                                    artifact_refs["acceptance"],
                                    artifact_refs["architecture"],
                                    artifact_refs["qa"],
                                    artifact_refs["governance"],
                                ],
                                capsule_payload,
                                draft_bundle,
                                branch_payloads.get(branch_id),
                            )
                            for branch_id, branch_ref, synth_id in work
                        ]
                    else:
                        synth_futures = [
                            exe.submit(
                                plan_candidate,
                                run_id,
                                capsule_payload,
                                branch_ref,
                                branch_id,
                                synth_id,
                                [
                                    artifact_refs["requirements"],
                                    artifact_refs["acceptance"],
                                    artifact_refs["architecture"],
                                    artifact_refs["qa"],
                                    artifact_refs["governance"],
                                ],
                            )
                            for branch_id, branch_ref, synth_id in work
                        ]
                for fut in synth_futures:
                    env = fut.result()
                    self._write_artifact(store, run_root, env, stage=stage)
                    payload = PlanCandidatePayload.model_validate(env.payload)
                    candidates[payload.candidate_id] = CandidateState(artifact_id=env.artifact_id, payload=payload)
                    candidate_count += 1
                    if not llm_generation_enabled:
                        synth_role = (
                            "synthesizer_a"
                            if payload.synthesizer_id == "SA"
                            else "synthesizer_b" if payload.synthesizer_id == "SB" else "requirements_pod"
                        )
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            synth_role,
                            f"Synthesize candidate {payload.candidate_id}",
                            stage=stage,
                        )
                if candidates:
                    artifact_refs["candidate"] = sorted(candidates.values(), key=lambda c: c.artifact_id)[0].artifact_id

            elif stage == "validate":
                for candidate_id in sorted(candidates.keys()):
                    state = candidates[candidate_id]
                    report = validate_candidate(state.artifact_id, state.payload.plan_package)
                    if report.overall_status == "PASS":
                        validator_pass_count += 1
                    else:
                        validator_fail_count += 1
                    validator_id = f"validator_{candidate_id}_v1"
                    validator = ArtifactEnvelope(
                        artifact_type="validator_report",
                        artifact_id=validator_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=store.read_artifact(state.artifact_id).created_at,
                        source_run_id=run_id,
                        parents=[state.artifact_id],
                        payload=report.model_dump(by_alias=True),
                    )
                    self._write_artifact(store, run_root, validator, stage=stage)
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="validator_result",
                        refs={"artifact_id": validator_id, "candidate_id": candidate_id},
                        payload={"overall_status": report.overall_status},
                        actor={"kind": "validator", "role": "deterministic_suite"},
                    )
                    state.validator_artifact_id = validator_id
                    state.validator_status = report.overall_status

            elif stage == "triage":
                max_repairs = int(config["iteration_caps"]["max_candidate_repairs"])
                critic_roles = self._triage_role_names(role_configs, max(2, triage_critics))
                if len(critic_roles) < 2:
                    critic_roles = ["requirements_pod", "requirements_pod"]
                if triage_critics > 2:
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="decision",
                        payload={"triage_critics_configured": triage_critics, "triage_critics_used": 2},
                    )
                for candidate_id in sorted(candidates.keys()):
                    state = candidates[candidate_id]
                    if llm_generation_enabled:
                        triage_a = self._llm_triage_findings(
                            runtimes,
                            role_configs,
                            critic_roles[0],
                            "A",
                            state.artifact_id,
                            state.payload,
                        )
                        triage_b = self._llm_triage_findings(
                            runtimes,
                            role_configs,
                            critic_roles[1],
                            "B",
                            state.artifact_id,
                            state.payload,
                        )
                        merged = merge_findings(triage_a, triage_b)
                        repairs = 0
                        repaired_plan = False
                        while any(d.label == "blocker" for d in merged.defects) and repairs < max_repairs:
                            try:
                                result = apply_repairs(state.payload.plan_package, merged)
                            except Exception as exc:
                                self._event(
                                    run_root,
                                    run_id,
                                    stage=stage,
                                    event_type="error",
                                    payload={"phase": "triage_apply_repairs", "error": str(exc)},
                                )
                                break
                            if not result.repaired:
                                break
                            state.payload.plan_package = result.updated_plan
                            repairs += 1
                            repaired_plan = True
                            triage_a = self._llm_triage_findings(
                                runtimes,
                                role_configs,
                                critic_roles[0],
                                "A",
                                state.artifact_id,
                                state.payload,
                            )
                            triage_b = self._llm_triage_findings(
                                runtimes,
                                role_configs,
                                critic_roles[1],
                                "B",
                                state.artifact_id,
                                state.payload,
                            )
                            merged = merge_findings(triage_a, triage_b)
                        if repaired_plan:
                            repaired_id = f"{state.artifact_id}_r1"
                            repaired_env = ArtifactEnvelope(
                                artifact_type="plan_candidate",
                                artifact_id=repaired_id,
                                schema_version=SCHEMA_VERSION,
                                created_at=store.read_artifact(state.artifact_id).created_at,
                                source_run_id=run_id,
                                parents=[state.artifact_id],
                                payload=state.payload.model_dump(by_alias=True),
                            )
                            self._write_artifact(store, run_root, repaired_env, stage=stage)
                            state.artifact_id = repaired_id
                            repaired_candidate_count += 1

                        post_repair_report = validate_candidate(state.artifact_id, state.payload.plan_package)
                        state.validator_status = post_repair_report.overall_status
                        post_repair_validator_id = f"validator_{candidate_id}_triage_v1"
                        post_repair_validator = ArtifactEnvelope(
                            artifact_type="validator_report",
                            artifact_id=post_repair_validator_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=post_repair_report.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, post_repair_validator, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="validator_result",
                            refs={"artifact_id": post_repair_validator_id, "candidate_id": candidate_id},
                            payload={
                                "overall_status": post_repair_report.overall_status,
                                "reason": "post_triage_repair",
                            },
                            actor={"kind": "validator", "role": "deterministic_suite"},
                        )
                        state.validator_artifact_id = post_repair_validator_id
                    else:
                        repaired_candidate, merged = triage_repair_loop(
                            state.artifact_id,
                            state.payload,
                            max_repairs=max_repairs,
                        )
                        if repaired_candidate.model_dump() != state.payload.model_dump():
                            repaired_id = f"{state.artifact_id}_r1"
                            repaired_env = ArtifactEnvelope(
                                artifact_type="plan_candidate",
                                artifact_id=repaired_id,
                                schema_version=SCHEMA_VERSION,
                                created_at=store.read_artifact(state.artifact_id).created_at,
                                source_run_id=run_id,
                                parents=[state.artifact_id],
                                payload=repaired_candidate.model_dump(by_alias=True),
                            )
                            self._write_artifact(store, run_root, repaired_env, stage=stage)
                            state.artifact_id = repaired_id
                            state.payload = repaired_candidate
                            repaired_candidate_count += 1

                        post_repair_report = validate_candidate(state.artifact_id, state.payload.plan_package)
                        state.validator_status = post_repair_report.overall_status
                        post_repair_validator_id = f"validator_{candidate_id}_triage_v1"
                        post_repair_validator = ArtifactEnvelope(
                            artifact_type="validator_report",
                            artifact_id=post_repair_validator_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=post_repair_report.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, post_repair_validator, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="validator_result",
                            refs={"artifact_id": post_repair_validator_id, "candidate_id": candidate_id},
                            payload={
                                "overall_status": post_repair_report.overall_status,
                                "reason": "post_triage_repair",
                            },
                            actor={"kind": "validator", "role": "deterministic_suite"},
                        )
                        state.validator_artifact_id = post_repair_validator_id

                        triage_a = critic_findings(state.artifact_id, state.payload, "A")
                        triage_b = critic_findings(state.artifact_id, state.payload, "B")
                        merged = merge_findings(triage_a, triage_b)

                    blocker_defects = [d for d in merged.defects if d.label == "blocker"]
                    if blocker_defects and hitl_should_interrupt(self._hitl_policy, "high_severity_decision"):
                        approved = False
                        allow_override = False
                        interrupt_payload = self._load_interrupt_payload(run_root)
                        approved = bool(interrupt_payload.get("approved", False))
                        allow_override = bool(
                            interrupt_payload.get(
                                "allow_triage_override",
                                interrupt_payload.get("allow_blocker_override", approved),
                            )
                        )
                        if approved and allow_override:
                            evidence: list[dict[str, object]] = []
                            for defect in blocker_defects:
                                for pointer in defect.evidence:
                                    evidence.append(pointer.model_dump(by_alias=True))
                            self._event(
                                run_root,
                                run_id,
                                stage=stage,
                                event_type="decision",
                                payload={
                                    "decision": "hitl_override_triage_blockers",
                                    "blocker_count": len(blocker_defects),
                                    "evidence": evidence,
                                },
                            )
                            merged = merged.model_copy(deep=True)
                            merged.defects = [d for d in merged.defects if d.label != "blocker"]
                            merged.verdict = "approve_with_fixes" if merged.defects else "approve"
                    triage_a.candidate_ref = state.artifact_id
                    triage_b.candidate_ref = state.artifact_id
                    merged.candidate_ref = state.artifact_id
                    triage_a_id = f"triage_{candidate_id}_A_v1"
                    triage_b_id = f"triage_{candidate_id}_B_v1"
                    triage_a_env = ArtifactEnvelope(
                        artifact_type="triage_findings",
                        artifact_id=triage_a_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=store.read_artifact(state.artifact_id).created_at,
                        source_run_id=run_id,
                        parents=[state.artifact_id],
                        payload=triage_a.model_dump(by_alias=True),
                    )
                    triage_b_env = ArtifactEnvelope(
                        artifact_type="triage_findings",
                        artifact_id=triage_b_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=store.read_artifact(state.artifact_id).created_at,
                        source_run_id=run_id,
                        parents=[state.artifact_id],
                        payload=triage_b.model_dump(by_alias=True),
                    )
                    self._write_artifact(store, run_root, triage_a_env, stage=stage)
                    self._write_artifact(store, run_root, triage_b_env, stage=stage)
                    triage_actor_a = (
                        {"kind": "llm", "role": "triage_critic_A"}
                        if llm_generation_enabled
                        else {"kind": "validator", "role": "triage_critic_A"}
                    )
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="triage_result",
                        refs={"artifact_id": triage_a_id, "candidate_id": candidate_id},
                        payload={
                            "verdict": triage_a.verdict,
                            "blocker_count": sum(1 for d in triage_a.defects if d.label == "blocker"),
                        },
                        actor=triage_actor_a,
                    )
                    triage_actor_b = (
                        {"kind": "llm", "role": "triage_critic_B"}
                        if llm_generation_enabled
                        else {"kind": "validator", "role": "triage_critic_B"}
                    )
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="triage_result",
                        refs={"artifact_id": triage_b_id, "candidate_id": candidate_id},
                        payload={
                            "verdict": triage_b.verdict,
                            "blocker_count": sum(1 for d in triage_b.defects if d.label == "blocker"),
                        },
                        actor=triage_actor_b,
                    )
                    triage_id = f"triage_{candidate_id}_v1"
                    triage = ArtifactEnvelope(
                        artifact_type="triage_findings",
                        artifact_id=triage_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=store.read_artifact(state.artifact_id).created_at,
                        source_run_id=run_id,
                        parents=[state.artifact_id],
                        payload=merged.model_dump(by_alias=True),
                    )
                    self._write_artifact(store, run_root, triage, stage=stage)
                    triage_actor_merge = (
                        {"kind": "llm", "role": "triage_merge"}
                        if llm_generation_enabled
                        else {"kind": "validator", "role": "triage_merge"}
                    )
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="triage_result",
                        refs={"artifact_id": triage_id, "candidate_id": candidate_id},
                        payload={
                            "verdict": merged.verdict,
                            "blocker_count": sum(1 for d in merged.defects if d.label == "blocker"),
                        },
                        actor=triage_actor_merge,
                    )
                    state.triage_artifact_id = triage_id
                    state.triage_blocked = any(d.label == "blocker" for d in merged.defects)

            elif stage == "failure_mode":
                max_repairs = int(config["iteration_caps"]["max_candidate_repairs"])
                analyst_count = int(config["ensemble_policy"].get("failure_mode_analysts", 2))
                for candidate_id in sorted(candidates.keys()):
                    state = candidates[candidate_id]
                    findings_list = [
                        analyze_failure_modes(state.artifact_id, state.payload.plan_package)
                        for _ in range(analyst_count)
                    ]
                    for analyst_idx, analyst_findings in enumerate(findings_list, start=1):
                        analyst_id = f"failure_mode_{candidate_id}_A{analyst_idx}_v1"
                        analyst_env = ArtifactEnvelope(
                            artifact_type="failure_mode_findings",
                            artifact_id=analyst_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=analyst_findings.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, analyst_env, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="failure_mode_result",
                            refs={"artifact_id": analyst_id, "candidate_id": candidate_id},
                            payload={
                                "closure_pass": analyst_findings.closure_pass,
                                "critical_findings": len(analyst_findings.critical_findings),
                            },
                            actor={"kind": "validator", "role": f"failure_mode_analyst_{analyst_idx}"},
                        )
                    findings = self._merge_failure_mode_findings(findings_list)
                    repairs = 0
                    while not findings.closure_pass and repairs < max_repairs:
                        if llm_generation_enabled:
                            capsule_art = store.read_artifact(artifact_refs["capsule"])
                            capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                            capsule_ctx = json.dumps(capsule_payload.model_dump(by_alias=True), indent=2, default=str)
                            governance_task = self._prompt_task(
                                "governance",
                                "Draft governance with risk_register K# entries and machine-readable "
                                "tool_policy/hitl_policy.",
                            )
                            gov_messages, gov_meta = self._json_schema_prompt(
                                GovernanceDraftPayload,
                                task=governance_task,
                                context=capsule_ctx,
                                task_name="governance",
                            )
                            governance_payload = self._invoke_role_json(
                                runtimes,
                                role_configs,
                                "requirements_pod",
                                gov_messages,
                                GovernanceDraftPayload,
                                stage=stage,
                                prompt_metadata=gov_meta,
                            )
                            state.payload.plan_package.risk_register = governance_payload.risk_register
                            state.payload.plan_package.governance.tool_policy = governance_payload.tool_policy
                            state.payload.plan_package.governance.hitl_policy = governance_payload.hitl_policy

                            rerun_env = ArtifactEnvelope(
                                artifact_type="governance_draft",
                                artifact_id=f"governance_v1_fm{repairs + 1}",
                                schema_version=SCHEMA_VERSION,
                                created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                source_run_id=run_id,
                                parents=[artifact_refs["capsule"]],
                                payload=governance_payload.model_dump(by_alias=True),
                            )
                            self._write_artifact(store, run_root, rerun_env, stage=stage)
                            self._event(
                                run_root,
                                run_id,
                                stage=stage,
                                event_type="decision",
                                payload={
                                    "decision": "failure_mode_governance_rerun",
                                    "candidate_id": candidate_id,
                                    "repair_iter": repairs + 1,
                                },
                            )

                        state.payload.plan_package = close_open_findings(state.payload.plan_package, findings)
                        findings_list = [
                            analyze_failure_modes(state.artifact_id, state.payload.plan_package)
                            for _ in range(analyst_count)
                        ]
                        findings = self._merge_failure_mode_findings(findings_list)
                        repairs += 1

                    if repairs > 0:
                        repaired_id = f"{state.artifact_id}_fm"
                        repaired_env = ArtifactEnvelope(
                            artifact_type="plan_candidate",
                            artifact_id=repaired_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=state.payload.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, repaired_env, stage=stage)
                        state.artifact_id = repaired_id
                        repaired_candidate_count += 1

                    fm_id = f"failure_mode_{candidate_id}_v1"
                    fm = ArtifactEnvelope(
                        artifact_type="failure_mode_findings",
                        artifact_id=fm_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=store.read_artifact(state.artifact_id).created_at,
                        source_run_id=run_id,
                        parents=[state.artifact_id],
                        payload=findings.model_dump(by_alias=True),
                    )
                    self._write_artifact(store, run_root, fm, stage=stage)
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="failure_mode_result",
                        refs={"artifact_id": fm_id, "candidate_id": candidate_id},
                        payload={
                            "closure_pass": findings.closure_pass,
                            "critical_findings": len(findings.critical_findings),
                        },
                        actor={"kind": "validator", "role": "failure_mode_gate"},
                    )
                    state.failure_mode_artifact_id = fm_id
                    state.failure_mode_pass = findings.closure_pass

            elif stage == "judge":
                judge_panel_size = int(config["ensemble_policy"]["judge_panel_size"])
                max_full_cycles = int(config["iteration_caps"]["max_full_cycles_without_pass"])
                max_pod_reruns = int(config["iteration_caps"]["max_pod_reruns"])
                max_branch_rebuilds = int(config["iteration_caps"]["max_branch_rebuilds"])
                full_cycles = 0
                pod_reruns = 0
                branch_rebuilds = 0
                eligible_ids = [
                    cid
                    for cid, s in sorted(candidates.items())
                    if s.validator_status == "PASS" and not s.triage_blocked and s.failure_mode_pass
                ]
                while not eligible_ids and full_cycles < max_full_cycles:
                    full_cycles += 1
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="decision",
                        payload={"decision": "full_cycle_retry", "cycle": full_cycles},
                    )
                    if pod_reruns < max_pod_reruns:
                        pod_reruns += 1
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="decision",
                            payload={"decision": "pod_rerun", "count": pod_reruns},
                        )
                        if llm_generation_enabled:
                            capsule_art = store.read_artifact(artifact_refs["capsule"])
                            capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                            (
                                requirements_payload,
                                acceptance_payload,
                                architecture_payload,
                                qa_payload,
                                governance_payload,
                            ) = self._run_llm_draft_pods(
                                runtimes,
                                role_configs,
                                capsule_payload,
                                stage,
                            )
                            rerun_envs = [
                                (
                                    "requirements",
                                    ArtifactEnvelope(
                                        artifact_type="requirements_draft",
                                        artifact_id=f"requirements_v1_r{pod_reruns}",
                                        schema_version=SCHEMA_VERSION,
                                        created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                        source_run_id=run_id,
                                        parents=[artifact_refs["capsule"]],
                                        payload=requirements_payload.model_dump(by_alias=True),
                                    ),
                                ),
                                (
                                    "acceptance",
                                    ArtifactEnvelope(
                                        artifact_type="acceptance_draft",
                                        artifact_id=f"acceptance_v1_r{pod_reruns}",
                                        schema_version=SCHEMA_VERSION,
                                        created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                        source_run_id=run_id,
                                        parents=[artifact_refs["capsule"]],
                                        payload=acceptance_payload.model_dump(by_alias=True),
                                    ),
                                ),
                                (
                                    "architecture",
                                    ArtifactEnvelope(
                                        artifact_type="architecture_options",
                                        artifact_id=f"architecture_v1_r{pod_reruns}",
                                        schema_version=SCHEMA_VERSION,
                                        created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                        source_run_id=run_id,
                                        parents=[artifact_refs["capsule"]],
                                        payload=architecture_payload.model_dump(by_alias=True),
                                    ),
                                ),
                                (
                                    "qa",
                                    ArtifactEnvelope(
                                        artifact_type="qa_strategy",
                                        artifact_id=f"qa_v1_r{pod_reruns}",
                                        schema_version=SCHEMA_VERSION,
                                        created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                        source_run_id=run_id,
                                        parents=[artifact_refs["capsule"]],
                                        payload=qa_payload.model_dump(by_alias=True),
                                    ),
                                ),
                                (
                                    "governance",
                                    ArtifactEnvelope(
                                        artifact_type="governance_draft",
                                        artifact_id=f"governance_v1_r{pod_reruns}",
                                        schema_version=SCHEMA_VERSION,
                                        created_at=store.read_artifact(artifact_refs["capsule"]).created_at,
                                        source_run_id=run_id,
                                        parents=[artifact_refs["capsule"]],
                                        payload=governance_payload.model_dump(by_alias=True),
                                    ),
                                ),
                            ]
                        else:
                            rerun_envs = [
                                (
                                    "requirements",
                                    self._reid_envelope(
                                        requirements_draft(run_id, artifact_refs["capsule"]),
                                        f"requirements_v1_r{pod_reruns}",
                                    ),
                                ),
                                (
                                    "acceptance",
                                    self._reid_envelope(
                                        acceptance_draft(run_id, artifact_refs["capsule"]),
                                        f"acceptance_v1_r{pod_reruns}",
                                    ),
                                ),
                                (
                                    "architecture",
                                    self._reid_envelope(
                                        architecture_options(run_id, artifact_refs["capsule"]),
                                        f"architecture_v1_r{pod_reruns}",
                                    ),
                                ),
                                (
                                    "qa",
                                    self._reid_envelope(
                                        qa_strategy(run_id, artifact_refs["capsule"]),
                                        f"qa_v1_r{pod_reruns}",
                                    ),
                                ),
                                (
                                    "governance",
                                    self._reid_envelope(
                                        governance_draft(run_id, artifact_refs["capsule"]),
                                        f"governance_v1_r{pod_reruns}",
                                    ),
                                ),
                            ]
                        for key, env in rerun_envs:
                            self._write_artifact(store, run_root, env, stage=stage)
                            artifact_refs[key] = env.artifact_id

                    if branch_rebuilds < max_branch_rebuilds:
                        branch_rebuilds += 1
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="decision",
                            payload={"decision": "branch_rebuild", "count": branch_rebuilds},
                        )
                        branch_count = int(config["branching_policy"]["branch_count"])
                        keep_count = int(config["branching_policy"]["keep_count"])
                        contrarian_rule = bool(config["branching_policy"].get("contrarian_rule", False))
                        rebuilt: list[tuple[str, str]] = []
                        for idx in range(1, branch_count + 1):
                            branch_env = branch_bundle(
                                run_id,
                                artifact_refs["architecture"],
                                idx,
                                contrarian_rule=contrarian_rule,
                            )
                            rewritten = self._reid_envelope(
                                branch_env,
                                f"{branch_env.artifact_id}_r{branch_rebuilds}",
                                parents=[artifact_refs["architecture"]],
                            )
                            self._write_artifact(store, run_root, rewritten, stage=stage)
                            branch_id = str(rewritten.payload["branch_id"])
                            rebuilt.append((branch_id, rewritten.artifact_id))
                        branches = sorted(rebuilt, key=lambda x: x[0])[:keep_count]
                        artifact_refs["branch"] = branches[0][1]

                    capsule_art = store.read_artifact(artifact_refs["capsule"])
                    capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                    synth_count = int(config["ensemble_policy"]["synthesizers_per_branch"])
                    synth_ids = [f"S{chr(ord('A') + i)}" for i in range(synth_count)]
                    work: list[tuple[str, str, str]] = []
                    for branch_id, branch_ref in branches:
                        for synth_id in synth_ids:
                            work.append((branch_id, branch_ref, synth_id))

                    if llm_generation_enabled:
                        requirements_art = store.read_artifact(artifact_refs["requirements"])
                        acceptance_art = store.read_artifact(artifact_refs["acceptance"])
                        architecture_art = store.read_artifact(artifact_refs["architecture"])
                        qa_art = store.read_artifact(artifact_refs["qa"])
                        governance_art = store.read_artifact(artifact_refs["governance"])
                        draft_bundle = DraftBundle(
                            requirements=RequirementsDraftPayload.model_validate(requirements_art.payload),
                            acceptance=AcceptanceDraftPayload.model_validate(acceptance_art.payload),
                            architecture=ArchitectureOptionsPayload.model_validate(architecture_art.payload),
                            governance=GovernanceDraftPayload.model_validate(governance_art.payload),
                        )
                        branch_payloads = self._safe_branch_payloads(store, branches, run_root, run_id, stage)
                        capsule_ctx = json.dumps(capsule_payload.model_dump(by_alias=True), indent=2, default=str)
                        requirements_ctx = json.dumps(requirements_art.payload, indent=2, default=str)
                        acceptance_ctx = json.dumps(acceptance_art.payload, indent=2, default=str)
                        architecture_ctx = json.dumps(architecture_art.payload, indent=2, default=str)
                        qa_ctx = json.dumps(qa_art.payload, indent=2, default=str)
                        governance_ctx = json.dumps(governance_art.payload, indent=2, default=str)
                        shared_context = "\n\n".join(
                            [
                                f"Project capsule:\n{capsule_ctx}",
                                f"Requirements draft:\n{requirements_ctx}",
                                f"Acceptance draft:\n{acceptance_ctx}",
                                f"Architecture options:\n{architecture_ctx}",
                                f"QA strategy:\n{qa_ctx}",
                                f"Governance draft:\n{governance_ctx}",
                            ]
                        )
                        with ThreadPoolExecutor(max_workers=max(1, len(work))) as exe:
                            retry_futures = [
                                exe.submit(
                                    self._build_llm_candidate,
                                    runtimes,
                                    role_configs,
                                    run_id,
                                    run_root,
                                    branch_ref,
                                    branch_id,
                                    synth_id,
                                    shared_context,
                                    [
                                        artifact_refs["requirements"],
                                        artifact_refs["acceptance"],
                                        artifact_refs["architecture"],
                                        artifact_refs["qa"],
                                        artifact_refs["governance"],
                                    ],
                                    capsule_payload,
                                    draft_bundle,
                                    branch_payloads.get(branch_id),
                                    f"-C{full_cycles}",
                                )
                                for branch_id, branch_ref, synth_id in work
                            ]
                    else:
                        with ThreadPoolExecutor(max_workers=max(1, len(work))) as exe:
                            retry_futures = [
                                exe.submit(
                                    plan_candidate,
                                    run_id,
                                    capsule_payload,
                                    branch_ref,
                                    branch_id,
                                    synth_id,
                                    [
                                        artifact_refs["requirements"],
                                        artifact_refs["acceptance"],
                                        artifact_refs["architecture"],
                                        artifact_refs["qa"],
                                        artifact_refs["governance"],
                                    ],
                                    f"-C{full_cycles}",
                                )
                                for branch_id, branch_ref, synth_id in work
                            ]
                    new_candidate_ids: list[str] = []
                    for fut in retry_futures:
                        env = fut.result()
                        self._write_artifact(store, run_root, env, stage=stage)
                        payload = PlanCandidatePayload.model_validate(env.payload)
                        candidates[payload.candidate_id] = CandidateState(artifact_id=env.artifact_id, payload=payload)
                        new_candidate_ids.append(payload.candidate_id)
                        candidate_count += 1

                    max_repairs = int(config["iteration_caps"]["max_candidate_repairs"])
                    analyst_count = int(config["ensemble_policy"].get("failure_mode_analysts", 2))
                    for candidate_id in sorted(new_candidate_ids):
                        state = candidates[candidate_id]
                        report = validate_candidate(state.artifact_id, state.payload.plan_package)
                        if report.overall_status == "PASS":
                            validator_pass_count += 1
                        else:
                            validator_fail_count += 1
                        validator_id = f"validator_{candidate_id}_v1"
                        validator = ArtifactEnvelope(
                            artifact_type="validator_report",
                            artifact_id=validator_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=report.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, validator, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="validator_result",
                            refs={"artifact_id": validator_id, "candidate_id": candidate_id},
                            payload={"overall_status": report.overall_status},
                            actor={"kind": "validator", "role": "deterministic_suite"},
                        )
                        state.validator_artifact_id = validator_id
                        state.validator_status = report.overall_status

                        repaired_candidate, _ = triage_repair_loop(
                            state.artifact_id,
                            state.payload,
                            max_repairs=max_repairs,
                        )
                        if repaired_candidate.model_dump() != state.payload.model_dump():
                            repaired_id = f"{state.artifact_id}_r1"
                            repaired_env = ArtifactEnvelope(
                                artifact_type="plan_candidate",
                                artifact_id=repaired_id,
                                schema_version=SCHEMA_VERSION,
                                created_at=store.read_artifact(state.artifact_id).created_at,
                                source_run_id=run_id,
                                parents=[state.artifact_id],
                                payload=repaired_candidate.model_dump(by_alias=True),
                            )
                            self._write_artifact(store, run_root, repaired_env, stage=stage)
                            state.artifact_id = repaired_id
                            state.payload = repaired_candidate
                            repaired_candidate_count += 1

                        post_repair_report = validate_candidate(state.artifact_id, state.payload.plan_package)
                        state.validator_status = post_repair_report.overall_status
                        post_repair_validator_id = f"validator_{candidate_id}_cycle{full_cycles}_triage_v1"
                        post_repair_validator = ArtifactEnvelope(
                            artifact_type="validator_report",
                            artifact_id=post_repair_validator_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=post_repair_report.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, post_repair_validator, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="validator_result",
                            refs={"artifact_id": post_repair_validator_id, "candidate_id": candidate_id},
                            payload={
                                "overall_status": post_repair_report.overall_status,
                                "reason": "post_triage_repair_cycle",
                                "cycle": full_cycles,
                            },
                            actor={"kind": "validator", "role": "deterministic_suite"},
                        )
                        state.validator_artifact_id = post_repair_validator_id

                        triage_a = critic_findings(state.artifact_id, state.payload, "A")
                        triage_b = critic_findings(state.artifact_id, state.payload, "B")
                        triage_a_id = f"triage_{candidate_id}_A_v1"
                        triage_b_id = f"triage_{candidate_id}_B_v1"
                        triage_a_env = ArtifactEnvelope(
                            artifact_type="triage_findings",
                            artifact_id=triage_a_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=triage_a.model_dump(by_alias=True),
                        )
                        triage_b_env = ArtifactEnvelope(
                            artifact_type="triage_findings",
                            artifact_id=triage_b_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=triage_b.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, triage_a_env, stage=stage)
                        self._write_artifact(store, run_root, triage_b_env, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="triage_result",
                            refs={"artifact_id": triage_a_id, "candidate_id": candidate_id},
                            payload={
                                "verdict": triage_a.verdict,
                                "blocker_count": sum(1 for d in triage_a.defects if d.label == "blocker"),
                            },
                            actor={"kind": "validator", "role": "triage_critic_A"},
                        )
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="triage_result",
                            refs={"artifact_id": triage_b_id, "candidate_id": candidate_id},
                            payload={
                                "verdict": triage_b.verdict,
                                "blocker_count": sum(1 for d in triage_b.defects if d.label == "blocker"),
                            },
                            actor={"kind": "validator", "role": "triage_critic_B"},
                        )
                        merged = merge_findings(triage_a, triage_b)
                        state.triage_blocked = any(d.label == "blocker" for d in merged.defects)
                        triage_id = f"triage_{candidate_id}_v1"
                        triage = ArtifactEnvelope(
                            artifact_type="triage_findings",
                            artifact_id=triage_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=merged.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, triage, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="triage_result",
                            refs={"artifact_id": triage_id, "candidate_id": candidate_id},
                            payload={
                                "verdict": merged.verdict,
                                "blocker_count": sum(1 for d in merged.defects if d.label == "blocker"),
                            },
                            actor={"kind": "validator", "role": "triage_merge"},
                        )
                        state.triage_artifact_id = triage_id

                        findings_list = [
                            analyze_failure_modes(state.artifact_id, state.payload.plan_package)
                            for _ in range(analyst_count)
                        ]
                        for analyst_idx, analyst_findings in enumerate(findings_list, start=1):
                            analyst_id = f"failure_mode_{candidate_id}_A{analyst_idx}_v1"
                            analyst_env = ArtifactEnvelope(
                                artifact_type="failure_mode_findings",
                                artifact_id=analyst_id,
                                schema_version=SCHEMA_VERSION,
                                created_at=store.read_artifact(state.artifact_id).created_at,
                                source_run_id=run_id,
                                parents=[state.artifact_id],
                                payload=analyst_findings.model_dump(by_alias=True),
                            )
                            self._write_artifact(store, run_root, analyst_env, stage=stage)
                            self._event(
                                run_root,
                                run_id,
                                stage=stage,
                                event_type="failure_mode_result",
                                refs={"artifact_id": analyst_id, "candidate_id": candidate_id},
                                payload={
                                    "closure_pass": analyst_findings.closure_pass,
                                    "critical_findings": len(analyst_findings.critical_findings),
                                },
                                actor={"kind": "validator", "role": f"failure_mode_analyst_{analyst_idx}"},
                            )
                        findings = self._merge_failure_mode_findings(findings_list)
                        repairs = 0
                        while not findings.closure_pass and repairs < max_repairs:
                            state.payload.plan_package = close_open_findings(state.payload.plan_package, findings)
                            findings_list = [
                                analyze_failure_modes(state.artifact_id, state.payload.plan_package)
                                for _ in range(analyst_count)
                            ]
                            findings = self._merge_failure_mode_findings(findings_list)
                            repairs += 1
                        if repairs > 0:
                            repaired_id = f"{state.artifact_id}_fm"
                            repaired_env = ArtifactEnvelope(
                                artifact_type="plan_candidate",
                                artifact_id=repaired_id,
                                schema_version=SCHEMA_VERSION,
                                created_at=store.read_artifact(state.artifact_id).created_at,
                                source_run_id=run_id,
                                parents=[state.artifact_id],
                                payload=state.payload.model_dump(by_alias=True),
                            )
                            self._write_artifact(store, run_root, repaired_env, stage=stage)
                            state.artifact_id = repaired_id
                            repaired_candidate_count += 1
                        fm_id = f"failure_mode_{candidate_id}_v1"
                        fm = ArtifactEnvelope(
                            artifact_type="failure_mode_findings",
                            artifact_id=fm_id,
                            schema_version=SCHEMA_VERSION,
                            created_at=store.read_artifact(state.artifact_id).created_at,
                            source_run_id=run_id,
                            parents=[state.artifact_id],
                            payload=findings.model_dump(by_alias=True),
                        )
                        self._write_artifact(store, run_root, fm, stage=stage)
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="failure_mode_result",
                            refs={"artifact_id": fm_id, "candidate_id": candidate_id},
                            payload={
                                "closure_pass": findings.closure_pass,
                                "critical_findings": len(findings.critical_findings),
                            },
                            actor={"kind": "validator", "role": "failure_mode_gate"},
                        )
                        state.failure_mode_artifact_id = fm_id
                        state.failure_mode_pass = findings.closure_pass

                    eligible_ids = [
                        cid
                        for cid, s in sorted(candidates.items())
                        if s.validator_status == "PASS" and not s.triage_blocked and s.failure_mode_pass
                    ]

                if not eligible_ids:
                    interrupt_payload = self._load_interrupt_payload(run_root)
                    approved = bool(interrupt_payload.get("approved", False))
                    allow_ineligible = bool(interrupt_payload.get("allow_ineligible_candidates", approved))
                    if approved and allow_ineligible and candidates:
                        eligible_ids = sorted(candidates.keys())
                        self._event(
                            run_root,
                            run_id,
                            stage=stage,
                            event_type="decision",
                            payload={
                                "decision": "hitl_override_allow_ineligible",
                                "eligible_ids": eligible_ids,
                            },
                        )

                if not eligible_ids:
                    capsule_art = store.read_artifact(artifact_refs["capsule"])
                    capsule_payload = ProjectCapsulePayload.model_validate(capsule_art.payload)
                    blockers = [
                        {
                            "candidate_id": cid,
                            "validator_status": state.validator_status,
                            "triage_blocked": state.triage_blocked,
                            "failure_mode_pass": state.failure_mode_pass,
                        }
                        for cid, state in sorted(candidates.items())
                    ]
                    required_assumption_changes = [
                        q.id
                        for q in capsule_payload.open_questions
                        if q.blocking or q.impact in {"high", "medium"}
                    ]
                    self._event(
                        run_root,
                        run_id,
                        stage="clarify",
                        event_type="decision",
                        payload={
                            "interrupt_required": True,
                            "reason": "no_acceptable_candidate_after_cycle_cap",
                            "blockers": blockers,
                            "required_assumption_changes": required_assumption_changes,
                        },
                    )
                    raise RuntimeError("HITL interrupt required: no acceptable candidate after cycle cap")

                if llm_generation_enabled:
                    winner_candidate_id, pairwise_results, arbitrator_memo = self._llm_tournament(
                        run_id,
                        runtimes,
                        role_configs,
                        candidates,
                        eligible_ids,
                        judge_panel_size=judge_panel_size,
                    )
                else:
                    tournament = run_tournament(
                        str(run_id),
                        eligible_ids,
                        judge_panel_size=judge_panel_size,
                    )
                    winner_candidate_id = tournament.winner
                    pairwise_results = tournament.pairwise_results
                    arbitrator_memo = tournament.arbitrator_memo
                winner_artifact_id = candidates[winner_candidate_id].artifact_id

                judge_artifacts = []
                for idx, pairwise in enumerate(pairwise_results, start=1):
                    if not llm_generation_enabled:
                        self._invoke_role(
                            runtimes,
                            role_configs,
                            "judge_1",
                            f"Judge comparison {pairwise.comparison['a']} vs {pairwise.comparison['b']}",
                            stage=stage,
                        )
                    judge_id = f"judge_{idx}_v1"
                    judge_env = ArtifactEnvelope(
                        artifact_type="judge_pairwise_result",
                        artifact_id=judge_id,
                        schema_version=SCHEMA_VERSION,
                        created_at=store.read_artifact(winner_artifact_id).created_at,
                        source_run_id=run_id,
                        parents=[winner_artifact_id],
                        payload=pairwise.model_dump(by_alias=True),
                    )
                    self._write_artifact(store, run_root, judge_env, stage=stage)
                    judge_actor = (
                        {"kind": "llm", "role": "judge_panel"}
                        if llm_generation_enabled
                        else {"kind": "validator", "role": "judge_mock"}
                    )
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="judge_pairwise",
                        refs={"artifact_id": judge_id},
                        payload=pairwise.model_dump(by_alias=True),
                        actor=judge_actor,
                    )
                    judge_artifacts.append(judge_id)
                if arbitrator_memo:
                    arbitrator_evidence = self._arbitrator_evidence(candidates.get(winner_candidate_id))
                    self._event(
                        run_root,
                        run_id,
                        stage=stage,
                        event_type="decision",
                        payload={"arbitrator_memo": arbitrator_memo, "evidence": arbitrator_evidence},
                    )
                    require_hitl = hitl_should_interrupt(self._hitl_policy, "high_severity_decision") or bool(
                        config.get("high_impact", False)
                    )
                    if require_hitl:
                        approved = False
                        interrupt_payload = self._load_interrupt_payload(run_root)
                        approved = bool(interrupt_payload.get("approved", False))
                        if approved:
                            self._event(
                                run_root,
                                run_id,
                                stage=stage,
                                event_type="decision",
                                payload={"decision": "hitl_override_high_impact_tie"},
                            )
                        else:
                            self._event(
                                run_root,
                                run_id,
                                stage="clarify",
                                event_type="decision",
                                payload={
                                    "interrupt_required": True,
                                    "reason": "high_impact_tie_requires_signoff",
                                    "arbitrator_memo": arbitrator_memo,
                                    "evidence": arbitrator_evidence,
                                },
                            )
                            raise RuntimeError("HITL interrupt required: high-impact tie arbitration")
                artifact_refs["judge"] = judge_artifacts[0]
                artifact_refs["candidate"] = winner_artifact_id

            elif stage == "freeze":
                if not winner_candidate_id:
                    raise RuntimeError("No winner selected before freeze")
                winner_state = candidates[winner_candidate_id]
                plan_dict = winner_state.payload.plan_package.model_dump(by_alias=True, mode="json")
                approval = None
                if feedback_cfg.plan_review:
                    plan_dict, approval = run_plan_review_loop(
                        run_id=str(run_id),
                        run_root=run_root,
                        plan=plan_dict,
                        gate=feedback_gate,
                        feedback_cfg=feedback_cfg,
                        invoke_json=(
                            lambda messages, model, stage, meta: self._invoke_role_json(
                                runtimes,
                                role_configs,
                                "requirements_pod",
                                messages,
                                model,
                                stage,
                                meta,
                            )
                        ),
                        llm_enabled=llm_generation_enabled,
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
                remaining_open_questions = [
                    q.id for q in winner_state.payload.plan_package.project_capsule.open_questions
                ]
                accepted_risks = [
                    risk.id
                    for risk in winner_state.payload.plan_package.risk_register
                    if risk.acceptance is not None and bool(risk.acceptance.signoff)
                ]
                freeze = freeze_record(
                    run_id,
                    winner_state.artifact_id,
                    winner_candidate_id,
                    validator_summary_ref=winner_state.validator_artifact_id,
                    judge_summary_refs=judge_artifacts,
                    remaining_open_questions=remaining_open_questions,
                    accepted_risks=accepted_risks,
                )
                self._write_artifact(store, run_root, freeze, stage=stage)
                artifact_refs["freeze_record"] = freeze.artifact_id

                frozen = plan_frozen(run_id, winner_state.artifact_id, winner_state.payload, freeze.artifact_id)
                self._write_artifact(store, run_root, frozen, stage=stage)
                artifact_refs["frozen"] = frozen.artifact_id
                markdown = render_plan_markdown(winner_state.payload.plan_package)
                (run_root / "frozen_plan.md").write_text(markdown, encoding="utf-8")
                repo_context_path: Path | None = None
                workspace_context_path: Path | None = None
                handoff_cfg = config.get("handoff", {}) if isinstance(config, dict) else {}
                if isinstance(handoff_cfg, dict):
                    repo_cfg = handoff_cfg.get("repo_context_path")
                    if isinstance(repo_cfg, str) and repo_cfg.strip():
                        repo_context_path = Path(repo_cfg)
                        if not repo_context_path.is_absolute():
                            repo_context_path = (config_path.parent / repo_context_path).resolve()
                    workspace_cfg = handoff_cfg.get("workspace_context_path")
                    if isinstance(workspace_cfg, str) and workspace_cfg.strip():
                        workspace_context_path = Path(workspace_cfg)
                        if not workspace_context_path.is_absolute():
                            workspace_context_path = (config_path.parent / workspace_context_path).resolve()
                if repo_context_path is None and (run_root / "repo_context.json").exists():
                    repo_context_path = run_root / "repo_context.json"
                if workspace_context_path is None and (run_root / "workspace_context.json").exists():
                    workspace_context_path = run_root / "workspace_context.json"
                config_snapshot = build_config_snapshot(config, feedback_cfg)
                finalize_and_write_handoff(
                    run_id=str(run_id),
                    run_root=run_root,
                    plan=winner_state.payload.plan_package.model_dump(by_alias=True, mode="json"),
                    feedback_cfg=feedback_cfg,
                    config_snapshot=config_snapshot,
                    repo_context_path=repo_context_path,
                    workspace_context_path=workspace_context_path,
                    force=False,
                )

            stage_elapsed = time.perf_counter() - stage_start
            stage_durations[stage] = round(stage_elapsed, 6)
            print(f"<== [{stage_idx + 1}/{len(STAGES)}] {stage} ({stage_elapsed:.2f}s)", flush=True)
            cursor = self._event(
                run_root,
                run_id,
                stage=stage,
                event_type="decision",
                payload={"stage_duration_sec": stage_durations[stage]},
            )

            checkpoint = write_checkpoint(
                run_root,
                run_id,
                CheckpointState(
                    stage_name=stage,
                    stage_index=stage_idx,
                    event_cursor=cursor,
                    artifact_refs=dict(artifact_refs),
                    routing_state={
                        "stage": stage,
                        "index": stage_idx,
                        "candidate_count": len(candidates),
                        "branch_count": len(branches),
                        "active_branches": [b for b, _ in branches],
                        "branch_artifacts": {b: ref for b, ref in branches},
                        "active_candidates": sorted(candidates.keys()),
                        "candidate_artifacts": {cid: state.artifact_id for cid, state in candidates.items()},
                        "loop_counters": {
                            "repairs_applied": repaired_candidate_count,
                        },
                        "budget_counters": {
                            "validator_pass_count": validator_pass_count,
                            "validator_fail_count": validator_fail_count,
                            "full_cycles_without_pass": 0,
                        },
                    },
                    tool_side_effect_ledger_ref="tool_ledger.json" if self._tools_active else None,
                ),
            )
            self._event(
                run_root,
                run_id,
                stage=stage,
                event_type="checkpoint",
                refs={"checkpoint_id": checkpoint.checkpoint_id},
                payload={"stage_index": stage_idx},
            )
            stage_idx += 1

        total_duration = round(time.perf_counter() - run_start, 6)
        metrics_payload = RunMetricsPayload(
            run_id=str(run_id),
            stage_durations_sec=stage_durations,
            candidate_count=candidate_count,
            repaired_candidate_count=repaired_candidate_count,
            validator_pass_count=validator_pass_count,
            validator_fail_count=validator_fail_count,
            frozen_count=1 if artifact_refs.get("frozen") else 0,
            total_duration_sec=total_duration,
        )
        metrics_artifact = ArtifactEnvelope(
            artifact_type="run_metrics",
            artifact_id="run_metrics_v1",
            schema_version=SCHEMA_VERSION,
            created_at=store.read_artifact(artifact_refs["frozen"]).created_at,
            source_run_id=run_id,
            parents=[artifact_refs["frozen"]],
            payload=metrics_payload.model_dump(by_alias=True),
        )
        self._write_artifact(store, run_root, metrics_artifact, stage="freeze")
        self._event(
            run_root,
            run_id,
            stage="freeze",
            event_type="decision",
            refs={"artifact_id": metrics_artifact.artifact_id},
            payload=metrics_payload.model_dump(by_alias=True),
        )

        _release_lock()
        return RunResult(run_id=run_id, run_root=run_root, frozen_artifact_id=artifact_refs["frozen"])

    def resume(
        self,
        run_id: str,
        checkpoint_id: str | None = None,
        *,
        feedback_config: FeedbackConfig | None = None,
        feedback_provider: HumanInputProvider | None = None,
    ) -> RunResult:
        run_uuid = UUID(run_id)
        run_root = self.storage_root / run_id
        checkpoint = load_checkpoint(run_root, checkpoint_id) if checkpoint_id else latest_checkpoint(run_root)
        frozen_id = checkpoint.artifact_refs.get("frozen", "")
        if frozen_id:
            return RunResult(run_id=run_uuid, run_root=run_root, frozen_artifact_id=frozen_id)
        config_snapshot = run_root / "config.snapshot.yml"
        brief_snapshot = run_root / "brief.snapshot.md"
        if not config_snapshot.exists() or not brief_snapshot.exists():
            raise FileNotFoundError("Run snapshots missing: config.snapshot.yml and brief.snapshot.md are required")
        resume_checkpoint: Checkpoint | None = checkpoint
        routing = checkpoint.routing_state
        if checkpoint.stage_index >= STAGES.index("branch"):
            branch_map = routing.get("branch_artifacts")
            if not isinstance(branch_map, dict):
                resume_checkpoint = None
        if checkpoint.stage_index >= STAGES.index("synthesize"):
            candidate_map = routing.get("candidate_artifacts")
            if not isinstance(candidate_map, dict):
                resume_checkpoint = None
        return self.run(
            brief_path=brief_snapshot,
            config_path=config_snapshot,
            feedback_config=feedback_config,
            feedback_provider=feedback_provider,
            _resume_run_id=run_uuid,
            _resume_run_root=run_root,
            _skip_manifest=True,
            _resume_checkpoint=resume_checkpoint,
        )

    def fork(self, checkpoint_id: str, new_config_path: Path) -> RunResult:
        config_raw = new_config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_raw)
        role_configs = load_role_configs(config["roles"])
        model_portfolio = self._manifest_model_portfolio(role_configs)
        schema_version = self._schema_version_from_config(config)
        prompt_root_raw = config.get("prompt_root")
        if isinstance(prompt_root_raw, str) and prompt_root_raw.strip():
            prompt_root = Path(prompt_root_raw)
            if not prompt_root.is_absolute():
                prompt_root = (new_config_path.parent / prompt_root).resolve()
            prompts = PromptLibrary(prompt_root)
        else:
            prompts = PromptLibrary()
        config_version = str(config.get("version") or config.get("stage_machine_version") or "")
        prompt_pack_hash = prompts.pack_hash()

        source_checkpoint: Path | None = None
        source_run_root: Path | None = None
        for cp in self.storage_root.glob("*/checkpoints/*.json"):
            if cp.stem == checkpoint_id:
                source_checkpoint = cp
                source_run_root = cp.parent.parent
                break
        if source_checkpoint is None or source_run_root is None:
            raise FileNotFoundError(f"Checkpoint not found: {checkpoint_id}")

        new_run_id = uuid4()
        new_run_root = self.storage_root / str(new_run_id)
        manifest = create_manifest(
            new_run_root,
            ManifestInput(
                run_id=new_run_id,
                code_version=self._git_code_version(),
                schema_versions={"artifacts": schema_version},
                run_config_version=config_version,
                stage_machine_version=str(config["stage_machine_version"]),
                validator_suite_version=str(config["validator_suite_version"]),
                selection_protocol_version=str(config["selection_protocol_version"]),
                model_portfolio=model_portfolio,
                branching_policy=dict(config["branching_policy"]),
                ensemble_policy=dict(config["ensemble_policy"]),
                iteration_caps=dict(config["iteration_caps"]),
                tool_policy_version="1.0.0",
                tools_enabled=bool(config["tools_enabled"]),
                consent_profile="trusted_user",
                determinism_disclaimer="LLM outputs are stochastic; orchestrator routing is deterministic.",
                prompt_pack_hash=prompt_pack_hash,
                config_raw=config_raw,
                parent_run_id=UUID(source_run_root.name),
                forked_from_checkpoint_id=checkpoint_id,
            ),
        )
        _ = fork_from_checkpoint(source_run_root, checkpoint_id, new_run_root, manifest)
        checkpoint = latest_checkpoint(new_run_root)
        return RunResult(
            run_id=new_run_id,
            run_root=new_run_root,
            frozen_artifact_id=checkpoint.artifact_refs.get("frozen", ""),
        )

    def list_artifacts(self, run_id: str) -> list[str]:
        run_uuid = UUID(run_id)
        store = ArtifactStore(self.storage_root, run_uuid)
        artifacts = store.list_artifacts()
        return [a.artifact_id for a in artifacts]

    def show_artifact(self, run_id: str, artifact_id: str) -> dict[str, object]:
        run_uuid = UUID(run_id)
        store = ArtifactStore(self.storage_root, run_uuid)
        env = store.read_artifact(artifact_id)
        return env.model_dump(by_alias=True)

    def events(self, run_id: str, since_event_id: str | None = None) -> list[dict[str, object]]:
        run_root = self.storage_root / run_id
        parsed_since = UUID(since_event_id) if since_event_id else None
        return [event.model_dump(by_alias=True) for event in read_events(run_root, since_event_id=parsed_since)]

    def status(self, run_id: str) -> dict[str, object]:
        run_root = self.storage_root / run_id
        checkpoint = latest_checkpoint(run_root)
        last_event = read_last_event(run_root)
        checkpoints = list_checkpoints(run_root)
        total_stages = len(STAGES)
        return {
            "run_id": run_id,
            "stage_name": checkpoint.stage_name,
            "stage_index": checkpoint.stage_index,
            "total_stages": total_stages,
            "stage_progress": f"{checkpoint.stage_index + 1}/{total_stages}" if total_stages else None,
            "is_complete": "frozen" in checkpoint.artifact_refs,
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

    def interrupt_response(self, run_id: str, payload: dict[str, object]) -> dict[str, object]:
        run_uuid = UUID(run_id)
        run_root = self.storage_root / run_id
        (run_root / "interrupt_response.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        self._event(
            run_root,
            run_uuid,
            stage="clarify",
            event_type="decision",
            refs={},
            payload={"interrupt_response": payload},
        )
        return {"run_id": run_id, "accepted": True}

    def event_count(self, run_id: str) -> int:
        run_root = self.storage_root / run_id
        return len(list(read_events(run_root)))

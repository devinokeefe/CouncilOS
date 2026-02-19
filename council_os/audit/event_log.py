from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

RedactionHook = Callable[[dict[str, object]], dict[str, object]]
_redaction_hook: RedactionHook | None = None


StageName = Literal[
    "intake",
    "intake.capsule_normalize",
    "intake.capsule_reconcile",
    "clarify",
    "clarify.clarification_gate",
    "draft_pods",
    "pods.requirements_draft",
    "pods.architecture_options_draft",
    "pods.qa_templates_draft",
    "pods.risk_gov_draft",
    "merge.requirements_merge",
    "canonicalize.requirements",
    "merge.architecture_merge",
    "canonicalize.architecture",
    "merge.risk_gov_merge",
    "canonicalize.risk_gov",
    "merge.qa_strategy",
    "acceptance.generate",
    "acceptance.canonicalize",
    "audit.alignment",
    "branch",
    "branch.build",
    "synthesize",
    "synthesis.candidate_delta",
    "assemble.plan_candidate",
    "audit.consistency",
    "audit.trace_graph",
    "validate",
    "validate.plan_candidate",
    "triage",
    "triage.critics",
    "repair.patch_generate",
    "repair.patch_apply_and_select",
    "failure_mode",
    "analysis.failure_modes",
    "judge",
    "judge.pairwise",
    "judge.arbitrator",
    "freeze",
    "freeze.freeze",
    "freeze.decision_record",
    "freeze.renderer",
]

ActorKind = Literal["orchestrator", "llm", "validator", "tool", "human"]

EventType = Literal[
    "stage_transition",
    "llm_request",
    "llm_response",
    "artifact_write",
    "validator_result",
    "triage_result",
    "failure_mode_result",
    "judge_pairwise",
    "decision",
    "checkpoint",
    "tool_call",
    "tool_result",
    "error",
]


class EventActor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ActorKind
    role: str


class EventRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", ser_json_exclude_none=True)

    artifact_id: str | None = None
    candidate_id: str | None = None
    checkpoint_id: str | None = None
    parent_event_id: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    event_id: UUID
    ts: datetime
    stage: StageName
    actor: EventActor
    type: EventType
    refs: EventRefs
    payload: dict[str, object]


def _event_path(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root / "events.jsonl"


def set_redaction_hook(hook: RedactionHook | None) -> None:
    global _redaction_hook
    _redaction_hook = hook


def _default_redact(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            lower = key.lower()
            if any(token in lower for token in ("secret", "token", "api_key", "password")):
                output[key] = "***REDACTED***"
            else:
                output[key] = _default_redact(item)
        return output
    if isinstance(value, list):
        return [_default_redact(item) for item in value]
    return value


def append_event(run_root: Path, event: Event) -> None:
    path = _event_path(run_root)
    payload: dict[str, object]
    if _redaction_hook is not None:
        payload = _redaction_hook(event.payload)
    else:
        payload = _default_redact(event.payload)
    event_to_write = event.model_copy(update={"payload": payload})
    with path.open("a", encoding="utf-8") as fh:
        fh.write(event_to_write.model_dump_json())
        fh.write("\n")


def new_event(
    run_id: UUID,
    stage: StageName,
    actor: dict[str, str],
    event_type: EventType,
    refs: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> Event:
    return Event(
        run_id=run_id,
        event_id=uuid4(),
        ts=datetime.now(UTC),
        stage=stage,
        actor=EventActor.model_validate(actor),
        type=event_type,
        refs=EventRefs.model_validate(refs or {}),
        payload=payload or {},
    )


def read_events(run_root: Path, since_event_id: UUID | None = None) -> Iterable[Event]:
    path = _event_path(run_root)
    if not path.exists():
        return []
    events: list[Event] = []
    found_cursor = since_event_id is None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = Event.model_validate(json.loads(line))
        if not found_cursor:
            if event.event_id == since_event_id:
                found_cursor = True
            continue
        events.append(event)
    return events


def read_last_event(run_root: Path) -> Event | None:
    path = _event_path(run_root)
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        return Event.model_validate(json.loads(line))
    return None

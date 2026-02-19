from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from council_os.orchestrator.feedback.schemas import PlanningEvent


def _event_path(run_root: Path) -> Path:
    planning_root = run_root / "planning"
    planning_root.mkdir(parents=True, exist_ok=True)
    return planning_root / "event_log.jsonl"


def append_event(run_root: Path, event: PlanningEvent) -> None:
    path = _event_path(run_root)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(event.model_dump_json())
        fh.write("\n")


def new_event(
    *,
    run_id: str,
    stage: str,
    event_type: str,
    gate_type: str | None = None,
    artifact_refs: list[str] | None = None,
    message: str | None = None,
) -> PlanningEvent:
    return PlanningEvent(
        ts=datetime.now(UTC).isoformat(),
        run_id=run_id,
        stage=stage,
        event_type=event_type,  # type: ignore[arg-type]
        gate_type=gate_type,  # type: ignore[arg-type]
        artifact_refs=artifact_refs or [],
        message=message,
    )

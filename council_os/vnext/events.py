from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


def append_vnext_event(
    run_root: Path,
    phase: Literal["planning", "implementation"],
    event_name: str,
    *,
    run_id: str,
    plan_hash: str | None = None,
    milestone_id: str | None = None,
    task_id: str | None = None,
    job_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    record = {
        "schema_version": "event.v1",
        "event_name": event_name,
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "plan_hash": plan_hash,
        "milestone_id": milestone_id,
        "task_id": task_id,
        "job_id": job_id,
        "payload": payload or {},
    }
    path = run_root / phase / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True))
        handle.write("\n")

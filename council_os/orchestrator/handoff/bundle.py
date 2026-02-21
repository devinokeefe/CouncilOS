from __future__ import annotations

from typing import Any

from council_os.handoff.hashing import artifact_hash
from council_os.handoff.schemas import PlanningHandoffBundle


def compute_handoff_digest(bundle: PlanningHandoffBundle) -> str:
    payload: dict[str, Any] = bundle.model_dump()
    payload["handoff_digest"] = ""
    return artifact_hash(payload)

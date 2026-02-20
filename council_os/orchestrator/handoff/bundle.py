from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from council_os.handoff.hashing import artifact_hash
from council_os.handoff.schemas import PlanningHandoffBundle


def artifact_hash_from_path(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return artifact_hash(payload)


def compute_handoff_digest(bundle: PlanningHandoffBundle) -> str:
    payload: dict[str, Any] = bundle.model_dump()
    payload["handoff_digest"] = ""
    return artifact_hash(payload)


def with_handoff_digest(bundle: PlanningHandoffBundle) -> PlanningHandoffBundle:
    digest = compute_handoff_digest(bundle)
    return bundle.model_copy(update={"handoff_digest": digest})

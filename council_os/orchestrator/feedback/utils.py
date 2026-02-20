from __future__ import annotations

import json
import hashlib
from typing import Any

from council_os.handoff.hashing import canonical_json_dumps, plan_content_hash


def canonical_json(value: dict[str, Any]) -> str:
    return canonical_json_dumps(value)


def plan_hash(plan_dict: dict[str, Any]) -> str:
    return plan_content_hash(plan_dict)


def stable_json_dumps(value: Any) -> str:
    return canonical_json_dumps(value)

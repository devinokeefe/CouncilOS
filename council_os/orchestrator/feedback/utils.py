from __future__ import annotations

import json
import hashlib
from typing import Any


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_hash(plan_dict: dict[str, Any]) -> str:
    payload = canonical_json(plan_dict).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def stable_json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

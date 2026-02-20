from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


def canonical_json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_dumps(value).encode("utf-8")


def artifact_hash(value: Any) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"sha256:{digest}"


_VOLATILE_PLAN_PATHS: tuple[tuple[str, ...], ...] = (
    ("meta", "created_at"),
    ("meta", "source_run_id"),
    ("meta", "plan_status"),
)


def _remove_path(obj: Any, path: tuple[str, ...]) -> None:
    cursor = obj
    for key in path[:-1]:
        if not isinstance(cursor, dict):
            return
        if key not in cursor:
            return
        cursor = cursor[key]
    if isinstance(cursor, dict):
        cursor.pop(path[-1], None)


def normalize_plan_for_hash(plan_obj: Any) -> Any:
    if not isinstance(plan_obj, dict):
        return plan_obj
    plan = deepcopy(plan_obj)
    for path in _VOLATILE_PLAN_PATHS:
        _remove_path(plan, path)
    return plan


def plan_content_hash(plan_obj: Any) -> str:
    normalized = normalize_plan_for_hash(plan_obj)
    digest = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
    return f"sha256:{digest}"

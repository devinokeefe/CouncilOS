from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from fnmatch import fnmatch
from typing import Any, Iterable

from council_os.handoff.schemas import HashSpec


def canonical_json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_dumps(value).encode("utf-8")


def hash_bytes(payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    return f"sha256:{digest}"


def artifact_hash(value: Any) -> str:
    return hash_bytes(canonical_json_bytes(value))


def hash_json(value: Any, *, excluded_pointers: Iterable[str] | None = None) -> str:
    if excluded_pointers:
        value = _apply_excluded_pointers(value, excluded_pointers)
    return artifact_hash(value)


_DEFAULT_EXCLUDED_POINTERS: tuple[str, ...] = (
    "/meta/plan_status",
    "/meta/created_at",
    "/meta/source_run_id",
    "/meta/run_id",
    "/meta/last_updated",
    "/meta/*timestamp*",
    "/meta/*nonce*",
)


def _decode_pointer_segment(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def _pointer_segments(pointer: str) -> list[str]:
    if not pointer or pointer == "/":
        return []
    if pointer.startswith("/"):
        pointer = pointer[1:]
    return [_decode_pointer_segment(seg) for seg in pointer.split("/") if seg]


def _remove_pointer_pattern(obj: Any, segments: list[str]) -> None:
    if not segments:
        return
    head, *tail = segments
    wildcard = "*" in head or "?" in head or "[" in head
    if isinstance(obj, dict):
        if wildcard:
            keys = list(obj.keys())
            for key in keys:
                if fnmatch(str(key), head):
                    if tail:
                        _remove_pointer_pattern(obj.get(key), tail)
                    else:
                        obj.pop(key, None)
        else:
            if head in obj:
                if tail:
                    _remove_pointer_pattern(obj.get(head), tail)
                else:
                    obj.pop(head, None)
    elif isinstance(obj, list):
        if wildcard:
            return
        try:
            idx = int(head)
        except ValueError:
            return
        if 0 <= idx < len(obj):
            if tail:
                _remove_pointer_pattern(obj[idx], tail)
            else:
                obj.pop(idx)


def _apply_excluded_pointers(value: Any, pointers: Iterable[str]) -> Any:
    if not isinstance(value, dict):
        return value
    cloned = deepcopy(value)
    for pointer in pointers:
        segments = _pointer_segments(pointer)
        _remove_pointer_pattern(cloned, segments)
    return cloned


def default_hash_spec() -> HashSpec:
    return HashSpec(excluded_fields_by_pointer=list(_DEFAULT_EXCLUDED_POINTERS))


def normalize_plan_for_hash(plan_obj: Any, *, hash_spec: HashSpec | None = None) -> Any:
    if not isinstance(plan_obj, dict):
        return plan_obj
    spec = hash_spec or default_hash_spec()
    return _apply_excluded_pointers(plan_obj, spec.excluded_fields_by_pointer)


def plan_content_hash(plan_obj: Any, *, hash_spec: HashSpec | None = None) -> str:
    normalized = normalize_plan_for_hash(plan_obj, hash_spec=hash_spec)
    return artifact_hash(normalized)

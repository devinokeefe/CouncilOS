from __future__ import annotations

import hashlib
import json
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import jsonpatch

from council_os.implementation.schemas import PatchChange, PatchsetPayload


class PatchApplyError(Exception):
    pass


class PatchConflictError(PatchApplyError):
    pass


class PatchLimitError(PatchApplyError):
    pass


def _normalize_rel_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _matches_any(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch(path, pattern):
            return True
    return False


def apply_json_patch(doc: Any, patch_ops: list[dict[str, Any]]) -> Any:
    patch = jsonpatch.JsonPatch(patch_ops)
    return patch.apply(doc, in_place=False)


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def apply_patchset(
    repo_root: Path,
    patchset: PatchsetPayload,
    forbidden_paths: list[str],
    protected_paths: list[str],
    op_cap: int,
    *,
    allow_protected: bool = False,
) -> dict[str, Any]:
    if len(patchset.changes) > op_cap:
        raise PatchLimitError(f"Patchset op cap exceeded: {len(patchset.changes)} > {op_cap}")

    order = patchset.apply_order or list(range(len(patchset.changes)))
    if sorted(order) != list(range(len(patchset.changes))):
        raise PatchApplyError("apply_order must contain each change index exactly once")

    changed_files: list[str] = []
    bytes_changed = 0
    for idx in order:
        change = patchset.changes[idx]
        rel_path = _normalize_rel_path(change.path)
        if _matches_any(rel_path, forbidden_paths):
            raise PatchApplyError(f"Patchset touches forbidden path: {rel_path}")
        if not allow_protected and _matches_any(rel_path, protected_paths):
            raise PatchApplyError(f"Patchset touches protected path: {rel_path}")
        target = repo_root / rel_path
        if change.action == "add":
            content = change.after or ""
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            bytes_changed += len(content)
        elif change.action == "modify":
            if not target.exists():
                raise PatchConflictError(f"Missing file for modify: {rel_path}")
            before = change.before or ""
            current = target.read_text(encoding="utf-8")
            if current.replace("\r\n", "\n") != before.replace("\r\n", "\n"):
                raise PatchConflictError(f"Conflict on modify for {rel_path}")
            after = change.after or ""
            target.write_text(after, encoding="utf-8")
            bytes_changed += abs(len(after) - len(before))
        elif change.action == "delete":
            if not target.exists():
                raise PatchConflictError(f"Missing file for delete: {rel_path}")
            before = change.before or ""
            current = target.read_text(encoding="utf-8")
            if current.replace("\r\n", "\n") != before.replace("\r\n", "\n"):
                raise PatchConflictError(f"Conflict on delete for {rel_path}")
            target.unlink()
            bytes_changed += len(before)
        else:
            raise PatchApplyError(f"Unsupported patch action: {change.action}")

        changed_files.append(rel_path)

    file_hashes = {}
    for path in changed_files:
        full_path = repo_root / path
        if full_path.exists():
            file_hashes[path] = _hash_content(full_path.read_text(encoding="utf-8"))
    return {"changed_files": sorted(set(changed_files)), "bytes_changed": bytes_changed, "file_hashes": file_hashes}


def patchset_bytes_changed(patchset: PatchsetPayload) -> int:
    total = 0
    for change in patchset.changes:
        before = change.before or ""
        after = change.after or ""
        if change.action == "add":
            total += len(after)
        elif change.action == "delete":
            total += len(before)
        else:
            total += abs(len(after) - len(before))
    return total


def patchset_operation_count(patchset: PatchsetPayload) -> int:
    return len(patchset.changes)


def select_smallest_patchset(candidates: list[PatchsetPayload]) -> PatchsetPayload:
    scored = []
    for idx, candidate in enumerate(candidates):
        scored.append(
            (
                patchset_operation_count(candidate),
                patchset_bytes_changed(candidate),
                idx,
                candidate,
            )
        )
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return scored[0][3]


def serialize_patchset(patchset: PatchsetPayload) -> str:
    return json.dumps(patchset.model_dump(), sort_keys=True, separators=(",", ":"))

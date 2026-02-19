from __future__ import annotations

from typing import Any


def _walk(path: str, left: Any, right: Any, out: list[dict[str, Any]]) -> None:
    if type(left) is not type(right):
        out.append({"path": path, "kind": "type_mismatch", "left": left, "right": right})
        return

    if isinstance(left, dict):
        keys = sorted(set(left.keys()).union(right.keys()))
        for key in keys:
            child = f"{path}/{key}" if path else f"/{key}"
            if key not in left:
                out.append({"path": child, "kind": "added", "left": None, "right": right[key]})
            elif key not in right:
                out.append({"path": child, "kind": "removed", "left": left[key], "right": None})
            else:
                _walk(child, left[key], right[key], out)
        return

    if isinstance(left, list):
        max_len = max(len(left), len(right))
        for idx in range(max_len):
            child = f"{path}/{idx}" if path else f"/{idx}"
            if idx >= len(left):
                out.append({"path": child, "kind": "added", "left": None, "right": right[idx]})
            elif idx >= len(right):
                out.append({"path": child, "kind": "removed", "left": left[idx], "right": None})
            else:
                _walk(child, left[idx], right[idx], out)
        return

    if left != right:
        out.append({"path": path or "/", "kind": "changed", "left": left, "right": right})


def diff_artifacts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    _walk("", left, right, changes)
    return {"equal": len(changes) == 0, "changes": changes}

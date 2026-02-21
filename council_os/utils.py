from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from council_os.handoff.hashing import canonical_json_dumps
from council_os.agents.roles import RoleConfig


def is_hq_config(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("stages") and raw.get("models") and raw.get("providers"))


def storage_root_from_config(config_path: Path) -> Path:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runtime = raw.get("runtime", {}) if isinstance(raw, dict) else {}
    artifacts = runtime.get("artifacts", {}) if isinstance(runtime, dict) else {}
    store_dir = artifacts.get("store_dir") if isinstance(artifacts, dict) else None
    if isinstance(store_dir, str) and store_dir:
        base = store_dir.replace("{{run_id}}", "").replace("{run_id}", "")
        base = base.rstrip("/\\")
        if base.endswith("artifacts"):
            base = str(Path(base).parent)
        base_path = Path(base) if base else Path(".")
        if not base_path.is_absolute():
            base_path = (config_path.parent / base_path).resolve()
        return base_path
    storage_root = raw.get("storage_root", "CouncilOS/runs") if isinstance(raw, dict) else "CouncilOS/runs"
    if isinstance(storage_root, str):
        base_path = Path(storage_root)
        if not base_path.is_absolute():
            base_path = (config_path.parent / base_path).resolve()
        return base_path
    return (config_path.parent / Path("CouncilOS/runs")).resolve()


def run_git(args: list[str], cwd: Path | None) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
    ).strip()


def git_code_version(*, include_dirty: bool = True, cwd: Path | None = None) -> str:
    try:
        commit = run_git(["rev-parse", "HEAD"], cwd)
        if not include_dirty:
            return commit
        dirty = bool(run_git(["status", "--porcelain"], cwd))
        return f"{commit}:{'dirty' if dirty else 'clean'}"
    except Exception:
        return "unknown:dirty" if include_dirty else "unknown"


def deterministic_json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def deterministic_json_hash(data: Any) -> str:
    return hashlib.sha256(deterministic_json_dumps(data).encode("utf-8")).hexdigest()


def is_valid_json_pointer(pointer: str) -> bool:
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    idx = 1
    length = len(pointer)
    while idx < length:
        ch = pointer[idx]
        if ch == "~":
            if idx + 1 >= length:
                return False
            nxt = pointer[idx + 1]
            if nxt not in {"0", "1"}:
                return False
            idx += 2
            continue
        idx += 1
    return True


def model_portfolio_from_roles(
    roles: dict[str, RoleConfig],
    *,
    sort_keys: bool = False,
) -> dict[str, dict[str, object]]:
    portfolio: dict[str, dict[str, object]] = {}
    items = sorted(roles.items()) if sort_keys else roles.items()
    for name, role in items:
        portfolio[name] = {
            "provider": role.model_provider,
            "model": role.model_name,
            "temperature": role.temperature,
            "max_tokens": role.max_tokens,
            "prompt_version_hash": role.prompt_version_hash,
        }
    return portfolio


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(canonical_json_dumps(payload), encoding="utf-8")

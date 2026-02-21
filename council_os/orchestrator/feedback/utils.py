from __future__ import annotations

from pathlib import Path
from typing import Any

from council_os.handoff.hashing import canonical_json_dumps


def write_planning_artifact(run_root: Path, name: str, payload: dict[str, Any]) -> Path:
    artifacts_root = run_root / "planning" / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    path = artifacts_root / name
    encoded = canonical_json_dumps(payload)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == encoded:
            return path
        raise RuntimeError(f"Planning artifact already exists with different content: {path}")
    path.write_text(encoded, encoding="utf-8")
    return path

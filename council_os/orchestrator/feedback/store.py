from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from council_os.orchestrator.feedback.utils import stable_json_dumps

class PlanningArtifactStoreError(RuntimeError):
    pass


class PlanningArtifactStore:
    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.root = run_root / "planning"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.root / name

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def read_json(self, name: str) -> dict[str, Any]:
        path = self.path(name)
        if not path.exists():
            raise FileNotFoundError(f"Planning artifact not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.path(name)
        encoded = stable_json_dumps(payload)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PlanningArtifactStoreError(f"Failed to read existing artifact {path}") from exc
            if existing != payload:
                raise PlanningArtifactStoreError(f"Planning artifact already exists with different content: {path}")
            return path
        path.write_text(encoded, encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.path(name)
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing != text:
                raise PlanningArtifactStoreError(f"Planning artifact already exists with different content: {path}")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

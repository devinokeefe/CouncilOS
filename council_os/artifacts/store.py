from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from council_os.agents.schemas import ArtifactEnvelope, validate_artifact_payload


class ArtifactStoreError(Exception):
    pass


class ArtifactExistsError(ArtifactStoreError):
    pass


class ParentMissingError(ArtifactStoreError):
    pass


class ArtifactNotFoundError(ArtifactStoreError):
    pass


class ArtifactStore:
    def __init__(self, base_path: Path, run_id: UUID) -> None:
        self.base_path = base_path
        self.run_id = run_id
        self.root = self.base_path / str(self.run_id) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def _artifact_path(self, artifact_type: str, artifact_id: str) -> Path:
        folder = self.root / artifact_type
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{artifact_id}.json"

    def _all_paths(self) -> list[Path]:
        return sorted(self.root.glob("*/*.json"))

    def _artifact_exists(self, artifact_id: str) -> bool:
        return any(path.stem == artifact_id for path in self._all_paths())

    def write_artifact(self, envelope: ArtifactEnvelope) -> Path:
        validate_artifact_payload(envelope)
        if self._artifact_exists(envelope.artifact_id):
            raise ArtifactExistsError(f"Artifact exists: {envelope.artifact_id}")

        for parent in envelope.parents:
            if not self._artifact_exists(parent):
                raise ParentMissingError(f"Parent not found: {parent}")

        path = self._artifact_path(envelope.artifact_type, envelope.artifact_id)
        if path.exists():
            raise ArtifactExistsError(f"Artifact path exists: {path}")

        path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
        return path

    def read_artifact(self, artifact_id: str) -> ArtifactEnvelope:
        for path in self._all_paths():
            if path.stem == artifact_id:
                data = json.loads(path.read_text(encoding="utf-8"))
                return ArtifactEnvelope.model_validate(data)
        raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")

    def list_artifacts(self, artifact_type: str | None = None) -> list[ArtifactEnvelope]:
        if artifact_type is None:
            paths = self._all_paths()
        else:
            paths = sorted((self.root / artifact_type).glob("*.json"))
        return [ArtifactEnvelope.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]

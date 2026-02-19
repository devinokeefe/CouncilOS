from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from datetime import UTC, datetime

from council_os.implementation.schemas import ImplementationArtifactEnvelope, validate_artifact_payload
from council_os.implementation.content import assert_no_markdown
from council_os.implementation.secrets import assert_no_secrets
from council_os.implementation.patches import apply_json_patch


class ImplementationArtifactStoreError(Exception):
    pass


class ArtifactExistsError(ImplementationArtifactStoreError):
    pass


class ParentMissingError(ImplementationArtifactStoreError):
    pass


class ArtifactNotFoundError(ImplementationArtifactStoreError):
    pass


class FrozenStoreError(ImplementationArtifactStoreError):
    pass


class ImplementationArtifactStore:
    def __init__(self, base_path: Path, run_id: UUID) -> None:
        self.base_path = base_path
        self.run_id = run_id
        self.root = self.base_path / str(self.run_id) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _artifact_path(self, artifact_type: str, artifact_id: str) -> Path:
        folder = self.root / artifact_type
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{artifact_id}.json"

    def _all_paths(self) -> list[Path]:
        return sorted(self.root.glob("*/*.json"))

    def _artifact_exists(self, artifact_id: str) -> bool:
        return any(path.stem == artifact_id for path in self._all_paths())

    def write_artifact(self, envelope: ImplementationArtifactEnvelope, patched: bool = False) -> Path:
        if self._frozen:
            raise FrozenStoreError("artifact store is frozen; no further writes allowed")
        assert_no_secrets(envelope.payload)
        assert_no_markdown(envelope.payload)
        validate_artifact_payload(envelope)
        if self._artifact_exists(envelope.artifact_id):
            raise ArtifactExistsError(f"Artifact exists: {envelope.artifact_id}")

        parent_types: set[str] = set()
        for parent in envelope.parents:
            if not self._artifact_exists(parent):
                raise ParentMissingError(f"Parent not found: {parent}")
            parent_types.add(self.read_artifact(parent).artifact_type)
        if envelope.artifact_type in parent_types and not patched:
            raise ImplementationArtifactStoreError("Canonical artifact repairs require JSON Patch")

        path = self._artifact_path(envelope.artifact_type, envelope.artifact_id)
        if path.exists():
            raise ArtifactExistsError(f"Artifact path exists: {path}")

        path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
        return path

    def patch_artifact(
        self,
        artifact_id: str,
        new_artifact_id: str,
        patch_ops: list[dict[str, object]],
        run_id: UUID,
    ) -> Path:
        if self._frozen:
            raise FrozenStoreError("artifact store is frozen; no further writes allowed")
        original = self.read_artifact(artifact_id)
        patched_payload = apply_json_patch(original.payload, patch_ops)
        envelope = ImplementationArtifactEnvelope(
            artifact_type=original.artifact_type,
            artifact_id=new_artifact_id,
            schema_version=original.schema_version,
            created_at=datetime.now(UTC),
            source_run_id=run_id,
            parents=[artifact_id],
            payload=patched_payload,
        )
        return self.write_artifact(envelope, patched=True)

    def read_artifact(self, artifact_id: str) -> ImplementationArtifactEnvelope:
        for path in self._all_paths():
            if path.stem == artifact_id:
                data = json.loads(path.read_text(encoding="utf-8"))
                return ImplementationArtifactEnvelope.model_validate(data)
        raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")

    def list_artifacts(self, artifact_type: str | None = None) -> list[ImplementationArtifactEnvelope]:
        if artifact_type is None:
            paths = self._all_paths()
        else:
            paths = sorted((self.root / artifact_type).glob("*.json"))
        return [
            ImplementationArtifactEnvelope.model_validate_json(p.read_text(encoding="utf-8")) for p in paths
        ]

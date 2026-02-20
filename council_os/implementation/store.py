from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import UUID

from datetime import UTC, datetime

from council_os.implementation.schemas import ImplementationArtifactEnvelope, deterministic_json_dumps, validate_artifact_payload
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
        self._cas_root = self.root.parent / "cas"
        self._cas_root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root.parent / "artifact_index.sqlite"
        self._db = sqlite3.connect(self._index_path)
        self._ensure_index_schema()
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True
        try:
            self._db.commit()
        except Exception:
            pass

    def _artifact_path(self, artifact_type: str, artifact_id: str) -> Path:
        folder = self.root / artifact_type
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{artifact_id}.json"

    def _ensure_index_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                artifact_type TEXT,
                schema_version TEXT,
                created_at TEXT,
                source_run_id TEXT,
                digest TEXT,
                merkle_hash TEXT,
                parents TEXT,
                path TEXT
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS artifact_parents (
                artifact_id TEXT,
                parent_id TEXT
            )
            """
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_artifact_type ON artifacts(artifact_type)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_parent_id ON artifact_parents(parent_id)")
        self._db.commit()

    def _artifact_digest(self, envelope: ImplementationArtifactEnvelope) -> str:
        payload = deterministic_json_dumps(envelope.model_dump())
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def _lookup_digest(self, artifact_id: str) -> str | None:
        cursor = self._db.execute("SELECT digest FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        row = cursor.fetchone()
        if row:
            return row[0]
        try:
            envelope = self.read_artifact(artifact_id)
        except Exception:
            return None
        digest = self._artifact_digest(envelope)
        return digest

    def _compute_merkle_hash(self, digest: str, parents: list[str]) -> str:
        parent_digests: list[str] = []
        for parent_id in parents:
            parent_digest = self._lookup_digest(parent_id)
            if parent_digest:
                parent_digests.append(parent_digest)
        parent_digests.sort()
        data = "|".join([digest, *parent_digests])
        merkle = hashlib.sha256(data.encode("utf-8")).hexdigest()
        return f"sha256:{merkle}"

    def _write_cas(self, envelope: ImplementationArtifactEnvelope, digest: str) -> None:
        algo, _, hexdigest = digest.partition(":")
        cas_dir = self._cas_root / algo
        cas_dir.mkdir(parents=True, exist_ok=True)
        cas_path = cas_dir / f"{hexdigest}.json"
        if cas_path.exists():
            return
        cas_path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")

    def _index_artifact(self, envelope: ImplementationArtifactEnvelope, path: Path) -> None:
        digest = self._artifact_digest(envelope)
        merkle_hash = self._compute_merkle_hash(digest, envelope.parents)
        parents_json = json.dumps(envelope.parents, sort_keys=True)
        self._db.execute(
            """
            INSERT OR REPLACE INTO artifacts
            (artifact_id, artifact_type, schema_version, created_at, source_run_id, digest, merkle_hash, parents, path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.artifact_id,
                envelope.artifact_type,
                envelope.schema_version,
                envelope.created_at.isoformat(),
                str(envelope.source_run_id),
                digest,
                merkle_hash,
                parents_json,
                str(path),
            ),
        )
        self._db.execute("DELETE FROM artifact_parents WHERE artifact_id = ?", (envelope.artifact_id,))
        for parent_id in envelope.parents:
            self._db.execute(
                "INSERT INTO artifact_parents (artifact_id, parent_id) VALUES (?, ?)",
                (envelope.artifact_id, parent_id),
            )
        self._db.commit()
        self._write_cas(envelope, digest)
        self._update_merkle_root()

    def _update_merkle_root(self) -> None:
        cursor = self._db.execute("SELECT merkle_hash FROM artifacts")
        hashes = sorted(row[0] for row in cursor.fetchall() if row and row[0])
        if not hashes:
            return
        root = hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()
        root_path = self.root.parent / "artifact_merkle_root.json"
        payload = {"schema_version": "artifact_merkle_root.v1", "root_hash": f"sha256:{root}"}
        root_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

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
        self._index_artifact(envelope, path)
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

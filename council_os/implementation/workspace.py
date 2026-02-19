from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from council_os.implementation.schemas import (
    ContextPackPayload,
    StageHandoffPayload,
    WorkspaceContextPayload,
    WorkspaceIndexPayload,
    WorkspaceIndexPayloadV21,
    WorkspaceItem,
    WorkspaceItemV21,
    WorkspaceProvenanceV21,
)
from council_os.implementation.secrets import assert_no_secrets
import hashlib


class WorkspaceError(Exception):
    pass


class WorkspaceIndexError(WorkspaceError):
    pass


class WorkspaceManager:
    def __init__(
        self,
        run_root: Path,
        context: WorkspaceContextPayload,
        schema_version: str = "1.0.0",
        run_id: str | None = None,
    ) -> None:
        self.run_root = run_root
        self.context = context
        self.schema_version = schema_version
        self.run_id = run_id
        root = Path(context.workspace_root)
        if not root.is_absolute():
            root = (run_root / root).resolve()
        self.root = root
        self.canonical_root = self.root / "canonical"
        self.non_canonical_root = self.root / "non_canonical"
        self.context_packs_root = self.root / "context_packs"
        self.stage_handoffs_root = self.root / "stage_handoffs"
        self.bundle_context_packs_root = self.run_root / "context_packs"
        self.bundle_stage_handoffs_root = self.run_root / "stage_handoffs"
        for path in (
            self.root,
            self.canonical_root,
            self.non_canonical_root,
            self.context_packs_root,
            self.stage_handoffs_root,
            self.bundle_context_packs_root,
            self.bundle_stage_handoffs_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._index_path = run_root / "workspace_index.json"
        self._index = self._load_index()
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _load_index(self) -> WorkspaceIndexPayload | WorkspaceIndexPayloadV21:
        if not self._index_path.exists():
            if self.schema_version.startswith("2.1"):
                return WorkspaceIndexPayloadV21(schema_version=self.schema_version, items=[])
            return WorkspaceIndexPayload(schema_version=self.schema_version, items=[])
        data = json.loads(self._index_path.read_text(encoding="utf-8"))
        payload = (
            WorkspaceIndexPayloadV21.model_validate(data)
            if self.schema_version.startswith("2.1")
            else WorkspaceIndexPayload.model_validate(data)
        )
        if payload.schema_version != self.schema_version:
            payload.schema_version = self.schema_version
        return payload

    def _save_index(self) -> None:
        self._index.schema_version = self.schema_version
        self._index_path.write_text(self._index.model_dump_json(indent=2), encoding="utf-8")

    def _hash_file(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _record_item(
        self,
        path: Path,
        item_type: str,
        version: str,
        provenance: dict[str, str],
        canonical: bool,
        enforce_partition: bool = True,
    ) -> None:
        if enforce_partition:
            self._assert_partition(path, canonical)
        rel_path = self._relative_path(path)
        existing = next((item for item in self._index.items if item.path == rel_path), None)
        if self.schema_version.startswith("2.1"):
            created_by_stage = provenance.get("stage", provenance.get("created_by_stage", "unknown"))
            run_id = provenance.get("run_id", self.run_id or "")
            payload_hash = provenance.get("hash", self._hash_file(path))
            prov = WorkspaceProvenanceV21(
                created_by_stage=created_by_stage,
                run_id=str(run_id),
                hash=payload_hash,
                role=provenance.get("role"),
            )
            if existing is None:
                self._index.items.append(
                    WorkspaceItemV21(
                        path=rel_path,
                        item_type=item_type,
                        version=version,
                        provenance=prov,
                        canonical=canonical,
                    )
                )
            else:
                existing.item_type = item_type
                existing.version = version
                existing.provenance = prov
                existing.canonical = canonical
        else:
            if existing is None:
                self._index.items.append(
                    WorkspaceItem(
                        path=rel_path,
                        item_type=item_type,
                        version=version,
                        provenance=provenance,
                        canonical=canonical,
                    )
                )
            else:
                existing.item_type = item_type
                existing.version = version
                existing.provenance = provenance
                existing.canonical = canonical
        self._save_index()

    def _assert_partition(self, path: Path, canonical: bool) -> None:
        try:
            rel_path = str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            raise WorkspaceIndexError("workspace path outside workspace_root")
        rules = self.context.partition_rules
        canonical_prefixes = list(rules.canonical_prefixes)
        non_canonical_prefixes = list(rules.non_canonical_prefixes)
        canonical_prefixes.extend(["canonical/", "context_packs/", "stage_handoffs/"])
        non_canonical_prefixes.append("non_canonical/")

        prefixes = canonical_prefixes if canonical else non_canonical_prefixes
        if prefixes and not any(rel_path.startswith(prefix) for prefix in prefixes):
            raise WorkspaceIndexError("workspace partition rule violation")

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.run_root)).replace("\\", "/")
        except ValueError:
            pass
        try:
            return str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def index_artifact(self, path: Path, item_type: str, version: str, provenance: dict[str, str]) -> None:
        self._record_item(path, item_type, version, provenance, canonical=True, enforce_partition=False)

    def write_context_pack(self, payload: ContextPackPayload, provenance: dict[str, str]) -> Path:
        assert_no_secrets(payload.model_dump())
        if self._frozen:
            raise WorkspaceError("workspace is frozen; no context pack writes allowed")
        if payload.size_bytes > payload.max_bytes:
            raise WorkspaceIndexError("context pack exceeds max_bytes")
        if len(payload.artifact_refs) > payload.max_items:
            raise WorkspaceIndexError("context pack exceeds max_items")
        path = self.context_packs_root / f"{payload.pack_id}.json"
        path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        bundle_path = self.bundle_context_packs_root / f"{payload.pack_id}.json"
        bundle_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        self._record_item(path, "context_pack", payload.schema_version, provenance, canonical=True)
        return path

    def write_stage_handoff(self, payload: StageHandoffPayload, provenance: dict[str, str]) -> Path:
        assert_no_secrets(payload.model_dump())
        if self._frozen:
            raise WorkspaceError("workspace is frozen; no new stage handoffs allowed")
        path = self.stage_handoffs_root / f"{payload.handoff_id}.json"
        path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        bundle_path = self.bundle_stage_handoffs_root / f"{payload.handoff_id}.json"
        bundle_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        self._record_item(path, "stage_handoff", payload.schema_version, provenance, canonical=True)
        return path

    def write_canonical_note(self, name: str, payload: dict[str, Any], provenance: dict[str, str]) -> Path:
        if self._frozen:
            raise WorkspaceError("workspace is frozen; no canonical writes allowed")
        assert_no_secrets(payload)
        path = self.canonical_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._record_item(path, "canonical_note", "v1", provenance, canonical=True)
        return path

    def write_non_canonical_note(self, name: str, payload: dict[str, Any], provenance: dict[str, str]) -> Path:
        if self._frozen:
            raise WorkspaceError("workspace is frozen; no non-canonical writes allowed")
        assert_no_secrets(payload)
        path = self.non_canonical_root / name
        if path.exists():
            raise WorkspaceError("non-canonical notes are append-only")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._record_item(path, "non_canonical_note", "v1", provenance, canonical=False)
        return path

    def write_canonical_log(self, name: str, lines: list[dict[str, Any]], provenance: dict[str, str]) -> Path:
        if self._frozen:
            raise WorkspaceError("workspace is frozen; no canonical log writes allowed")
        assert_no_secrets(lines)
        path = self.canonical_root / name
        if path.exists():
            raise WorkspaceError("canonical logs are append-only")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(line, sort_keys=True, default=str) for line in lines) + "\n"
        path.write_text(payload, encoding="utf-8")
        self._record_item(path, "canonical_log", self.schema_version, provenance, canonical=True)
        return path

    def list_indexed_paths(self) -> set[str]:
        return {item.path for item in self._index.items}

    def assert_index_complete(self) -> None:
        indexed = self.list_indexed_paths()
        for folder in (self.context_packs_root, self.stage_handoffs_root, self.canonical_root, self.non_canonical_root):
            for path in list(folder.rglob("*.json")) + list(folder.rglob("*.jsonl")):
                rel_path = self._relative_path(path)
                if rel_path not in indexed:
                    raise WorkspaceIndexError(f"Workspace item not indexed: {rel_path}")

    def index_payload(self) -> WorkspaceIndexPayload:
        return self._index

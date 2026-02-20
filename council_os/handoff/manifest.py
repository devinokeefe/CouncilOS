from __future__ import annotations

from typing import Any

from council_os.handoff.hashing import artifact_hash
from council_os.handoff.schemas import HandoffManifest


def compute_manifest_digest(manifest: HandoffManifest) -> str:
    payload: dict[str, Any] = manifest.model_dump()
    payload["handoff_digest"] = ""
    return artifact_hash(payload)


def with_manifest_digest(manifest: HandoffManifest) -> HandoffManifest:
    return manifest.model_copy(update={"handoff_digest": compute_manifest_digest(manifest)})

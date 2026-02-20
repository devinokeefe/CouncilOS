from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from council_os.handoff.hashing import artifact_hash
from council_os.orchestrator.feedback.utils import stable_json_dumps


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def copy_with_provenance(inputs: dict[str, Path], dest_root: Path) -> dict[str, Path]:
    dest_root.mkdir(parents=True, exist_ok=True)
    provenance: dict[str, dict[str, str]] = {}
    copied: dict[str, Path] = {}

    for label, src in inputs.items():
        src = src.resolve()
        dest = dest_root / src.name
        shutil.copyfile(src, dest)
        provenance[dest.name] = {
            "source_path": str(src),
            "source_sha256": artifact_hash(_load_json(src)),
        }
        copied[label] = dest

    provenance_path = dest_root / "provenance.json"
    provenance_path.write_text(stable_json_dumps(provenance), encoding="utf-8")
    return copied

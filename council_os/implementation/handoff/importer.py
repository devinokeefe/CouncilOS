from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from council_os.handoff.hashing import artifact_hash
from council_os.utils import load_json, write_json


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
            "source_sha256": artifact_hash(load_json(src)),
        }
        copied[label] = dest

    provenance_path = dest_root / "provenance.json"
    write_json(provenance_path, provenance)
    return copied

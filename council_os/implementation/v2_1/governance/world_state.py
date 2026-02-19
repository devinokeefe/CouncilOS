from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from council_os.implementation.schemas import DriftReportPayload, WorldStateResource, WorldStateSnapshotPayload


def _hash_file(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def capture_snapshot(repo_root: Path, snapshot_id: str, env_class: str, tool_ids: list[str]) -> WorldStateSnapshotPayload:
    resources = []
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo_root)).replace("\\", "/")
        resources.append(WorldStateResource(resource_id=rel, hash=_hash_file(path)))
    return WorldStateSnapshotPayload(
        schema_version="2.1.0",
        snapshot_id=snapshot_id,
        tool_ids=tool_ids,
        resources=resources,
        timestamp=datetime.now(UTC),
        env_class=env_class,
    )


def diff_snapshots(pre: WorldStateSnapshotPayload, post: WorldStateSnapshotPayload, drift_id: str) -> DriftReportPayload:
    pre_map = {res.resource_id: res.hash for res in pre.resources}
    post_map = {res.resource_id: res.hash for res in post.resources}
    added = sorted(set(post_map) - set(pre_map))
    removed = sorted(set(pre_map) - set(post_map))
    changed = sorted([key for key in post_map.keys() & pre_map.keys() if post_map[key] != pre_map[key]])
    summary = "no drift"
    if added or removed or changed:
        summary = "drift detected"
    diffs = [
        {"added": added, "removed": removed, "changed": changed},
    ]
    return DriftReportPayload(
        schema_version="2.1.0",
        drift_id=drift_id,
        pre_snapshot_ref=pre.snapshot_id,
        post_snapshot_ref=post.snapshot_id,
        summary=summary,
        classification="tool_drift" if changed else "none",
        diffs=diffs,
    )

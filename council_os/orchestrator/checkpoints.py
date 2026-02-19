from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from council_os.audit.manifest import RunManifest


class CheckpointState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_name: str
    stage_index: int
    event_cursor: str
    artifact_refs: dict[str, str]
    routing_state: dict[str, object]
    tool_side_effect_ledger_ref: str | None = None


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    run_id: UUID
    created_at: datetime
    stage_name: str
    stage_index: int
    event_cursor: str
    artifact_refs: dict[str, str]
    routing_state: dict[str, object]
    tool_side_effect_ledger_ref: str | None = None


class ForkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_run_root: str
    copied_artifacts: int
    checkpoint_id: str
    copied_tool_ledger: bool


def _checkpoint_dir(run_root: Path) -> Path:
    path = run_root / "checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_checkpoint(run_root: Path, run_id: UUID, state: CheckpointState) -> Checkpoint:
    checkpoint = Checkpoint(
        checkpoint_id=f"cp-{uuid4()}",
        run_id=run_id,
        created_at=datetime.now(UTC),
        stage_name=state.stage_name,
        stage_index=state.stage_index,
        event_cursor=state.event_cursor,
        artifact_refs=state.artifact_refs,
        routing_state=state.routing_state,
        tool_side_effect_ledger_ref=state.tool_side_effect_ledger_ref,
    )
    path = _checkpoint_dir(run_root) / f"{checkpoint.checkpoint_id}.json"
    path.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
    return checkpoint


def load_checkpoint(run_root: Path, checkpoint_id: str) -> Checkpoint:
    path = _checkpoint_dir(run_root) / f"{checkpoint_id}.json"
    return Checkpoint.model_validate_json(path.read_text(encoding="utf-8"))


def latest_checkpoint(run_root: Path) -> Checkpoint:
    paths = sorted(_checkpoint_dir(run_root).glob("cp-*.json"), key=lambda p: p.stat().st_mtime)
    if not paths:
        raise FileNotFoundError("No checkpoints found")
    return Checkpoint.model_validate_json(paths[-1].read_text(encoding="utf-8"))


def list_checkpoints(run_root: Path) -> list[Checkpoint]:
    paths = sorted(_checkpoint_dir(run_root).glob("cp-*.json"), key=lambda p: p.stat().st_mtime)
    return [Checkpoint.model_validate_json(path.read_text(encoding="utf-8")) for path in paths]


def fork_from_checkpoint(
    src_run_root: Path,
    checkpoint_id: str,
    dest_run_root: Path,
    new_manifest: RunManifest,
) -> ForkResult:
    source_checkpoint = load_checkpoint(src_run_root, checkpoint_id)
    artifacts_src = src_run_root / "artifacts"
    artifacts_dst = dest_run_root / "artifacts"
    artifacts_dst.parent.mkdir(parents=True, exist_ok=True)
    if artifacts_src.exists():
        shutil.copytree(artifacts_src, artifacts_dst, dirs_exist_ok=True)
    (dest_run_root / "manifest.json").write_text(new_manifest.model_dump_json(indent=2), encoding="utf-8")
    source_ledger = src_run_root / "tool_ledger.json"
    copied_tool_ledger = False
    if source_ledger.exists():
        shutil.copy2(source_ledger, dest_run_root / "tool_ledger.json")
        copied_tool_ledger = True

    boot_checkpoint = write_checkpoint(
        dest_run_root,
        new_manifest.run_id,
        CheckpointState(
            stage_name=source_checkpoint.stage_name,
            stage_index=source_checkpoint.stage_index,
            event_cursor=source_checkpoint.event_cursor,
            artifact_refs=dict(source_checkpoint.artifact_refs),
            routing_state=dict(source_checkpoint.routing_state),
            tool_side_effect_ledger_ref=source_checkpoint.tool_side_effect_ledger_ref,
        ),
    )

    copied = len(list(artifacts_dst.glob("*/*.json"))) if artifacts_dst.exists() else 0
    return ForkResult(
        new_run_root=str(dest_run_root),
        copied_artifacts=copied,
        checkpoint_id=boot_checkpoint.checkpoint_id,
        copied_tool_ledger=copied_tool_ledger,
    )

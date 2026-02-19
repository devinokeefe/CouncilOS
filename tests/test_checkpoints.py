from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from council_os.audit.manifest import ManifestInput, create_manifest, load_manifest
from council_os.orchestrator.checkpoints import CheckpointState, fork_from_checkpoint, load_checkpoint, write_checkpoint


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    cp = write_checkpoint(
        run_root,
        run_id,
        CheckpointState(
            stage_name="intake",
            stage_index=0,
            event_cursor="ev1",
            artifact_refs={"capsule": "capsule_v1"},
            routing_state={"stage": "intake"},
        ),
    )
    loaded = load_checkpoint(run_root, cp.checkpoint_id)
    assert loaded.stage_name == "intake"
    assert loaded.artifact_refs["capsule"] == "capsule_v1"


def test_fork_primitives(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    cp = write_checkpoint(
        run_root,
        run_id,
        CheckpointState(
            stage_name="intake",
            stage_index=0,
            event_cursor="ev1",
            artifact_refs={"capsule": "capsule_v1"},
            routing_state={"stage": "intake"},
            tool_side_effect_ledger_ref="tool_ledger.json",
        ),
    )
    (run_root / "tool_ledger.json").write_text("{}", encoding="utf-8")

    create_manifest(
        run_root,
        ManifestInput(
            run_id=run_id,
            code_version="x",
            schema_versions="1.0.0",
            run_config_version="1.0.0",
            stage_machine_version="1.0.0",
            validator_suite_version="1.0.0",
            selection_protocol_version="1.0.0",
            model_portfolio={},
            branching_policy={},
            ensemble_policy={},
            iteration_caps={},
            tool_policy_version="1.0.0",
            tools_enabled=False,
            consent_profile="trusted",
            determinism_disclaimer="d",
            prompt_pack_hash="hash",
            config_raw="y",
        ),
    )

    new_run_id = uuid4()
    new_run_root = tmp_path / str(new_run_id)
    new_manifest = create_manifest(
        new_run_root,
        ManifestInput(
            run_id=new_run_id,
            code_version="x2",
            schema_versions="1.0.0",
            run_config_version="1.0.0",
            stage_machine_version="1.0.0",
            validator_suite_version="1.0.0",
            selection_protocol_version="1.0.0",
            model_portfolio={},
            branching_policy={},
            ensemble_policy={},
            iteration_caps={},
            tool_policy_version="1.0.0",
            tools_enabled=True,
            consent_profile="trusted",
            determinism_disclaimer="d",
            prompt_pack_hash="hash",
            config_raw="z",
            parent_run_id=run_id,
            forked_from_checkpoint_id=cp.checkpoint_id,
        ),
    )

    result = fork_from_checkpoint(run_root, cp.checkpoint_id, new_run_root, new_manifest)
    assert result.copied_tool_ledger
    assert (new_run_root / "tool_ledger.json").exists()

    boot = load_checkpoint(new_run_root, result.checkpoint_id)
    assert boot.run_id == new_run_id
    assert boot.artifact_refs["capsule"] == "capsule_v1"
    assert boot.tool_side_effect_ledger_ref == "tool_ledger.json"

    loaded_manifest = load_manifest(new_run_root)
    assert loaded_manifest.parent_run_id == run_id
    assert loaded_manifest.forked_from_checkpoint_id == cp.checkpoint_id

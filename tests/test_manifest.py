from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from council_os.audit.manifest import ManifestInput, create_manifest


def test_manifest_write_once(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    manifest_input = ManifestInput(
        run_id=run_id,
        code_version="abc:clean",
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
        config_raw="x",
    )
    create_manifest(run_root, manifest_input)
    with pytest.raises(FileExistsError):
        create_manifest(run_root, manifest_input)


def test_manifest_model_portfolio_shape(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    manifest = create_manifest(
        run_root,
        ManifestInput(
            run_id=run_id,
            code_version="abc:clean",
            schema_versions={"artifacts": "1.0.0"},
            run_config_version="1.0.0",
            stage_machine_version="1.0.0",
            validator_suite_version="1.0.0",
            selection_protocol_version="1.0.0",
            model_portfolio={
                "requirements_pod": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "temperature": 0.2,
                    "max_tokens": 2000,
                    "prompt_version_hash": "requirements_v1",
                }
            },
            branching_policy={},
            ensemble_policy={},
            iteration_caps={},
            tool_policy_version="1.0.0",
            tools_enabled=False,
            consent_profile="trusted",
            determinism_disclaimer="d",
            prompt_pack_hash="hash",
            config_raw="x",
        ),
    )
    entry = manifest.model_portfolio["requirements_pod"]
    assert entry.provider == "openai"
    assert entry.model == "gpt-5-mini"


def test_manifest_rejects_non_normalized_model_portfolio(tmp_path: Path) -> None:
    run_id = uuid4()
    _ = tmp_path
    with pytest.raises(ValidationError):
        ManifestInput(
            run_id=run_id,
            code_version="abc:clean",
            schema_versions={"artifacts": "1.0.0"},
            run_config_version="1.0.0",
            stage_machine_version="1.0.0",
            validator_suite_version="1.0.0",
            selection_protocol_version="1.0.0",
            model_portfolio={
                "requirements_pod": {
                    "model_provider": "openai",
                    "model_name": "gpt-5-mini",
                }
            },
            branching_policy={},
            ensemble_policy={},
            iteration_caps={},
            tool_policy_version="1.0.0",
            tools_enabled=False,
            consent_profile="trusted",
            determinism_disclaimer="d",
            prompt_pack_hash="hash",
            config_raw="x",
        )

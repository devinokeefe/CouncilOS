from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from council_os.agents.roles import load_role_configs
from council_os.agents.schemas import SCHEMA_VERSION
from council_os.orchestrator.hq_pipeline import PROMPT_LABELS, _resolve_config_path
from council_os.utils import is_hq_config


def lint_config(config_path: Path) -> list[str]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return ["Config must be a YAML mapping at the top level."]
    if is_hq_config(raw):
        return _lint_hq_config(raw, config_path)
    return _lint_engine_config(raw, config_path)


def _lint_hq_config(config: dict[str, Any], config_path: Path) -> list[str]:
    errors: list[str] = []
    schemas_version = str(config.get("schemas_version", "")).strip()
    if not schemas_version:
        errors.append("schemas_version must be configured.")
    elif schemas_version != SCHEMA_VERSION:
        errors.append(f"schemas_version mismatch: config={schemas_version}, runtime={SCHEMA_VERSION}.")

    models = config.get("models", {})
    profiles = config.get("profiles", {})
    stages = config.get("stages", [])
    if not isinstance(stages, list):
        return errors + ["stages must be a list."]

    seen_ids: set[str] = set()
    for idx, stage in enumerate(stages):
        if not isinstance(stage, dict):
            errors.append(f"stage[{idx}] must be a mapping.")
            continue
        stage_id = str(stage.get("id", "")).strip()
        if not stage_id:
            errors.append(f"stage[{idx}] missing id.")
            continue
        if stage_id in seen_ids:
            errors.append(f"stage id duplicated: {stage_id}")
        seen_ids.add(stage_id)

        kind = str(stage.get("kind", "")).strip()
        prompt_id = stage.get("prompt_id")
        if kind == "llm":
            if not isinstance(prompt_id, str) or not prompt_id:
                errors.append(f"{stage_id}: prompt_id required for llm stage.")
            elif prompt_id not in PROMPT_LABELS:
                errors.append(f"{stage_id}: prompt_id not found in prompt labels: {prompt_id}.")

        schema_path = stage.get("output_schema")
        if isinstance(schema_path, str) and schema_path:
            schema_file = Path(schema_path)
            if not schema_file.is_absolute():
                schema_file = (config_path.parent / schema_file).resolve()
            if not schema_file.exists():
                errors.append(f"{stage_id}: output_schema not found at {schema_file}.")
        else:
            errors.append(f"{stage_id}: output_schema missing.")

        for call_group in ("calls", "fallbacks"):
            calls = stage.get(call_group, [])
            if not calls:
                continue
            if not isinstance(calls, list):
                errors.append(f"{stage_id}: {call_group} must be a list.")
                continue
            for call in calls:
                if not isinstance(call, dict):
                    errors.append(f"{stage_id}: {call_group} entry must be a mapping.")
                    continue
                model_key = str(call.get("model", "")).strip()
                if not model_key:
                    errors.append(f"{stage_id}: {call_group} missing model key.")
                    continue
                if model_key.startswith("code."):
                    continue
                if not isinstance(models, dict) or model_key not in models:
                    errors.append(f"{stage_id}: model not defined: {model_key}.")
                profile = call.get("profile")
                if profile and (not isinstance(profiles, dict) or profile not in profiles):
                    errors.append(f"{stage_id}: profile not defined: {profile}.")
                enabled_if = call.get("enabled_if")
                if isinstance(enabled_if, str) and enabled_if:
                    resolved = _resolve_config_path(config, enabled_if)
                    if resolved is None:
                        errors.append(f"{stage_id}: enabled_if path not found: {enabled_if}.")
    return errors


def _lint_engine_config(config: dict[str, Any], _config_path: Path) -> list[str]:
    errors: list[str] = []
    required = [
        "roles",
        "branching_policy",
        "ensemble_policy",
        "iteration_caps",
        "tool_policy",
        "hitl_policy",
        "schemas_version",
        "validator_suite_version",
        "selection_protocol_version",
        "stage_machine_version",
    ]
    for key in required:
        if key not in config:
            errors.append(f"missing required key: {key}.")

    schemas_version = str(config.get("schemas_version", "")).strip()
    if schemas_version and schemas_version != SCHEMA_VERSION:
        errors.append(f"schemas_version mismatch: config={schemas_version}, runtime={SCHEMA_VERSION}.")

    roles = config.get("roles")
    if not isinstance(roles, dict) or not roles:
        errors.append("roles must be a non-empty mapping.")
    else:
        try:
            _ = load_role_configs(roles)
        except Exception as exc:
            errors.append(f"roles invalid: {exc}")
        synth_roles = [name for name in roles.keys() if name.startswith("synthesizer_")]
        judge_roles = [name for name in roles.keys() if name.startswith("judge_")]
        if len(synth_roles) < 2:
            errors.append("roles must include at least two synthesizers (synthesizer_a/b).")
        if not judge_roles:
            errors.append("roles must include at least one judge role.")

    tool_policy = config.get("tool_policy")
    if tool_policy is None:
        errors.append("tool_policy must be configured.")
    hitl_policy = config.get("hitl_policy")
    if hitl_policy is None:
        errors.append("hitl_policy must be configured.")

    return errors

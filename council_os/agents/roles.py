from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleConfig:
    role_name: str
    model_provider: str
    model_name: str
    temperature: float
    max_tokens: int
    prompt_version_hash: str
    tools_allowed: list[str]
    output_schema: str | None = None
    top_p: float | None = None


def load_role_configs(raw_roles: dict[str, dict[str, object]]) -> dict[str, RoleConfig]:
    roles: dict[str, RoleConfig] = {}
    for name, data in raw_roles.items():
        tools_raw = data.get("tools_allowed", [])
        tools_allowed = [str(item) for item in (tools_raw if isinstance(tools_raw, list) else [])]
        output_schema_raw = data.get("output_schema")
        output_schema = str(output_schema_raw) if output_schema_raw is not None else None
        roles[name] = RoleConfig(
            role_name=name,
            model_provider=str(data["model_provider"]),
            model_name=str(data["model_name"]),
            temperature=float(str(data["temperature"])),
            max_tokens=int(str(data["max_tokens"])),
            prompt_version_hash=str(data["prompt_version_hash"]),
            tools_allowed=tools_allowed,
            output_schema=output_schema,
            top_p=float(str(data["top_p"])) if "top_p" in data and data["top_p"] is not None else None,
        )
    return roles

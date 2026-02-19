from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_STAGE_MAP: dict[str, str] = {
    "intake": "PLANNING",
    "clarify": "PLANNING",
    "draft_pods": "PLANNING",
    "branch": "PLANNING",
    "synthesize": "PLANNING",
    "validate": "PLANNING",
    "triage": "PLANNING",
    "failure_mode": "PLANNING",
    "judge": "PLANNING",
    "freeze": "PLANNING",
}


@dataclass(frozen=True)
class StagePolicy:
    stage: str
    allowlisted_tools: list[str]
    restrictions: list[dict[str, object]]
    hitl_triggers: list[str]


@dataclass(frozen=True)
class ToolPolicyConfig:
    stage_policies: list[StagePolicy]


@dataclass(frozen=True)
class HitlPolicyConfig:
    when_to_interrupt: list[str]
    approval_roles: list[str]


class ToolPolicyError(Exception):
    pass


class ToolApprovalRequired(ToolPolicyError):
    pass


class ToolRestrictionError(ToolPolicyError):
    pass


def tools_enabled(config: dict[str, object]) -> bool:
    return bool(config.get("tools_enabled", False))


def parse_tool_policy(raw: dict[str, Any] | None) -> ToolPolicyConfig:
    if raw is None:
        return ToolPolicyConfig(stage_policies=[])
    policies_raw = raw.get("stage_policies", [])
    parsed: list[StagePolicy] = []
    for item in policies_raw:
        parsed.append(
            StagePolicy(
                stage=str(item.get("stage", "")),
                allowlisted_tools=[str(t) for t in item.get("allowlisted_tools", [])],
                restrictions=[dict(r) for r in item.get("restrictions", [])],
                hitl_triggers=[str(t) for t in item.get("hitl_triggers", [])],
            )
        )
    return ToolPolicyConfig(stage_policies=parsed)


def parse_hitl_policy(raw: dict[str, Any] | None) -> HitlPolicyConfig:
    if raw is None:
        return HitlPolicyConfig(when_to_interrupt=[], approval_roles=[])
    when = raw.get("when_to_interrupt", [])
    roles = raw.get("approval_roles", [])
    when_list = [str(item) for item in (when if isinstance(when, list) else [])]
    roles_list = [str(item) for item in (roles if isinstance(roles, list) else [])]
    return HitlPolicyConfig(when_to_interrupt=when_list, approval_roles=roles_list)


def hitl_should_interrupt(policy: HitlPolicyConfig, trigger: str) -> bool:
    return trigger in policy.when_to_interrupt


def _resolve_stage_policy(policy: ToolPolicyConfig, stage: str) -> StagePolicy:
    stage_policy = next((p for p in policy.stage_policies if p.stage == stage), None)
    if stage_policy is not None:
        return stage_policy
    mapped = _STAGE_MAP.get(stage, "")
    if mapped:
        mapped_policy = next((p for p in policy.stage_policies if p.stage == mapped), None)
        if mapped_policy is not None:
            return mapped_policy
    if len(policy.stage_policies) == 1:
        return policy.stage_policies[0]
    raise ToolPolicyError(f"No policy configured for stage: {stage}")


def tool_call_requires_approval(policy: ToolPolicyConfig, stage: str, args: dict[str, Any]) -> bool:
    stage_policy = _resolve_stage_policy(policy, stage)

    trigger_values = {str(v) for v in args.values() if isinstance(v, str)}
    return any(trigger in trigger_values for trigger in stage_policy.hitl_triggers)


def enforce_tool_policy(
    policy: ToolPolicyConfig,
    stage: str,
    tool_name: str,
    args: dict[str, Any],
    approved: bool,
) -> None:
    stage_policy = _resolve_stage_policy(policy, stage)

    if tool_name not in stage_policy.allowlisted_tools:
        raise ToolPolicyError(f"Tool not allowlisted in stage {stage}: {tool_name}")

    for restriction in stage_policy.restrictions:
        restriction_tool = str(restriction.get("tool", ""))
        if restriction_tool not in ("", tool_name):
            continue

        required_key = str(restriction.get("require_arg_key", ""))
        if required_key and required_key not in args:
            raise ToolRestrictionError(f"Missing required arg `{required_key}` for tool {tool_name}")

        denied_key = str(restriction.get("deny_arg_key", ""))
        if denied_key and denied_key in args:
            raise ToolRestrictionError(f"Arg `{denied_key}` is denied for tool {tool_name}")

        denied_value = str(restriction.get("deny_arg_value", ""))
        if denied_value and any(str(v) == denied_value for v in args.values()):
            raise ToolRestrictionError(f"Arg value `{denied_value}` is denied for tool {tool_name}")

    trigger_required = tool_call_requires_approval(policy, stage, args)
    if trigger_required and not approved:
        raise ToolApprovalRequired(f"Tool call requires approval due to HITL trigger in stage {stage}")

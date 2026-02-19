from __future__ import annotations

import pytest

from council_os.orchestrator.policies import (
    ToolApprovalRequired,
    ToolPolicyError,
    ToolRestrictionError,
    enforce_tool_policy,
    parse_tool_policy,
    tool_call_requires_approval,
)


def test_tool_policy_blocks_forbidden_tool() -> None:
    policy = parse_tool_policy(
        {
            "stage_policies": [
                {
                    "stage": "draft_pods",
                    "allowlisted_tools": ["mock.echo"],
                    "restrictions": [],
                    "hitl_triggers": [],
                }
            ]
        }
    )

    with pytest.raises(ToolPolicyError):
        enforce_tool_policy(policy, "draft_pods", "mock.counter", {}, approved=True)


def test_tool_policy_requires_approval_on_trigger() -> None:
    policy = parse_tool_policy(
        {
            "stage_policies": [
                {
                    "stage": "draft_pods",
                    "allowlisted_tools": ["mock.counter"],
                    "restrictions": [],
                    "hitl_triggers": ["shell_exec"],
                }
            ]
        }
    )

    with pytest.raises(ToolApprovalRequired):
        enforce_tool_policy(
            policy,
            "draft_pods",
            "mock.counter",
            {"trigger": "shell_exec"},
            approved=False,
        )

    assert tool_call_requires_approval(policy, "draft_pods", {"trigger": "shell_exec"})


def test_tool_policy_restrictions_are_enforced() -> None:
    policy = parse_tool_policy(
        {
            "stage_policies": [
                {
                    "stage": "draft_pods",
                    "allowlisted_tools": ["mock.counter"],
                    "restrictions": [{"tool": "mock.counter", "deny_arg_key": "danger"}],
                    "hitl_triggers": [],
                }
            ]
        }
    )

    with pytest.raises(ToolRestrictionError):
        enforce_tool_policy(
            policy,
            "draft_pods",
            "mock.counter",
            {"danger": "true"},
            approved=True,
        )

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from council_os.agents.providers.base import ProviderResponse
from council_os.agents.providers.mock import MockProvider
from council_os.agents.roles import RoleConfig
from council_os.agents.runtime import AgentRuntime, RuntimeErrorException


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class FlakyProvider:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages, role_config, allowed_tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(text="{bad json", metadata={})
        return ProviderResponse(text='{"value": 3}', metadata={})


class BadProvider:
    def generate(self, messages, role_config, allowed_tools):  # type: ignore[no-untyped-def]
        return ProviderResponse(text='{"wrong": 1}', metadata={})


def test_runtime_repairs_invalid_json(tmp_path: Path) -> None:
    runtime = AgentRuntime(FlakyProvider(), tmp_path / "run", uuid4())
    role = RoleConfig("role", "mock", "mock-v1", 0.0, 100, "p", [])
    result = runtime.invoke_role(role, [{"role": "user", "content": "x"}], OutputModel)
    assert result.retries == 1
    assert isinstance(result.parsed, OutputModel)


def test_runtime_fails_after_retries(tmp_path: Path) -> None:
    runtime = AgentRuntime(BadProvider(), tmp_path / "run", uuid4())
    role = RoleConfig("role", "mock", "mock-v1", 0.0, 100, "p", [])
    with pytest.raises(RuntimeErrorException):
        runtime.invoke_role(role, [{"role": "user", "content": "x"}], OutputModel)


def test_mock_provider_record_replay(tmp_path: Path) -> None:
    cassette = tmp_path / "mock_cassette.json"
    messages = [{"role": "user", "content": "hello"}]
    role_config = {"role_name": "requirements_pod"}

    first = MockProvider(seed=7, cassette_path=cassette).generate(messages, role_config, [])
    second = MockProvider(seed=999, cassette_path=cassette).generate(messages, role_config, [])

    assert first.text == second.text
    assert cassette.exists()

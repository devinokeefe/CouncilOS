from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    metadata: dict[str, object]


class Provider(Protocol):
    def generate(
        self,
        messages: list[dict[str, str]],
        role_config: dict[str, object],
        allowed_tools: list[str],
    ) -> ProviderResponse: ...


class ProviderError(Exception):
    pass


class QuotaExceededError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.detail = detail
        self.stage: str | None = None
        self.role: str | None = None
        self.request_id: str | None = None

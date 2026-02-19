from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from council_os.agents.providers.base import ProviderResponse


@dataclass
class MockProvider:
    seed: int = 0
    cassette_path: Path | None = None

    def _cassette_load(self) -> dict[str, dict[str, Any]]:
        if self.cassette_path is None or not self.cassette_path.exists():
            return {}
        raw = json.loads(self.cassette_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def _cassette_save(self, data: dict[str, dict[str, Any]]) -> None:
        if self.cassette_path is None:
            return
        self.cassette_path.parent.mkdir(parents=True, exist_ok=True)
        self.cassette_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _request_key(
        self,
        messages: list[dict[str, str]],
        role_config: dict[str, object],
        allowed_tools: list[str],
    ) -> str:
        packed = json.dumps(
            {
                "messages": messages,
                "role_config": role_config,
                "allowed_tools": allowed_tools,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(packed.encode("utf-8")).hexdigest()

    def generate(
        self,
        messages: list[dict[str, str]],
        role_config: dict[str, object],
        allowed_tools: list[str],
    ) -> ProviderResponse:
        request_key = self._request_key(messages, role_config, allowed_tools)
        if self.cassette_path is not None:
            cassette = self._cassette_load()
            recorded = cassette.get(request_key)
            if isinstance(recorded, dict):
                text = recorded.get("text", "")
                metadata = recorded.get("metadata", {})
                if isinstance(text, str) and isinstance(metadata, dict):
                    return ProviderResponse(text=text, metadata=metadata)

        role_name = str(role_config.get("role_name", "unknown"))
        payload = {
            "role": role_name,
            "message_count": len(messages),
            "seed": self.seed,
            "tools_allowed": list(allowed_tools),
        }
        response = ProviderResponse(text=json.dumps(payload), metadata={"provider": "mock", "model": "mock-v1"})
        if self.cassette_path is not None:
            cassette = self._cassette_load()
            cassette[request_key] = {"text": response.text, "metadata": response.metadata}
            self._cassette_save(cassette)
        return response

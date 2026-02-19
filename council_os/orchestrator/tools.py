from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID


@dataclass(frozen=True)
class ToolResult:
    output: dict[str, Any]
    side_effect: bool


class Tool(Protocol):
    name: str

    def execute(self, args: dict[str, Any]) -> ToolResult: ...


class ToolNotFoundError(Exception):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def execute(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolNotFoundError(f"Tool not found: {tool_name}")
        return tool.execute(args)


@dataclass
class EchoTool:
    name: str = "mock.echo"

    def execute(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output={"echo": args}, side_effect=False)


@dataclass
class SideEffectCounterTool:
    name: str = "mock.counter"
    count: int = 0

    def execute(self, args: dict[str, Any]) -> ToolResult:
        self.count += 1
        return ToolResult(output={"count": self.count, "args": args}, side_effect=True)


@dataclass(frozen=True)
class LedgerEntry:
    idempotency_key: str
    stage: str
    tool_name: str
    normalized_args: str
    result: dict[str, Any]


def compute_idempotency_key(
    run_id: UUID,
    stage: str,
    tool_name: str,
    normalized_args: str,
    target_resource: str,
) -> str:
    digest = hashlib.sha256(f"{run_id}{stage}{tool_name}{normalized_args}{target_resource}".encode()).hexdigest()
    return digest


class IdempotencyLedger:
    def __init__(self, run_root: Path) -> None:
        self.path = run_root / "tool_ledger.json"
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict[str, dict[str, Any]]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return cast(dict[str, dict[str, Any]], raw)

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, key: str) -> LedgerEntry | None:
        data = self._load()
        item = data.get(key)
        if item is None:
            return None
        return LedgerEntry(
            idempotency_key=key,
            stage=str(item["stage"]),
            tool_name=str(item["tool_name"]),
            normalized_args=str(item["normalized_args"]),
            result=dict(item["result"]),
        )

    def put(self, entry: LedgerEntry) -> None:
        data = self._load()
        data[entry.idempotency_key] = {
            "stage": entry.stage,
            "tool_name": entry.tool_name,
            "normalized_args": entry.normalized_args,
            "result": entry.result,
        }
        self._save(data)


def normalize_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)

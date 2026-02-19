from __future__ import annotations

from datetime import UTC, datetime

from council_os.implementation.schemas import ToolProbeResult


def probe_tool(tool_id: str, tool_version: str, attestation_ref: str, status: str = "ok") -> ToolProbeResult:
    return ToolProbeResult(
        probe_run_id=f"probe-{tool_id}",
        tool_id=tool_id,
        tool_version=tool_version,
        timestamp=datetime.now(UTC),
        request_params={},
        response_schema_hash="",
        latency_ms=1.0,
        status=status,
        quota_headers={},
        attestation_ref=attestation_ref,
    )

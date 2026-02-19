from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"(?i)secret[_-]?key"),
    re.compile(r"(?i)api[_-]?key"),
    re.compile(r"(?i)token"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|PRIVATE) KEY-----"),
]


@dataclass(frozen=True)
class SecretFinding:
    path: str
    pattern: str
    value_snippet: str


class SecretDetectedError(RuntimeError):
    pass


def _scan_value(value: Any, path: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}/{key}" if path else f"/{key}"
            findings.extend(_scan_value(item, next_path))
        return findings
    if isinstance(value, list):
        for idx, item in enumerate(value):
            next_path = f"{path}/{idx}" if path else f"/{idx}"
            findings.extend(_scan_value(item, next_path))
        return findings
    if isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            match = pattern.search(value)
            if match:
                snippet = value[max(0, match.start() - 10) : match.end() + 10]
                findings.append(
                    SecretFinding(
                        path=path or "/",
                        pattern=pattern.pattern,
                        value_snippet=snippet,
                    )
                )
        return findings
    return findings


def scan_for_secrets(value: Any) -> list[SecretFinding]:
    return _scan_value(value, "")


def assert_no_secrets(value: Any) -> None:
    findings = scan_for_secrets(value)
    if findings:
        detail = ", ".join(f"{f.path}:{f.pattern}" for f in findings)
        raise SecretDetectedError(f"Secrets detected in payload: {detail}")


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        redacted = value
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("***REDACTED***", redacted)
        return redacted
    return value


def redact_secrets(value: Any) -> Any:
    return _redact_value(value)

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


MARKDOWN_PATTERNS = [
    re.compile(r"```"),
    re.compile(r"(^|\n)\s{0,3}#{1,6}\s"),
    re.compile(r"(^|\n)\s{0,3}[-*+]\s+"),
    re.compile(r"(^|\n)\s{0,3}\d+\.\s+"),
    re.compile(r"\[[^\]]+\]\([^)]+\)"),
    re.compile(r"(^|\n)\s{0,3}>\s+"),
]


@dataclass(frozen=True)
class MarkdownFinding:
    path: str
    pattern: str
    value_snippet: str


class MarkdownDetectedError(RuntimeError):
    pass


def _scan_value(value: Any, path: str) -> list[MarkdownFinding]:
    findings: list[MarkdownFinding] = []
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
        for pattern in MARKDOWN_PATTERNS:
            match = pattern.search(value)
            if match:
                snippet = value[max(0, match.start() - 10) : match.end() + 10]
                findings.append(
                    MarkdownFinding(
                        path=path or "/",
                        pattern=pattern.pattern,
                        value_snippet=snippet,
                    )
                )
        return findings
    return findings


def scan_for_markdown(value: Any) -> list[MarkdownFinding]:
    return _scan_value(value, "")


def assert_no_markdown(value: Any) -> None:
    findings = scan_for_markdown(value)
    if findings:
        detail = ", ".join(f"{f.path}:{f.pattern}" for f in findings)
        raise MarkdownDetectedError(f"Markdown detected in payload: {detail}")

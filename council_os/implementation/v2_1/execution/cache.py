from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    idempotency_key: str
    job_result_ref: str
    metadata: dict[str, Any]


class IdempotencyCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    def put(self, key: str, job_result_ref: str, metadata: dict[str, Any] | None = None) -> CacheEntry:
        entry = CacheEntry(idempotency_key=key, job_result_ref=job_result_ref, metadata=metadata or {})
        self._entries[key] = entry
        return entry

    def as_dict(self) -> dict[str, CacheEntry]:
        return dict(self._entries)

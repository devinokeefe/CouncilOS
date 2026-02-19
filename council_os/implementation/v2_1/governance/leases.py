from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import Iterable

from council_os.implementation.schemas import LeaseHolder, SurfaceDefinition, SurfaceLeaseEvent, SurfaceRef


@dataclass
class LeaseState:
    lease_id: str
    surface_id: str
    holder: LeaseHolder
    expires_at: datetime
    priority: int


class LeaseManager:
    def __init__(self) -> None:
        self._active: dict[str, LeaseState] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _expired(self, state: LeaseState, now: datetime) -> bool:
        return now >= state.expires_at

    def request_lease(
        self,
        *,
        lease_id: str,
        surface: SurfaceRef,
        holder: LeaseHolder,
        ttl_seconds: int,
        priority: int = 0,
        allow_preempt: bool = False,
    ) -> tuple[SurfaceLeaseEvent, SurfaceLeaseEvent | None]:
        now = self._now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        existing = self._active.get(surface.id)
        if existing is None or self._expired(existing, now):
            if existing is not None and self._expired(existing, now):
                self._active.pop(surface.id, None)
            state = LeaseState(
                lease_id=lease_id,
                surface_id=surface.id,
                holder=holder,
                expires_at=expires_at,
                priority=priority,
            )
            self._active[surface.id] = state
            event = SurfaceLeaseEvent(
                lease_id=lease_id,
                surface=surface,
                holder=holder,
                ttl_seconds=ttl_seconds,
                granted_at=now,
                expires_at=expires_at,
                status="granted",
                conflicts=[],
                decision_reason_code="no_conflict" if existing is None else "expired_previous",
            )
            return event, None

        conflict_id = existing.lease_id
        preempted_event: SurfaceLeaseEvent | None = None
        new_key = (surface.id, holder.task_id or "")
        existing_key = (surface.id, existing.holder.task_id or "")
        should_preempt = False
        if allow_preempt:
            if priority > existing.priority:
                should_preempt = True
                reason = "preempted_lower_priority"
            elif priority == existing.priority and new_key < existing_key:
                should_preempt = True
                reason = "preempted_tie_breaker"
            else:
                reason = "conflict_denied"
        else:
            reason = "conflict_denied"

        if should_preempt:
            preempted_event = SurfaceLeaseEvent(
                lease_id=existing.lease_id,
                surface=surface,
                holder=existing.holder,
                ttl_seconds=max(0, int((existing.expires_at - now).total_seconds())),
                granted_at=now,
                expires_at=existing.expires_at,
                status="preempted",
                conflicts=[lease_id],
                decision_reason_code=reason,
            )
            state = LeaseState(
                lease_id=lease_id,
                surface_id=surface.id,
                holder=holder,
                expires_at=expires_at,
                priority=priority,
            )
            self._active[surface.id] = state
            event = SurfaceLeaseEvent(
                lease_id=lease_id,
                surface=surface,
                holder=holder,
                ttl_seconds=ttl_seconds,
                granted_at=now,
                expires_at=expires_at,
                status="granted",
                conflicts=[conflict_id],
                decision_reason_code=reason,
            )
            return event, preempted_event

        event = SurfaceLeaseEvent(
            lease_id=lease_id,
            surface=surface,
            holder=holder,
            ttl_seconds=ttl_seconds,
            granted_at=now,
            expires_at=expires_at,
            status="denied",
            conflicts=[conflict_id],
            decision_reason_code=reason,
        )
        return event, None

    def release_lease(self, surface_id: str, lease_id: str, holder: LeaseHolder) -> SurfaceLeaseEvent | None:
        now = self._now()
        existing = self._active.get(surface_id)
        if existing is None or existing.lease_id != lease_id:
            return None
        self._active.pop(surface_id, None)
        return SurfaceLeaseEvent(
            lease_id=lease_id,
            surface=SurfaceRef(type="surface", id=surface_id),
            holder=holder,
            ttl_seconds=0,
            granted_at=now,
            expires_at=now,
            status="released",
            conflicts=[],
            decision_reason_code="released",
        )

    def has_active_lease(self, surface_id: str, lease_id: str) -> bool:
        now = self._now()
        existing = self._active.get(surface_id)
        if existing is None:
            return False
        if self._expired(existing, now):
            self._active.pop(surface_id, None)
            return False
        return existing.lease_id == lease_id


class SurfaceResolver:
    def __init__(self, surfaces: Iterable[SurfaceDefinition]) -> None:
        self._surfaces = list(surfaces)

    def resolve_paths(self, paths: Iterable[str]) -> list[str]:
        hits: list[str] = []
        normalized = [p.replace("\\", "/").lstrip("/") for p in paths]
        for surface in self._surfaces:
            for path in normalized:
                if any(fnmatch(path, pattern) for pattern in surface.path_globs):
                    hits.append(surface.surface_id)
                    break
        return sorted(set(hits))

    def resolve_patchset(self, changes: Iterable[str]) -> list[str]:
        return self.resolve_paths(changes)

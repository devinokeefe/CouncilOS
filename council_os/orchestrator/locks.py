from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lock_payload(*, started_at: str | None = None, stage: str | None = None) -> dict[str, object]:
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": started_at or _now(),
        "updated_at": _now(),
    }
    if stage:
        payload["stage"] = stage
    return payload


@contextmanager
def run_lock(run_root: Path, lock_name: str = "run.lock") -> Iterator[Path]:
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / lock_name
    payload = _lock_payload()
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
    except FileExistsError as exc:
        existing = ""
        if lock_path.exists():
            existing = lock_path.read_text(encoding="utf-8")
        raise RuntimeError(
            f"Run lock exists at {lock_path}. Remove it if stale. {existing}".strip()
        ) from exc
    try:
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def update_run_lock(lock_path: Path | None, stage: str | None = None) -> None:
    if lock_path is None or not lock_path.exists():
        return
    started_at = None
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            started_at = str(existing.get("started_at") or "") or None
    except Exception:
        started_at = None
    payload = _lock_payload(started_at=started_at, stage=stage)
    lock_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

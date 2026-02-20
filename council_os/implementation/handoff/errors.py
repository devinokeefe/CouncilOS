from __future__ import annotations

from pathlib import Path


class HandoffError(RuntimeError):
    pass


class HandoffRejected(HandoffError):
    def __init__(self, message: str, rejection_path: Path | None = None) -> None:
        super().__init__(message)
        self.rejection_path = rejection_path

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from council_os.audit.event_log import append_event, new_event, read_events


def test_event_log_append_and_read(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    e1 = new_event(run_id, "intake", {"kind": "orchestrator", "role": "engine"}, "stage_transition")
    e2 = new_event(run_id, "freeze", {"kind": "orchestrator", "role": "engine"}, "stage_transition")
    append_event(run_root, e1)
    append_event(run_root, e2)

    events = list(read_events(run_root))
    assert len(events) == 2
    assert events[0].event_id == e1.event_id
    assert events[1].event_id == e2.event_id


def test_event_log_rejects_invalid_stage_or_type() -> None:
    run_id = uuid4()
    with pytest.raises(ValidationError):
        _ = new_event(run_id, "runtime", {"kind": "orchestrator", "role": "engine"}, "stage_transition")
    with pytest.raises(ValidationError):
        _ = new_event(run_id, "intake", {"kind": "orchestrator", "role": "engine"}, "unknown_type")

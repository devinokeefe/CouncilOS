from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from council_os.audit.event_log import append_event, new_event, read_events, set_redaction_hook


def test_default_redaction_masks_sensitive_fields(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)
    event = new_event(
        run_id,
        stage="intake",
        actor={"kind": "llm", "role": "role"},
        event_type="llm_request",
        payload={"api_key": "secret-value", "nested": {"token": "abc", "ok": "x"}},
    )
    append_event(run_root, event)
    saved = list(read_events(run_root))[0]
    assert saved.payload["api_key"] == "***REDACTED***"
    assert isinstance(saved.payload["nested"], dict)
    assert saved.payload["nested"]["token"] == "***REDACTED***"


def test_custom_redaction_hook(tmp_path: Path) -> None:
    run_id = uuid4()
    run_root = tmp_path / str(run_id)

    def _hook(payload: dict[str, object]) -> dict[str, object]:
        return {"replaced": True}

    set_redaction_hook(_hook)
    try:
        event = new_event(
            run_id,
            stage="intake",
            actor={"kind": "llm", "role": "role"},
            event_type="llm_request",
            payload={"anything": "value"},
        )
        append_event(run_root, event)
    finally:
        set_redaction_hook(None)

    saved = list(read_events(run_root))[0]
    assert saved.payload == {"replaced": True}

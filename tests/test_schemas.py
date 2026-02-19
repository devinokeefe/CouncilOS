from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from council_os.agents.schemas import SCHEMA_VERSION, ArtifactEnvelope


def test_artifact_envelope_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ArtifactEnvelope.model_validate(
            {
                "artifact_type": "project_capsule",
                "artifact_id": "x",
                "schema_version": SCHEMA_VERSION,
                "created_at": datetime.now(UTC),
                "source_run_id": uuid4(),
                "parents": [],
                "payload": {},
                "unexpected": True,
            }
        )

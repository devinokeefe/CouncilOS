from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from council_os.agents.schemas import SCHEMA_VERSION, ArtifactEnvelope
from council_os.artifacts.store import ArtifactExistsError, ArtifactStore, ParentMissingError


def _capsule(run_id):
    return ArtifactEnvelope(
        artifact_type="project_capsule",
        artifact_id="capsule_v1",
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(UTC),
        source_run_id=run_id,
        parents=[],
        payload={
            "brief": "b",
            "problem_statement": "p",
            "goals": ["g"],
            "non_goals": ["n"],
            "constraints": [{"id": "CNS1", "type": "tech", "text": "t"}],
            "assumptions": [
                {
                    "id": "A1",
                    "text": "a",
                    "impact": "low",
                    "confidence": "high",
                    "needs_confirmation": False,
                }
            ],
            "open_questions": [{"id": "Q1", "text": "q", "impact": "low", "blocking": False}],
            "success_metrics": ["s"],
            "stakeholders": [],
            "glossary": [],
        },
    )


def test_immutable_store_and_parent_check(tmp_path: Path) -> None:
    run_id = uuid4()
    store = ArtifactStore(tmp_path, run_id)
    c = _capsule(run_id)
    store.write_artifact(c)

    with pytest.raises(ArtifactExistsError):
        store.write_artifact(c)

    child = ArtifactEnvelope(
        artifact_type="requirements_draft",
        artifact_id="requirements_v1",
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(UTC),
        source_run_id=run_id,
        parents=["missing"],
        payload={"requirements": [{"id": "R1", "priority": "MUST", "text": "x", "rationale": "y"}]},
    )
    with pytest.raises(ParentMissingError):
        store.write_artifact(child)

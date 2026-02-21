from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from council_os.orchestrator.engine import Engine
from council_os.orchestrator.feedback.gates import NeedsUserInput
from council_os.orchestrator.hq_pipeline import HQPipeline
from council_os.utils import storage_root_from_config


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_path: str
    brief_path: str


class InterruptResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: dict[str, Any]


def _resolve_run_root(run_id: str, storage_root: Path) -> Path:
    base = storage_root.parent
    candidates = [
        storage_root / run_id,
        base / "runs" / run_id,
        base / "config" / "runs" / run_id,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return storage_root / run_id


def create_app(storage_root: Path = Path("CouncilOS/runs")) -> FastAPI:
    app = FastAPI(title="Council OS Service API", version="0.1.0")

    @app.post("/runs")
    def create_run(req: RunRequest) -> dict[str, str]:
        config_path = Path(req.config_path)
        config_raw = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_raw)
        resolved_root = storage_root_from_config(config_path)
        if isinstance(config, dict) and config.get("stages") and config.get("models") and config.get("providers"):
            pipeline = HQPipeline(
                storage_root=resolved_root,
                config_path=config_path,
                config_raw=config_raw,
                config=config,
            )
            try:
                result = pipeline.run(brief_path=Path(req.brief_path))
            except NeedsUserInput as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"run_id": str(exc.run_id), "pending_action": str(exc.pending_action_path)},
                ) from exc
        else:
            engine = Engine(storage_root=resolved_root)
            try:
                result = engine.run(brief_path=Path(req.brief_path), config_path=config_path)
            except NeedsUserInput as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"run_id": str(exc.run_id), "pending_action": str(exc.pending_action_path)},
                ) from exc
        return {"run_id": str(result.run_id), "frozen_artifact_id": result.frozen_artifact_id}

    @app.post("/runs/{run_id}/resume")
    def resume_run(run_id: str, checkpoint_id: str | None = None) -> dict[str, str]:
        run_root = _resolve_run_root(run_id, storage_root)
        storage_root_for_run = run_root.parent
        config_snapshot = run_root / "config.snapshot.yml"
        if config_snapshot.exists():
            config_raw = config_snapshot.read_text(encoding="utf-8")
            config = yaml.safe_load(config_raw)
            if isinstance(config, dict) and config.get("stages") and config.get("models") and config.get("providers"):
                pipeline = HQPipeline(
                    storage_root=storage_root_for_run,
                    config_path=config_snapshot,
                    config_raw=config_raw,
                    config=config,
                    run_id=UUID(run_id),
                    run_root=run_root,
                )
                try:
                    result = pipeline.resume(checkpoint_id=checkpoint_id)
                except NeedsUserInput as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"run_id": str(exc.run_id), "pending_action": str(exc.pending_action_path)},
                    ) from exc
            else:
                engine = Engine(storage_root=storage_root_for_run)
                try:
                    result = engine.resume(run_id, checkpoint_id=checkpoint_id)
                except NeedsUserInput as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"run_id": str(exc.run_id), "pending_action": str(exc.pending_action_path)},
                    ) from exc
        else:
            engine = Engine(storage_root=storage_root_for_run)
            try:
                result = engine.resume(run_id, checkpoint_id=checkpoint_id)
            except NeedsUserInput as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"run_id": str(exc.run_id), "pending_action": str(exc.pending_action_path)},
                ) from exc
        return {"run_id": str(result.run_id), "frozen_artifact_id": result.frozen_artifact_id}

    @app.post("/runs/{run_id}/interrupt_response")
    def interrupt_response(run_id: str, req: InterruptResponseRequest) -> dict[str, object]:
        run_root = _resolve_run_root(run_id, storage_root)
        engine = Engine(storage_root=run_root.parent)
        return engine.interrupt_response(run_id, req.response)

    @app.get("/runs/{run_id}/artifacts")
    def artifacts(run_id: str) -> dict[str, list[str]]:
        run_root = _resolve_run_root(run_id, storage_root)
        engine = Engine(storage_root=run_root.parent)
        return {"artifacts": engine.list_artifacts(run_id)}

    @app.get("/runs/{run_id}/events")
    def events(run_id: str, since_event_id: str | None = None) -> dict[str, list[dict[str, object]]]:
        run_root = _resolve_run_root(run_id, storage_root)
        engine = Engine(storage_root=run_root.parent)
        return {"events": engine.events(run_id, since_event_id=since_event_id)}

    @app.get("/runs/{run_id}/status")
    def status(run_id: str) -> dict[str, object]:
        run_root = _resolve_run_root(run_id, storage_root)
        config_snapshot = run_root / "config.snapshot.yml"
        if config_snapshot.exists():
            config_raw = config_snapshot.read_text(encoding="utf-8")
            config = yaml.safe_load(config_raw)
            if isinstance(config, dict) and config.get("stages") and config.get("models") and config.get("providers"):
                pipeline = HQPipeline(
                    storage_root=run_root.parent,
                    config_path=config_snapshot,
                    config_raw=config_raw,
                    config=config,
                    run_id=UUID(run_id),
                    run_root=run_root,
                )
                return pipeline.status()
        engine = Engine(storage_root=run_root.parent)
        return engine.status(run_id)

    return app


app = create_app()

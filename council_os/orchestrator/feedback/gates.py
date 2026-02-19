from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Type

from council_os.orchestrator.feedback import event_log
from council_os.orchestrator.feedback.human_input import HumanInputProvider
from council_os.orchestrator.feedback.schemas import PendingAction, PlanningEvent
from council_os.orchestrator.feedback.store import PlanningArtifactStore


class NeedsUserInput(RuntimeError):
    def __init__(self, run_id: str, pending_action_path: Path) -> None:
        super().__init__("Needs user input")
        self.run_id = run_id
        self.pending_action_path = pending_action_path


@dataclass
class FeedbackResponse:
    payload: dict[str, Any]
    artifact_path: Path


class HumanFeedbackGate:
    def __init__(
        self,
        *,
        run_id: str,
        run_root: Path,
        provider: HumanInputProvider,
    ) -> None:
        self.run_id = run_id
        self.run_root = run_root
        self.provider = provider
        self.store = PlanningArtifactStore(run_root)

    def request_response(
        self,
        *,
        gate_type: str,
        request_artifact: str,
        response_artifact: str,
        response_model: Type[Any],
        round: int,
        instructions: dict[str, str],
        validator: Callable[[Any], tuple[bool, str | None]] | None = None,
    ) -> FeedbackResponse:
        request_path = self.store.path(request_artifact)
        event_log.append_event(
            self.run_root,
            event_log.new_event(
                run_id=self.run_id,
                stage="planning",
                event_type="HUMAN_FEEDBACK_REQUESTED",
                gate_type=gate_type,
                artifact_refs=[request_artifact],
            ),
        )

        payload = self.provider.request_response(
            gate_type=gate_type,
            request_artifact_path=str(request_path),
            expected_response_schema=response_model.__name__,
            round=round,
        )

        if payload is None:
            pending = PendingAction(
                run_id=self.run_id,
                gate_type=gate_type,  # type: ignore[arg-type]
                request_artifact=request_artifact,
                expected_response_artifact=response_artifact,
                round=round,
                instructions=instructions,
            )
            pending_path = self.store.write_json("pending_action.json", pending.model_dump())
            event_log.append_event(
                self.run_root,
                event_log.new_event(
                    run_id=self.run_id,
                    stage="planning",
                    event_type="WAIT_FOR_USER",
                    gate_type=gate_type,
                    artifact_refs=[request_artifact, "pending_action.json"],
                    message="Waiting for user input",
                ),
            )
            raise NeedsUserInput(self.run_id, pending_path)

        response_obj = response_model.model_validate(payload)
        if validator is not None:
            ok, message = validator(response_obj)
            if not ok:
                pending = PendingAction(
                    run_id=self.run_id,
                    gate_type=gate_type,  # type: ignore[arg-type]
                    request_artifact=request_artifact,
                    expected_response_artifact=response_artifact,
                    round=round,
                    instructions=instructions,
                )
                pending_path = self.store.write_json("pending_action.json", pending.model_dump())
                event_log.append_event(
                    self.run_root,
                    event_log.new_event(
                        run_id=self.run_id,
                        stage="planning",
                        event_type="WAIT_FOR_USER",
                        gate_type=gate_type,
                        artifact_refs=[request_artifact, "pending_action.json"],
                        message=message or "Waiting for user input",
                    ),
                )
                raise NeedsUserInput(self.run_id, pending_path)
        response_path = self.store.write_json(response_artifact, response_obj.model_dump())
        event_log.append_event(
            self.run_root,
            event_log.new_event(
                run_id=self.run_id,
                stage="planning",
                event_type="HUMAN_FEEDBACK_RECEIVED",
                gate_type=gate_type,
                artifact_refs=[response_artifact],
            ),
        )
        return FeedbackResponse(payload=response_obj.model_dump(), artifact_path=response_path)

    def log_event(self, event: PlanningEvent) -> None:
        event_log.append_event(self.run_root, event)

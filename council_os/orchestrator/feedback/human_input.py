from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional, Protocol

from council_os.orchestrator.feedback.schemas import (
    ClarificationQuestions,
    ClarificationResponses,
    PlanReviewFeedback,
    PlanReviewPacket,
)


class HumanInputProvider(Protocol):
    def is_interactive(self) -> bool: ...

    def request_response(
        self,
        *,
        gate_type: str,
        request_artifact_path: str,
        expected_response_schema: str,
        round: int,
    ) -> Optional[dict]:
        """
        Return response JSON payload if obtained now, else None (meaning offline/pause).
        """


class CLIProvider:
    def __init__(self) -> None:
        self._interactive = bool(sys.stdin.isatty())

    def is_interactive(self) -> bool:
        return self._interactive

    def request_response(
        self,
        *,
        gate_type: str,
        request_artifact_path: str,
        expected_response_schema: str,
        round: int,
    ) -> Optional[dict]:
        if not self._interactive:
            return None
        if gate_type == "clarify_intent":
            return self._clarify_response(request_artifact_path)
        if gate_type == "plan_review":
            return self._plan_review_response(request_artifact_path)
        return None

    def _clarify_response(self, request_artifact_path: str) -> dict:
        questions = ClarificationQuestions.model_validate_json(
            Path(request_artifact_path).read_text(encoding="utf-8")
        )
        responses = []
        print("Clarify Intent Gate", flush=True)
        for question in questions.questions:
            default = question.default_resolution.value
            prompt = f"{question.text} (default: {default}) "
            answer = input(prompt)
            responses.append({"question_id": question.id, "response": answer})
        payload = ClarificationResponses(schema_version="1.0", responses=responses)
        return payload.model_dump()

    def _plan_review_response(self, request_artifact_path: str) -> dict:
        packet = PlanReviewPacket.model_validate_json(Path(request_artifact_path).read_text(encoding="utf-8"))
        render_path = Path(request_artifact_path).with_name(
            Path(request_artifact_path).name.replace("plan_review_packet", "plan_review_render").replace(".json", ".md")
        )
        print("Plan Review Gate", flush=True)
        if render_path.exists():
            print(f"Review the draft here: {render_path}", flush=True)
        keyword = packet.instructions.approve_keyword or "APPROVE"
        print(f"Type '{keyword}' to approve, or enter feedback text.", flush=True)
        raw = input("> ")
        if raw.strip().upper() == keyword.upper():
            payload = PlanReviewFeedback(
                schema_version="1.0",
                plan_hash=packet.plan_hash,
                action="approve",
                note="Approved via CLI",
            )
            return payload.model_dump()
        payload = PlanReviewFeedback(
            schema_version="1.0",
            plan_hash=packet.plan_hash,
            action="feedback",
            feedback_text=raw,
        )
        return payload.model_dump()


class FileProvider:
    def __init__(self, response_file: Path) -> None:
        self._response_file = response_file

    def is_interactive(self) -> bool:
        return False

    def request_response(
        self,
        *,
        gate_type: str,
        request_artifact_path: str,
        expected_response_schema: str,
        round: int,
    ) -> Optional[dict]:
        if not self._response_file.exists():
            raise FileNotFoundError(f"Response file not found: {self._response_file}")
        return json.loads(self._response_file.read_text(encoding="utf-8"))


class AutoProvider:
    def __init__(self, cli_provider: CLIProvider | None = None) -> None:
        self._cli = cli_provider or CLIProvider()

    def is_interactive(self) -> bool:
        return self._cli.is_interactive()

    def request_response(
        self,
        *,
        gate_type: str,
        request_artifact_path: str,
        expected_response_schema: str,
        round: int,
    ) -> Optional[dict]:
        if self._cli.is_interactive():
            return self._cli.request_response(
                gate_type=gate_type,
                request_artifact_path=request_artifact_path,
                expected_response_schema=expected_response_schema,
                round=round,
            )
        return None

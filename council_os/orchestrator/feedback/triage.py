from __future__ import annotations

from typing import Callable

from pydantic import BaseModel

from council_os.orchestrator.feedback.llm import build_json_schema_messages
from council_os.orchestrator.feedback.schemas import ClarificationQuestions, PrePlanTriage


Invoker = Callable[[list[dict[str, str]], type[BaseModel], str, dict[str, object]], BaseModel]


def run_preplan_triage(
    *,
    invoke_json: Invoker,
    brief: str,
    config_summary: str,
    stage: str,
) -> PrePlanTriage:
    task = (
        "Perform a quick uncertainty scan and list potential avenues for this planning run. "
        "Return PrePlanTriage JSON."
    )
    context = f"Brief:\n{brief}\n\nConfig summary:\n{config_summary}"
    messages, meta = build_json_schema_messages(PrePlanTriage, task=task, context=context)
    return PrePlanTriage.model_validate(
        invoke_json(messages, PrePlanTriage, stage, meta).model_dump()
    )


def build_clarification_questions(
    *,
    invoke_json: Invoker,
    brief: str,
    triage: PrePlanTriage,
    stage: str,
) -> ClarificationQuestions:
    task = (
        "Propose a minimal set of clarification questions with default resolutions. "
        "Use ClarificationQuestions schema."
    )
    context = f"Brief:\n{brief}\n\nTriage:\n{triage.model_dump_json(indent=2)}"
    messages, meta = build_json_schema_messages(ClarificationQuestions, task=task, context=context)
    return ClarificationQuestions.model_validate(
        invoke_json(messages, ClarificationQuestions, stage, meta).model_dump()
    )

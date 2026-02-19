from __future__ import annotations

from council_os.implementation.schemas import ChangeIntentPayload, JobSpecPayload


class SideEffectError(RuntimeError):
    pass


def require_change_intent(job_spec: JobSpecPayload, intents: dict[str, ChangeIntentPayload]) -> ChangeIntentPayload:
    if job_spec.side_effects == "none":
        raise SideEffectError("change intent requested for side_effects=none")
    intent_id = job_spec.change_intent_id
    if not intent_id:
        raise SideEffectError("side-effect job missing change_intent_id")
    intent = intents.get(intent_id)
    if intent is None:
        raise SideEffectError(f"change intent not found: {intent_id}")
    if intent.status != "approved":
        raise SideEffectError(f"change intent not approved: {intent_id}")
    return intent

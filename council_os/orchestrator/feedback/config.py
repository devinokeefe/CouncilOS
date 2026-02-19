from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FeedbackConfig:
    clarify: bool = False
    plan_review: bool = False
    provider: str = "auto"
    max_plan_review_rounds: int = 10
    require_explicit_approval: bool = True

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "FeedbackConfig":
        raw = config.get("feedback", {}) if isinstance(config, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            clarify=bool(raw.get("clarify", False)),
            plan_review=bool(raw.get("plan_review", False)),
            provider=str(raw.get("provider", "auto") or "auto"),
            max_plan_review_rounds=int(raw.get("max_plan_review_rounds", 10) or 10),
            require_explicit_approval=bool(raw.get("require_explicit_approval", True)),
        )

    def apply_overrides(
        self,
        *,
        clarify: bool | None = None,
        plan_review: bool | None = None,
        provider: str | None = None,
        max_plan_review_rounds: int | None = None,
        require_explicit_approval: bool | None = None,
    ) -> "FeedbackConfig":
        if clarify is not None:
            self.clarify = bool(clarify)
        if plan_review is not None:
            self.plan_review = bool(plan_review)
        if provider:
            self.provider = str(provider)
        if max_plan_review_rounds is not None:
            self.max_plan_review_rounds = int(max_plan_review_rounds)
        if require_explicit_approval is not None:
            self.require_explicit_approval = bool(require_explicit_approval)
        return self

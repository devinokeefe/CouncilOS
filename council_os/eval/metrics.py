from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalMetrics:
    total_briefs: int
    successful_runs: int
    invariant_passed_runs: int
    avg_stage_duration_sec: float
    avg_events_per_run: float
    avg_judge_agreement: float
    judge_agreement_drift: float
    blocker_spike_runs: int
    blocker_spike_rate: float

    @property
    def pass_rate(self) -> float:
        if self.total_briefs == 0:
            return 0.0
        return self.successful_runs / self.total_briefs

    @property
    def invariant_pass_rate(self) -> float:
        if self.total_briefs == 0:
            return 0.0
        return self.invariant_passed_runs / self.total_briefs

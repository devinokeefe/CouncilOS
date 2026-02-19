from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from council_os.agents.schemas import RunMetricsPayload
from council_os.eval.metrics import EvalMetrics
from council_os.orchestrator.engine import Engine


def _load_invariants(brief: Path) -> dict[str, object]:
    inv_path = brief.with_suffix(".invariants.json")
    if not inv_path.exists():
        return {}
    raw = json.loads(inv_path.read_text(encoding="utf-8"))
    return cast(dict[str, object], raw)


def _load_artifacts(run_root: Path, artifact_type: str) -> list[dict[str, Any]]:
    folder = run_root / "artifacts" / artifact_type
    if not folder.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        try:
            artifacts.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return artifacts


def _triage_blocker_count(run_root: Path) -> int:
    artifacts = _load_artifacts(run_root, "triage_findings")
    merged = [a for a in artifacts if a.get("payload", {}).get("critic_id") == "MERGED"]
    target = merged if merged else artifacts
    count = 0
    for art in target:
        defects = art.get("payload", {}).get("defects", [])
        if isinstance(defects, list):
            count += sum(1 for d in defects if isinstance(d, dict) and d.get("label") == "blocker")
    return count


def _judge_agreement(run_root: Path) -> float:
    artifacts = _load_artifacts(run_root, "judge_pairwise_result")
    groups: dict[str, list[str]] = {}
    for art in artifacts:
        payload = art.get("payload", {})
        comparison = payload.get("comparison", {})
        if not isinstance(comparison, dict):
            continue
        a = str(comparison.get("a", ""))
        b = str(comparison.get("b", ""))
        if not a or not b:
            continue
        key = "|".join(sorted([a, b]))
        groups.setdefault(key, []).append(str(payload.get("winner", "")))
    if not groups:
        return 0.0
    ratios: list[float] = []
    for winners in groups.values():
        if not winners:
            continue
        counts: dict[str, int] = {}
        for winner in winners:
            counts[winner] = counts.get(winner, 0) + 1
        majority = max(counts.values())
        ratios.append(majority / len(winners))
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios)


def _check_invariants(engine: Engine, run_id: str, invariants: dict[str, object]) -> bool:
    artifacts_raw: Any = invariants.get("required_artifacts", [])
    if isinstance(artifacts_raw, list):
        expected_artifacts = [str(a) for a in artifacts_raw]
    else:
        expected_artifacts = []

    min_events_raw: Any = invariants.get("min_events", 0)
    min_events = int(min_events_raw) if isinstance(min_events_raw, (int, str)) else 0

    artifacts = set(engine.list_artifacts(run_id))
    if any(artifact not in artifacts for artifact in expected_artifacts):
        return False
    if engine.event_count(run_id) < min_events:
        return False
    return True


def run_eval(engine: Engine, config_path: Path, golden_dir: Path) -> EvalMetrics:
    briefs = sorted(golden_dir.glob("*.md"))
    successes = 0
    invariant_passed_runs = 0
    total_avg_stage_duration = 0.0
    total_events = 0
    judge_agreements: list[float] = []
    triage_blockers: list[int] = []
    for brief in briefs:
        result = engine.run(brief, config_path)
        if result.frozen_artifact_id:
            successes += 1

        metrics_artifact = engine.show_artifact(str(result.run_id), "run_metrics_v1")
        metrics = RunMetricsPayload.model_validate(metrics_artifact["payload"])
        total_events += engine.event_count(str(result.run_id))
        if metrics.stage_durations_sec:
            total_avg_stage_duration += sum(metrics.stage_durations_sec.values()) / len(metrics.stage_durations_sec)
        invariants = _load_invariants(brief)
        if _check_invariants(engine, str(result.run_id), invariants):
            invariant_passed_runs += 1
        run_root = engine.storage_root / str(result.run_id)
        judge_agreements.append(_judge_agreement(run_root))
        triage_blockers.append(_triage_blocker_count(run_root))

    total = len(briefs)
    avg_stage_duration = total_avg_stage_duration / total if total else 0.0
    avg_events = total_events / total if total else 0.0
    avg_judge_agreement = sum(judge_agreements) / total if total else 0.0
    judge_agreement_drift = (max(judge_agreements) - min(judge_agreements)) if len(judge_agreements) > 1 else 0.0
    blocker_mean = (sum(triage_blockers) / total) if total else 0.0
    blocker_spike_runs = (
        sum(1 for count in triage_blockers if blocker_mean > 0 and count > blocker_mean * 2) if total else 0
    )
    blocker_spike_rate = blocker_spike_runs / total if total else 0.0
    return EvalMetrics(
        total_briefs=total,
        successful_runs=successes,
        invariant_passed_runs=invariant_passed_runs,
        avg_stage_duration_sec=avg_stage_duration,
        avg_events_per_run=avg_events,
        avg_judge_agreement=avg_judge_agreement,
        judge_agreement_drift=judge_agreement_drift,
        blocker_spike_runs=blocker_spike_runs,
        blocker_spike_rate=blocker_spike_rate,
    )

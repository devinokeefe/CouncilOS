from __future__ import annotations

V2_1_ARTIFACT_TYPES: set[str] = {
    "program_graph",
    "verification_plan",
    "work_graph",
    "work_graph_events",
    "execution_profile_catalog",
    "tool_registry",
    "tool_probe_results",
    "expectation_registry",
    "evidence_index",
    "diff_report",
    "assumption_registry",
    "surface_leases",
    "job_spec",
    "job_result",
    "job_events",
    "change_intent",
    "apply_record",
    "world_state_snapshot",
    "drift_report",
    "diagnosis_report",
    "interface_contracts",
}

__all__ = ["V2_1_ARTIFACT_TYPES"]

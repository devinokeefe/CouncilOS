from __future__ import annotations

from typing import Any

from council_os.implementation.schemas import DiffReportPayload, Expectation, ExpectationRegistryPayload
from council_os.implementation.validators import evaluate_expectation


def evaluate_expectations(
    registry: ExpectationRegistryPayload,
    expectation_ids: list[str],
    output: Any,
    artifacts: dict[str, dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]], str | None]:
    expectation_map = {exp.expectation_id: exp for exp in registry.expectations}
    for exp_id in expectation_ids:
        exp = expectation_map.get(exp_id)
        if exp is None:
            return False, [{"reason": "expectation_missing", "expectation_id": exp_id}], exp_id
        ok, diffs = evaluate_expectation(exp, output, artifacts)
        if not ok:
            return False, diffs, exp_id
    return True, [], None


def build_diff_report(expectation_id: str, run_id: str, summary: str, diffs: list[dict[str, Any]]) -> DiffReportPayload:
    return DiffReportPayload(
        schema_version="2.1.0",
        expectation_id=expectation_id,
        run_id=run_id,
        summary=summary,
        classification="code_bug",
        diffs=diffs,
        evidence=[],
    )

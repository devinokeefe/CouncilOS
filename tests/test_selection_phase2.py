from __future__ import annotations

import pytest

from council_os.orchestrator.selection import deterministic_seed, run_tournament


def test_deterministic_seed_stable() -> None:
    s1 = deterministic_seed("run-1", ["C2", "C1"])
    s2 = deterministic_seed("run-1", ["C1", "C2"])
    assert s1 == s2


def test_tournament_determinism_and_arbitration_path() -> None:
    r1 = run_tournament("run-1", ["C1", "C2", "C3"], judge_panel_size=3)
    r2 = run_tournament("run-1", ["C1", "C2", "C3"], judge_panel_size=3)
    assert r1.winner == r2.winner
    assert len(r1.pairwise_results) == len(r2.pairwise_results)

    with pytest.raises(ValueError):
        run_tournament("run-2", ["C1", "C2"], judge_panel_size=2)

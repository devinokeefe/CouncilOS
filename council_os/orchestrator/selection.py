from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Literal

from council_os.agents.schemas import EvidencePointer, JudgePairwisePayload


def deterministic_seed(run_id: str, candidate_ids: list[str]) -> int:
    joined = run_id + "".join(sorted(candidate_ids))
    return int(hashlib.sha256(joined.encode("utf-8")).hexdigest(), 16)


@dataclass(frozen=True)
class TournamentResult:
    winner: str
    pairwise_results: list[JudgePairwisePayload]
    arbitrator_memo: str | None


def _judge_vote(a: str, b: str, judge_id: str) -> Literal["a", "b"]:
    token = f"{a}|{b}|{judge_id}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return "a" if int(digest, 16) % 2 == 0 else "b"


def _pairwise(a: str, b: str, judge_ids: list[str]) -> tuple[str, list[JudgePairwisePayload], str | None]:
    votes_a = 0
    votes_b = 0
    results: list[JudgePairwisePayload] = []

    for judge_id in judge_ids:
        vote_winner = _judge_vote(a, b, judge_id)
        votes_a += 1 if vote_winner == "a" else 0
        votes_b += 1 if vote_winner == "b" else 0
        results.append(
            JudgePairwisePayload(
                comparison={"a": a, "b": b},
                judge_id=judge_id,
                judge_model="mock/mock-v1",
                winner=vote_winner,
                scores={
                    "feasibility": 8,
                    "testability": 8,
                    "governance": 8,
                    "architecture": 8,
                    "clarity": 8,
                },
                evidence=[
                    EvidencePointer(
                        candidate_id=a,
                        artifact_ref="candidate",
                        artifact_id="DEC1",
                        json_pointer="/plan_package/decision_log/0",
                    )
                ],
                notes="Deterministic mock judge result",
            )
        )

    if votes_a > votes_b:
        return a, results, None
    if votes_b > votes_a:
        return b, results, None

    # Tie: deterministic arbitrator decision.
    arbitrator_token = hashlib.sha256(f"{a}|{b}|arbitrator".encode()).hexdigest()
    winner = a if int(arbitrator_token, 16) % 2 == 0 else b
    memo = f"Arbitrator selected {winner} after tie with evidence-backed parity rule."
    return winner, results, memo


def run_tournament(run_id: str, candidate_ids: list[str], judge_panel_size: int = 3) -> TournamentResult:
    if not candidate_ids:
        raise ValueError("No candidates for tournament")
    if judge_panel_size < 3:
        raise ValueError("Judge panel must be at least 3 for majority voting")
    if len(candidate_ids) == 1:
        winner = candidate_ids[0]
        dummy = JudgePairwisePayload(
            comparison={"a": winner, "b": winner},
            judge_id="J1",
            judge_model="mock/mock-v1",
            winner="a",
            scores={"feasibility": 10, "testability": 10, "governance": 10, "architecture": 10, "clarity": 10},
            evidence=[],
            notes="Single candidate auto-win",
        )
        return TournamentResult(winner=winner, pairwise_results=[dummy], arbitrator_memo=None)

    judges = [f"J{i + 1}" for i in range(judge_panel_size)]
    seed = deterministic_seed(run_id, candidate_ids)
    rng = random.Random(seed)
    seeded = sorted(candidate_ids)
    rng.shuffle(seeded)

    bracket = seeded
    round_results: list[JudgePairwisePayload] = []
    memo: str | None = None

    while len(bracket) > 1:
        next_round: list[str] = []
        index = 0
        while index < len(bracket):
            if index == len(bracket) - 1:
                next_round.append(bracket[index])
                break

            a = bracket[index]
            b = bracket[index + 1]
            winner, pairwise_results, tie_memo = _pairwise(a, b, judges)
            round_results.extend(pairwise_results)
            if tie_memo is not None:
                memo = tie_memo
            next_round.append(winner)
            index += 2

        bracket = next_round

    return TournamentResult(winner=bracket[0], pairwise_results=round_results, arbitrator_memo=memo)

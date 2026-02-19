from __future__ import annotations


def should_continue_loop(repair_count: int, max_repairs: int) -> bool:
    return repair_count < max_repairs

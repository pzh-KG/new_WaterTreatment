"""Pure, bounded wall-clock planning for an existing candidate's source audit.

Reserve every permitted audit/revision turn plus controller overhead. This is
independent of first-extraction budgets and never changes scientific gates.
"""
from __future__ import annotations


POLICY = "reaudit-wall-clock-v1"
MAX_REVISIONS = 2
MAX_MODEL_TURNS = 2 * MAX_REVISIONS + 1
CONTROLLER_ALLOWANCE_SECONDS = 600
MAX_TURN_TIMEOUT_SECONDS = 3600


def reaudit_time_budget(per_turn_timeout_seconds: int) -> dict:
    """Plan at most five configured turns, bounded by 18,600 seconds overall."""
    if (type(per_turn_timeout_seconds) is not int
            or not 1 <= per_turn_timeout_seconds <= MAX_TURN_TIMEOUT_SECONDS):
        raise ValueError("re-audit per-turn timeout must be an integer in [1, 3600]")
    return {
        "policy": POLICY,
        "max_revisions": MAX_REVISIONS,
        "max_model_turns": MAX_MODEL_TURNS,
        "per_turn_timeout_seconds": per_turn_timeout_seconds,
        "controller_allowance_seconds": CONTROLLER_ALLOWANCE_SECONDS,
        "total_timeout_seconds": MAX_MODEL_TURNS * per_turn_timeout_seconds + CONTROLLER_ALLOWANCE_SECONDS,
    }


def validate_reaudit_time_budget(budget: dict) -> dict:
    """Return a detached verified frozen budget, never recalculate from settings."""
    if not isinstance(budget, dict):
        raise ValueError("re-audit time budget must be a frozen policy object")
    expected = reaudit_time_budget(budget.get("per_turn_timeout_seconds"))
    if (budget != expected or any(type(budget.get(key)) is not int
                                  for key in expected if key != "policy")):
        raise ValueError("frozen re-audit time budget differs from its policy and limits")
    return expected

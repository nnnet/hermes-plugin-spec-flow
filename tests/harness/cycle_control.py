"""Cycle-control decisions for the engine's review/lifecycle loops (A3 + A4).

Pure, deterministic, offline — no LLM, no I/O. The engine calls these to decide:

  * A3 ESCALATION — when a node's spec keeps getting REJECTED, do NOT grind the
    same model forever. After the normal rework budget is spent, make ONE final
    attempt with a STRONG model, then stop. escalation_model() is that decision.

  * A4 LIFECYCLE PRUNE — a lifecycle transition that neither changes the node's
    state nor fires a new gate is a NO-OP: emitting it only pollutes the trace
    (~19% of transitions measured). is_noop_transition() flags those for
    suppression, so the run log carries signal, not bookkeeping.

Test: tests/nodes/test_cycle_control.py — both decisions in isolation.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def escalation_model(rejects: int, max_rework: int, strong: str) -> str:
    """A3: the STRONG model to use for a one-shot escalation attempt, or '' when
    not escalating.

    The node has been REJECTED ``rejects`` times; ``max_rework`` is the normal
    same-model rework budget; ``strong`` is the configured escalation model.
    Escalate exactly ONCE — on the first attempt AFTER the budget is spent —
    so a stubborn node gets a stronger opinion instead of an endless grind. No
    strong model configured (or still within budget) -> '' (carry on as today).
    """
    if not strong:
        return ""
    return strong if rejects >= max(1, int(max_rework or 0)) else ""


def is_noop_transition(prev_state: Optional[str], new_state: Optional[str],
                       fired_before: Iterable[str],
                       fired_after: Iterable[str]) -> bool:
    """A4: True when a lifecycle transition should be SUPPRESSED from the trace.

    A transition is a NO-OP when it BOTH leaves the state unchanged AND fires no
    new gate — pure bookkeeping the run log does not need. A transition that
    advances the state OR satisfies a fresh gate is real and must be kept. When
    the engine cannot report a state (the inline engine has no FSM phase),
    ``new_state`` is None and the decision rests on whether a gate fired.
    """
    state_changed = new_state is not None and new_state != prev_state
    gate_fired = bool(set(fired_after) - set(fired_before))
    return not state_changed and not gate_fired


def review_escalate_model(review_policy: Any) -> str:
    """The configured A3 escalation model from a review policy dict, or '' when
    none is set (escalation off — today's behaviour)."""
    if not isinstance(review_policy, dict):
        return ""
    return str(review_policy.get("escalate_model") or "")

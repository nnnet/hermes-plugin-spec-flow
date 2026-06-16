"""A3 escalation + A4 lifecycle-prune decisions — pure, offline."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import cycle_control as cc   # noqa: E402


# ── A3: escalate to a strong model once the rework budget is spent ─────────

def test_escalation_fires_once_after_budget():
    strong = "openrouter/strong:free"
    # within budget (max_rework=2): grind the same model, no escalation
    assert cc.escalation_model(0, 2, strong) == ""
    assert cc.escalation_model(1, 2, strong) == ""
    # budget spent -> escalate to the strong model
    assert cc.escalation_model(2, 2, strong) == strong
    assert cc.escalation_model(3, 2, strong) == strong


def test_no_escalation_without_a_strong_model():
    assert cc.escalation_model(5, 2, "") == ""        # nothing configured -> off


def test_escalation_budget_floor_is_one():
    # a zero/None budget still escalates only after at least one reject
    strong = "s:free"
    assert cc.escalation_model(0, 0, strong) == ""
    assert cc.escalation_model(1, 0, strong) == strong


def test_review_escalate_model_reads_policy():
    assert cc.review_escalate_model({"escalate_model": "s:free"}) == "s:free"
    assert cc.review_escalate_model({}) == ""
    assert cc.review_escalate_model(None) == ""


# ── A4: suppress no-op lifecycle transitions from the trace ───────────────

def test_state_change_is_kept():
    assert cc.is_noop_transition("REQUIREMENTS", "DECOMPOSE", [], []) is False


def test_new_gate_is_kept_even_without_state_change():
    # inline engine reports no FSM phase (None), but a fresh gate fired -> keep
    assert cc.is_noop_transition(None, None, [], ["leaf_check"]) is False
    assert cc.is_noop_transition("X", "X", {"a"}, {"a", "b"}) is False


def test_no_state_change_and_no_new_gate_is_noop():
    # nothing advanced and no gate fired -> pure bookkeeping, suppress it
    assert cc.is_noop_transition("X", "X", {"a"}, {"a"}) is True
    assert cc.is_noop_transition(None, None, [], []) is True


# ── A4 wiring: the node driver suppresses no-op transitions when pruning ───

def test_driver_prunes_noop_transition_when_on():
    import spec_flow_runner as sfr
    import pytest
    if not sfr._NODE_FSM_OK:
        pytest.skip("node FSM unavailable")
    seen = []
    d = sfr._NodeDriver("leaf", fsm_mode=False, prune=True, node="n",
                        on_event=lambda *a: seen.append(a[2]))
    d.go(sfr.EV_CONTRACT)        # fires the contract gate -> a REAL transition
    d.go("noop_event")           # no gate, no state change -> suppressed
    assert sfr.EV_CONTRACT in seen
    assert "noop_event" not in seen, "a no-op transition must be pruned"


def test_driver_keeps_everything_when_pruning_off():
    import spec_flow_runner as sfr
    import pytest
    if not sfr._NODE_FSM_OK:
        pytest.skip("node FSM unavailable")
    seen = []
    d = sfr._NodeDriver("leaf", fsm_mode=False, prune=False, node="n",
                        on_event=lambda *a: seen.append(a[2]))
    d.go("noop_event")           # default off -> nothing is suppressed
    assert "noop_event" in seen


# ── A3 wiring: the engine accessor reads the review policy ────────────────

class _Eng:
    def __init__(self, policy):
        self.review_policy = policy


def test_engine_review_escalation_accessor():
    import spec_flow_runner as sfr
    eng = _Eng({"max_rework": 2, "escalate_model": "openrouter/strong:free"})
    # within budget -> no escalation; budget spent -> the strong model
    assert sfr.Engine._review_escalation(eng, 1) == ""
    assert sfr.Engine._review_escalation(eng, 2) == "openrouter/strong:free"
    # no escalate_model configured -> always ''
    eng2 = _Eng({"max_rework": 2, "escalate_model": ""})
    assert sfr.Engine._review_escalation(eng2, 5) == ""

"""Tests for spec_flow_node_fsm — the standalone node-lifecycle FSM.

Why: prove that ONE spec-flow node's phase order and mandatory gates,
expressed as DATA in ``spec_flow_node_fsm``, drive the correct order and
actually enforce the gates — independently of the imperative runner.

What: leaf happy-path order, branch skipping IMPLEMENT/REVIEW, the three
loops (clarify / critique / drift), the guard blocking DONE on a skipped
gate, and the dependency-free fallback path.

Test: run ``/usr/bin/python3 -m pytest tests/test_node_fsm.py -q``. The
``prefer_engine=False`` cases run with NO ``transitions`` dependency.
"""
from __future__ import annotations

import pytest

import spec_flow_node_fsm as fsm
from spec_flow_node_fsm import (
    NodeLifecycle,
    GateViolation,
    REQUIREMENTS,
    DECOMPOSE,
    CONTRACT,
    IMPLEMENT,
    REVIEW,
    INTEGRATE,
    DONE,
    EV_DECOMPOSE,
    EV_CLARIFY,
    EV_CONTRACT,
    EV_IMPLEMENT,
    EV_DRIFT,
    EV_REVIEW,
    EV_CRITIQUE,
    EV_REVIEW_PASS,
    EV_BRANCH_INTEGRATE,
    EV_DONE,
)


# Run every behavioural test against BOTH backends when the engine is
# available, and always against the fallback. This guarantees the fallback
# path is covered even on an interpreter without ``transitions``.
_BACKENDS = [False]  # fallback always
if fsm._ENGINE_AVAILABLE:
    _BACKENDS.append(True)


@pytest.fixture(params=_BACKENDS, ids=lambda b: "engine" if b else "fallback")
def prefer_engine(request) -> bool:
    return request.param


# ─── leaf happy path: full phase order ────────────────────────────────


def test_leaf_happy_path_in_order(prefer_engine):
    """Why: the canonical leaf must traverse every phase in declared order.
    What: REQUIREMENTS→DECOMPOSE→CONTRACT→IMPLEMENT→REVIEW→INTEGRATE→DONE.
    Test: assert phase after each advance equals the expected next phase."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    assert n.phase == REQUIREMENTS

    steps = [
        (EV_DECOMPOSE, DECOMPOSE),
        (EV_CONTRACT, CONTRACT),
        (EV_IMPLEMENT, IMPLEMENT),
        (EV_REVIEW, REVIEW),
        (EV_REVIEW_PASS, INTEGRATE),
        (EV_DONE, DONE),
    ]
    for event, expected in steps:
        assert n.advance(event) == expected

    assert n.phase == DONE
    assert n.guard() is True


# ─── branch path: skips IMPLEMENT and REVIEW ──────────────────────────


def test_branch_skips_implement_and_review(prefer_engine):
    """Why: a branch delegates impl to children; it must NOT pass through
    IMPLEMENT or REVIEW.
    What: REQUIREMENTS→DECOMPOSE→INTEGRATE→DONE only.
    Test: assert the path and that IMPLEMENT/REVIEW are never visited."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("branch")
    visited = [n.phase]

    assert n.advance(EV_DECOMPOSE) == DECOMPOSE
    visited.append(n.phase)
    assert n.advance(EV_BRANCH_INTEGRATE) == INTEGRATE
    visited.append(n.phase)
    assert n.advance(EV_DONE) == DONE
    visited.append(n.phase)

    assert IMPLEMENT not in visited
    assert REVIEW not in visited
    assert visited == [REQUIREMENTS, DECOMPOSE, INTEGRATE, DONE]
    assert n.guard() is True


def test_branch_cannot_enter_implement(prefer_engine):
    """Why: enforce that a branch has no leaf impl events available.
    What: firing EV_IMPLEMENT from DECOMPOSE is an illegal transition.
    Test: GateViolation raised (CONTRACT/IMPLEMENT not on branch path here)."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("branch")
    n.advance(EV_DECOMPOSE)
    with pytest.raises(GateViolation):
        n.advance(EV_IMPLEMENT)


# ─── clarify loop: blocks past DECOMPOSE until resolved ───────────────


def test_clarify_loop_blocks_until_resolved(prefer_engine):
    """Why: an open decision raised in DECOMPOSE must block forward progress
    (the kanban_block → clarify → unblock loop in the runner).
    What: with an open decision, leaving DECOMPOSE raises; after resolve it
    proceeds. The clarify self-loop is legal while blocked.
    Test: assert raise before resolve, success after."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    n.advance(EV_DECOMPOSE)
    n.open_decision()

    # self-loop while the decision is open is allowed and stays in DECOMPOSE
    assert n.advance(EV_CLARIFY) == DECOMPOSE

    with pytest.raises(GateViolation):
        n.advance(EV_CONTRACT)
    assert n.phase == DECOMPOSE  # did not move

    n.resolve_decision()
    assert n.advance(EV_CONTRACT) == CONTRACT


# ─── critique loop: REVIEW → IMPLEMENT on fail, forward on pass ───────


def test_critique_loop_back_then_forward(prefer_engine):
    """Why: a failing review must send the node back to IMPLEMENT, and a
    later pass must move it forward to INTEGRATE.
    What: REVIEW --critique--> IMPLEMENT --to_review--> REVIEW --pass--> INTEGRATE.
    Test: assert the back-and-forward phases."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    for ev in (EV_DECOMPOSE, EV_CONTRACT, EV_IMPLEMENT, EV_REVIEW):
        n.advance(ev)
    assert n.phase == REVIEW

    # fail → back to IMPLEMENT
    assert n.advance(EV_CRITIQUE) == IMPLEMENT
    # re-run review → pass → forward
    assert n.advance(EV_REVIEW) == REVIEW
    assert n.advance(EV_REVIEW_PASS) == INTEGRATE


# ─── drift loop: IMPLEMENT self-loop (respec / codefix re-check) ──────


def test_drift_loop_representable(prefer_engine):
    """Why: contract drift triggers a respec/codefix re-check that stays in
    IMPLEMENT before review — the loop must be representable.
    What: EV_DRIFT is a legal IMPLEMENT self-loop, repeatable.
    Test: fire drift twice, phase stays IMPLEMENT, then proceed to REVIEW."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    for ev in (EV_DECOMPOSE, EV_CONTRACT, EV_IMPLEMENT):
        n.advance(ev)
    assert n.phase == IMPLEMENT

    assert n.advance(EV_DRIFT) == IMPLEMENT
    assert n.advance(EV_DRIFT) == IMPLEMENT
    assert n.advance(EV_REVIEW) == REVIEW


# ─── guard: refuses DONE when a mandatory gate was skipped ────────────


def test_guard_blocks_done_when_gate_skipped(prefer_engine):
    """Why: the guard is the whole point — reaching DONE without the gates
    having fired must be impossible.
    What: drive a branch to INTEGRATE, then clear the recorded gate ledger
    to simulate a skipped integrate gate, and assert EV_DONE raises.
    Test: GateViolation raised; the missing gate is named."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("branch")
    n.advance(EV_DECOMPOSE)
    n.advance(EV_BRANCH_INTEGRATE)
    assert n.phase == INTEGRATE
    assert n.guard() is True  # integrate gate recorded

    # Simulate a skipped gate: wipe the ledger as if integrate never fired.
    n._gates.fired.clear()
    assert n.guard() is False
    with pytest.raises(GateViolation) as exc:
        n.advance(EV_DONE)
    assert "integrate" in str(exc.value)
    assert n.phase == INTEGRATE  # never reached DONE


def test_leaf_guard_requires_all_three_gates(prefer_engine):
    """Why: a leaf has three mandatory gates; the guard must demand all.
    What: at INTEGRATE the guard is True; dropping any one makes it False.
    Test: clear each gate in turn and assert guard() flips to False."""
    n = NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    for ev in (EV_DECOMPOSE, EV_CONTRACT, EV_IMPLEMENT, EV_REVIEW, EV_REVIEW_PASS):
        n.advance(ev)
    assert n.guard() is True

    for gate in ("contract_check", "review_pass", "verification"):
        saved = set(n._gates.fired)
        n._gates.fired.discard(gate)
        assert n.guard() is False, f"guard should fail without {gate}"
        n._gates.fired = saved
    assert n.guard() is True


# ─── fallback path: runs WITHOUT the transitions dependency ───────────


def test_fallback_runs_without_transitions():
    """Why: the module must import and drive the FSM even when neither the
    engine nor ``transitions`` is available.
    What: force the fallback backend and run a full leaf path.
    Test: engine reports 'fallback', the path completes, guard passes."""
    n = NodeLifecycle(prefer_engine=False).start("leaf")
    assert n.engine == "fallback"
    for ev in (EV_DECOMPOSE, EV_CONTRACT, EV_IMPLEMENT, EV_REVIEW, EV_REVIEW_PASS, EV_DONE):
        n.advance(ev)
    assert n.phase == DONE
    assert n.guard() is True


def test_module_imports_and_exposes_api():
    """Why: a smoke test that the module surface is intact regardless of
    which backend is active.
    What: key names exist and a fresh lifecycle starts at REQUIREMENTS.
    Test: attribute presence + initial phase."""
    assert hasattr(fsm, "NodeLifecycle")
    assert hasattr(fsm, "GateViolation")
    n = NodeLifecycle(prefer_engine=False)
    n.start("leaf")
    assert n.phase == REQUIREMENTS
    assert fsm.GATES_LEAF == ("contract_check", "review_pass", "verification")
    assert fsm.GATES_BRANCH == ("integrate",)


def test_start_rejects_bad_kind():
    """Why: kind drives which gates are mandatory; an invalid kind is a bug.
    What: start('widget') raises ValueError.
    Test: ValueError raised."""
    with pytest.raises(ValueError):
        NodeLifecycle(prefer_engine=False).start("widget")


def test_advance_before_start_raises():
    """Why: advancing before start() is a misuse that must fail loudly.
    What: advance() without start() raises RuntimeError.
    Test: RuntimeError raised."""
    with pytest.raises(RuntimeError):
        NodeLifecycle(prefer_engine=False).advance(EV_DECOMPOSE)

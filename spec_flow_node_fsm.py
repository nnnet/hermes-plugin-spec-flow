"""spec_flow_node_fsm — node lifecycle as an explicit state machine.

Why: today the phase order (requirements → decompose → contract → implement
→ review → integrate) and the mandatory gates (contract_check, review PASS,
verification for a leaf; integrate for a branch) live *implicitly* inside the
imperative ``_visit`` / ``_leaf_pipeline`` / ``_branch_pipeline`` code of
``spec_flow_runner.py``. This module re-expresses ONE node's lifecycle as
DATA — a single phase + transition declaration — so the order and the gates
are inspectable and testable in isolation. This is the roadmap's
``node_engine='fsm'`` option. It is intentionally standalone: nothing here is
wired into the runner yet.

What: ``NodeLifecycle`` drives a leaf or a branch through its phases via
``advance(event)``, exposes the current ``phase``, records which mandatory
gates fired, and a ``guard`` REFUSES to reach DONE until every gate for the
node kind has fired. Loops are first-class events: ``clarify`` (block on an
open decision until resolved), ``critique`` (REVIEW → IMPLEMENT on fail),
``drift`` (IMPLEMENT respec/codefix re-check).

Engine choice is graceful: if the sibling ``engines/workflow-engine``
package and its ``transitions`` dependency are importable, the lifecycle is
driven by ``WorkflowMachine`` (pytransitions). Otherwise it falls back to a
tiny self-contained transition table so the module still imports and runs
WITHOUT the dependency — mirroring the guarded optional-import style used at
the top of ``spec_flow_tools.py`` (the ``tools.registry`` fallback).

Test: ``tests/test_node_fsm.py`` drives a leaf happy-path through every phase
in order, drives a branch (skipping IMPLEMENT/REVIEW), exercises the clarify
/ critique / drift loops, and asserts the guard blocks DONE when a gate was
skipped. The fallback path is covered explicitly so the suite passes even
without ``transitions`` installed.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ─── Optional engine import (graceful, like tools.registry fallback) ───
#
# Resolve the path to the sibling generic state-machine engine RELATIVELY
# from this file. Both the canonical checkout (engines/ next to the plugin)
# and a git worktree (engines/ several levels up) are handled by walking up
# the parents until ``engines/workflow-engine`` is found. If anything is
# missing — the package, or its ``transitions`` dependency — we silently
# drop to the self-contained fallback table below. The module ALWAYS
# imports and runs.

_ENGINE_AVAILABLE = False
WorkflowConfig = None  # type: ignore[assignment]
WorkflowState = None  # type: ignore[assignment]
WorkflowMachine = None  # type: ignore[assignment]
PhaseSpec = None  # type: ignore[assignment]
TransitionSpec = None  # type: ignore[assignment]


def _find_engine_dir() -> Optional[Path]:
    """Why: support both checkout and worktree layouts without absolute paths.
    What: walk up from this file's dir looking for ``engines/workflow-engine``.
    Test: from the worktree it resolves to the repo-level engines dir.
    """
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        cand = base / "engines" / "workflow-engine"
        if (cand / "core" / "machine.py").is_file():
            return cand
    return None


_engine_dir = _find_engine_dir()
if _engine_dir is not None:
    if str(_engine_dir) not in sys.path:
        sys.path.insert(0, str(_engine_dir))
    try:
        from core.state import (  # type: ignore[no-redef]
            WorkflowConfig,
            WorkflowState,
            PhaseSpec,
            TransitionSpec,
        )
        from core.machine import WorkflowMachine  # type: ignore[no-redef]

        # WorkflowMachine raises ImportError at construction if `transitions`
        # is missing; probe the dependency now so we pick the fallback early.
        import transitions  # noqa: F401

        _ENGINE_AVAILABLE = True
    except Exception:  # pragma: no cover — exercised only when deps absent
        _ENGINE_AVAILABLE = False


# ─── Phase + event vocabulary (the DATA this module is all about) ──────

REQUIREMENTS = "REQUIREMENTS"
DECOMPOSE = "DECOMPOSE"
CONTRACT = "CONTRACT"
IMPLEMENT = "IMPLEMENT"
REVIEW = "REVIEW"
INTEGRATE = "INTEGRATE"
DONE = "DONE"

PHASES = [REQUIREMENTS, DECOMPOSE, CONTRACT, IMPLEMENT, REVIEW, INTEGRATE, DONE]

# Events fired via advance(). Linear-forward events plus the three loops.
EV_DECOMPOSE = "to_decompose"       # REQUIREMENTS → DECOMPOSE
EV_CLARIFY = "clarify"              # DECOMPOSE → DECOMPOSE (self loop, blocks)
EV_RESOLVE = "resolve"             # mark the open decision resolved (no move)
EV_CONTRACT = "to_contract"         # DECOMPOSE → CONTRACT
EV_IMPLEMENT = "to_implement"       # CONTRACT → IMPLEMENT (leaf)
EV_DRIFT = "drift"                  # IMPLEMENT → IMPLEMENT (respec/codefix loop)
EV_REVIEW = "to_review"             # IMPLEMENT → REVIEW
EV_CRITIQUE = "critique"            # REVIEW → IMPLEMENT (fail)
EV_REVIEW_PASS = "review_pass"      # REVIEW → INTEGRATE
EV_BRANCH_INTEGRATE = "to_integrate"  # DECOMPOSE → INTEGRATE (branch skips impl)
EV_DONE = "to_done"                 # INTEGRATE → DONE

# Mandatory gates that MUST fire before DONE, keyed by node kind.
GATES_LEAF = ("contract_check", "review_pass", "verification")
GATES_BRANCH = ("integrate",)


class GateViolation(Exception):
    """Why: make a skipped mandatory gate a hard, catchable failure rather
    than a silent bad state — the whole point of modelling gates as data.
    What: raised by ``advance(EV_DONE)`` / ``guard`` when a required gate
    for the node kind has not fired.
    Test: negative test asserts this is raised when a gate is skipped.
    """


# ─── Transition table (shared by both engines) ────────────────────────
#
# Each tuple is (event, source(s), dest). A leaf walks the full impl path;
# a branch jumps DECOMPOSE → INTEGRATE. CONTRACT is on the path for both
# the leaf and a branch that froze an L2 contract (the runner only emits a
# branch contract when node.get('contract') is set), so we model CONTRACT
# as reachable from DECOMPOSE for the leaf and let the branch skip straight
# to INTEGRATE via its own event.
_TABLE: list[tuple[str, object, str]] = [
    (EV_DECOMPOSE, REQUIREMENTS, DECOMPOSE),
    (EV_CLARIFY, DECOMPOSE, DECOMPOSE),
    (EV_CONTRACT, DECOMPOSE, CONTRACT),
    (EV_IMPLEMENT, CONTRACT, IMPLEMENT),
    (EV_DRIFT, IMPLEMENT, IMPLEMENT),
    (EV_REVIEW, IMPLEMENT, REVIEW),
    (EV_CRITIQUE, REVIEW, IMPLEMENT),
    (EV_REVIEW_PASS, REVIEW, INTEGRATE),
    (EV_BRANCH_INTEGRATE, DECOMPOSE, INTEGRATE),
    (EV_DONE, INTEGRATE, DONE),
]


@dataclass
class _GateLedger:
    """Why: the guard needs to know which gates actually fired, independent
    of the engine driving the phases.
    What: a set of gate names recorded as events fire.
    Test: leaf happy-path fills all three; a skipped path leaves one empty.
    """
    fired: set = field(default_factory=set)

    def record(self, gate: str) -> None:
        self.fired.add(gate)

    def has(self, gate: str) -> bool:
        return gate in self.fired


# ─── Tiny fallback machine (no pytransitions) ─────────────────────────


class _FallbackMachine:
    """Why: keep the module runnable when ``transitions`` / the engine are
    absent — same guard semantics, no third-party dependency.
    What: a dict-driven transition table keyed by (source, event) → dest.
    Test: the no-dependency test drives a full leaf path through this.
    """

    def __init__(self, initial: str):
        self._state = initial
        self._map: dict[tuple[str, str], str] = {}
        for event, src, dest in _TABLE:
            sources = src if isinstance(src, list) else [src]
            for s in sources:
                self._map[(s, event)] = dest

    @property
    def state(self) -> str:
        return self._state

    def fire(self, event: str) -> bool:
        key = (self._state, event)
        if key in self._map:
            self._state = self._map[key]
            return True
        return False


# ─── Engine-backed machine (pytransitions via WorkflowMachine) ────────


def _build_engine_machine(initial: str):
    """Why: reuse the project's own generic state-machine engine so the
    phase graph is driven by the same pytransitions wrapper the runner will
    eventually adopt.
    What: build a minimal ``WorkflowConfig`` + ``WorkflowState`` and wrap in
    ``WorkflowMachine``; routing is event-driven (we fire triggers directly),
    so ``decide_fn`` is a no-op returning the current phase.
    Test: engine-path tests assert the same phase order as the fallback.
    """
    transitions_spec = [
        TransitionSpec(trigger=event, source=src, dest=dest)
        for event, src, dest in _TABLE
    ]
    # Distinct triggers can share names across rows (e.g. self-loops); keep
    # each as its own TransitionSpec — pytransitions allows duplicate
    # trigger names with different source/dest.
    phases = [PhaseSpec(name=p, prompt_builder=lambda *a, **k: "") for p in PHASES]
    config = WorkflowConfig(
        name="spec_flow_node",
        slots_cls=_NoopSlots,
        phases=phases,
        transitions=transitions_spec,
        initial_phase=initial,
        decide_fn=lambda wstate, user, prev, cur: cur,
    )
    wstate = WorkflowState(
        session_id="node-fsm",
        workflow_name="spec_flow_node",
        phase=initial,
        slots=_NoopSlots(),
    )
    return WorkflowMachine(config, wstate)


if _ENGINE_AVAILABLE:
    from core.state import SlotsBase  # type: ignore[no-redef]

    @dataclass
    class _NoopSlots(SlotsBase):
        """Why: WorkflowConfig requires a SlotsBase subclass; this node FSM
        carries no slots (it models control flow, not data extraction).
        What: an empty slot container.
        Test: implicitly exercised whenever the engine path builds a machine.
        """

        def required_keys(self) -> list[str]:
            return []
else:  # pragma: no cover — only when engine/deps absent
    _NoopSlots = None  # type: ignore[assignment]


# ─── Public API ───────────────────────────────────────────────────────


class NodeLifecycle:
    """Why: a single, testable object that proves the spec-flow node phase
    order and mandatory gates can be expressed as DATA and enforced.
    What: ``start(kind)`` picks leaf vs branch, ``advance(event)`` moves the
    machine and records gates, ``phase`` reports the state, and the guard
    refuses DONE until every gate for the kind fired.
    Test: see ``tests/test_node_fsm.py`` for happy/branch/loop/guard cases.
    """

    def __init__(self, *, prefer_engine: bool = True):
        # prefer_engine=False forces the fallback even when the engine is
        # importable — used by the no-dependency test to exercise the table.
        self._use_engine = prefer_engine and _ENGINE_AVAILABLE
        self.kind: Optional[str] = None
        self._open_decision = False
        self._machine = None
        self._gates = _GateLedger()

    # ─── lifecycle control ────────────────────────────────────────

    @property
    def engine(self) -> str:
        """Which backend is live: 'workflow-engine' or 'fallback'."""
        return "workflow-engine" if self._use_engine else "fallback"

    @property
    def phase(self) -> str:
        if self._machine is None:
            return REQUIREMENTS
        return self._machine.state

    @property
    def gates_fired(self) -> set:
        return set(self._gates.fired)

    def start(self, kind: str) -> "NodeLifecycle":
        """Why: a node is either a leaf (full impl path) or a branch
        (decompose → children → integrate); the kind selects which gates
        are mandatory and which phases apply.
        What: initialise the machine at REQUIREMENTS for the given kind.
        Test: start('leaf') / start('branch') then walk the path.
        """
        if kind not in ("leaf", "branch"):
            raise ValueError(f"kind must be 'leaf' or 'branch', got {kind!r}")
        self.kind = kind
        self._open_decision = False
        self._gates = _GateLedger()
        if self._use_engine:
            self._machine = _build_engine_machine(REQUIREMENTS)
        else:
            self._machine = _FallbackMachine(REQUIREMENTS)
        return self

    def open_decision(self) -> None:
        """Why: model the clarify loop — an open decision raised during
        DECOMPOSE blocks forward progress until resolved.
        What: set a flag the guard on EV_CONTRACT / EV_BRANCH_INTEGRATE checks.
        Test: clarify-loop test sets this and asserts advance past DECOMPOSE
        is refused until resolve_decision() is called.
        """
        self._open_decision = True

    def resolve_decision(self) -> None:
        """Why/What: clear the open-decision block (the clarify resolution).
        Test: after this, advancing past DECOMPOSE succeeds."""
        self._open_decision = False

    def advance(self, event: str) -> str:
        """Why: single entry point for every state move, so gate recording
        and the clarify/DONE guards live in one place.
        What: validate the move, record the gate it represents, fire it on
        the active machine, and return the new phase. Blocks leaving
        DECOMPOSE while an open decision stands; refuses EV_DONE until all
        mandatory gates for the kind fired.
        Test: every test path calls advance() and asserts phase / raises.
        """
        if self._machine is None:
            raise RuntimeError("call start(kind) before advance()")

        # clarify guard: cannot leave DECOMPOSE toward CONTRACT/INTEGRATE
        # while an open decision is unresolved.
        if (
            self.phase == DECOMPOSE
            and event in (EV_CONTRACT, EV_BRANCH_INTEGRATE)
            and self._open_decision
        ):
            raise GateViolation(
                "open decision unresolved — clarify loop must close before "
                "leaving DECOMPOSE"
            )

        # DONE guard: every mandatory gate for the node kind must have fired.
        if event == EV_DONE:
            self._assert_gates_complete()

        moved = self._fire(event)
        if not moved:
            raise GateViolation(
                f"illegal transition: event {event!r} not allowed from "
                f"phase {self.phase!r} (kind={self.kind})"
            )

        # Record the gate this event represents AFTER a successful move.
        self._record_gate_for(event)
        return self.phase

    # ─── guard ────────────────────────────────────────────────────

    def guard(self) -> bool:
        """Why: expose the DONE-readiness check independently so callers can
        test gate completeness without firing EV_DONE.
        What: True iff every mandatory gate for the node kind has fired.
        Test: returns False mid-path, True only once all gates recorded.
        """
        required = GATES_BRANCH if self.kind == "branch" else GATES_LEAF
        return all(self._gates.has(g) for g in required)

    def _assert_gates_complete(self) -> None:
        required = GATES_BRANCH if self.kind == "branch" else GATES_LEAF
        missing = [g for g in required if not self._gates.has(g)]
        if missing:
            raise GateViolation(
                f"cannot reach DONE: mandatory gate(s) skipped for "
                f"{self.kind}: {', '.join(missing)}"
            )

    # ─── internals ────────────────────────────────────────────────

    def _fire(self, event: str) -> bool:
        if self._use_engine:
            # WorkflowMachine exposes each trigger as a bound method; call it
            # and report whether the state actually changed (or self-looped).
            before = self._machine.state
            trigger_fn = getattr(self._machine, event, None)
            if trigger_fn is None:
                return False
            try:
                trigger_fn()
            except Exception:
                return False
            after = self._machine.state
            # Self-loops (clarify, drift) keep the same state but are legal:
            # treat a self-loop as a successful fire by checking the table.
            if after != before:
                return True
            return self._is_self_loop(before, event)
        return self._machine.fire(event)

    @staticmethod
    def _is_self_loop(state: str, event: str) -> bool:
        for ev, src, dest in _TABLE:
            sources = src if isinstance(src, list) else [src]
            if ev == event and state in sources and dest == state:
                return True
        return False

    def _record_gate_for(self, event: str) -> None:
        """Map a fired event onto the mandatory gate it satisfies, if any.

        Why: the guard works off gate names, not events; one event can
        stand for one gate. Loop events (clarify/critique/drift) satisfy no
        terminal gate. CONTRACT entry = contract_check; review pass =
        review_pass + verification; branch integrate = integrate.
        """
        if event == EV_CONTRACT:
            self._gates.record("contract_check")
        elif event == EV_REVIEW_PASS:
            # Reaching INTEGRATE on a passing review implies both the review
            # gate and the verification-before-completion gate fired.
            self._gates.record("review_pass")
            self._gates.record("verification")
        elif event == EV_BRANCH_INTEGRATE:
            self._gates.record("integrate")


__all__ = [
    "NodeLifecycle",
    "GateViolation",
    "PHASES",
    "REQUIREMENTS",
    "DECOMPOSE",
    "CONTRACT",
    "IMPLEMENT",
    "REVIEW",
    "INTEGRATE",
    "DONE",
    "EV_DECOMPOSE",
    "EV_CLARIFY",
    "EV_CONTRACT",
    "EV_IMPLEMENT",
    "EV_DRIFT",
    "EV_REVIEW",
    "EV_CRITIQUE",
    "EV_REVIEW_PASS",
    "EV_BRANCH_INTEGRATE",
    "EV_DONE",
    "GATES_LEAF",
    "GATES_BRANCH",
]

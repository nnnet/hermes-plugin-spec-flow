"""Battle test — atomicity-first leaf_check with a threshold guardrail (Stage 4).

The decomposer makes the PRIMARY call (`atomic`); the hard thresholds are the
GUARDRAIL that overrides in both directions — pruning an over-decomposed node to
a leaf and forcing an under-decomposed one to branch. No third-party library
does this reconciliation drop-in (HTN libs need hand-written method libraries;
agent frameworks leave the atomicity test to your prompt) — it is ~30 lines of
plugin code, the core IP. Back-compatible: omit `atomic` ⇒ pure thresholds.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

ATOMIC = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
          "open_decisions": 0, "single_concern": True, "testable_criteria": True}
BIG = {"modules": 3, "tasks": 9, "interfaces": 3, "estimated_loc": 500,
       "open_decisions": 0, "single_concern": False, "testable_criteria": True}


def _check(plugin, metrics, **extra):
    return json.loads(plugin.tools._handle_leaf_check({**metrics, **extra}))


# ─── pure thresholds (back-compat: no `atomic`) ───────────────────────


def test_no_atomic_claim_is_pure_thresholds(plugin):
    assert _check(plugin, ATOMIC)["verdict"] == "leaf"
    assert _check(plugin, BIG)["verdict"] == "branch"
    assert _check(plugin, ATOMIC)["basis"] == "thresholds"
    assert _check(plugin, ATOMIC)["atomic_claim"] is None


# ─── agreement ────────────────────────────────────────────────────────


def test_claim_and_thresholds_agree_leaf(plugin):
    r = _check(plugin, ATOMIC, atomic=True)
    assert r["verdict"] == "leaf" and r["mismatch"] is None
    assert "agree" in r["basis"]


def test_claim_and_thresholds_agree_branch(plugin):
    r = _check(plugin, BIG, atomic=False)
    assert r["verdict"] == "branch" and r["mismatch"] is None


# ─── the guardrail overrides in BOTH directions ──────────────────────


def test_over_decomposition_is_pruned_to_leaf(plugin):
    # decomposer wanted to split, but every threshold says it is already atomic
    r = _check(plugin, ATOMIC, atomic=False)
    assert r["verdict"] == "leaf"
    assert r["mismatch"] == "over-decomposition"
    assert "already atomic" in r["basis"]


def test_under_decomposition_is_forced_to_branch(plugin):
    # decomposer claimed atomic, but it blows the size thresholds
    r = _check(plugin, BIG, atomic=True)
    assert r["verdict"] == "branch"
    assert r["mismatch"] == "under-decomposition"
    assert "too big" in r["basis"]


# ─── end-to-end through the engine ────────────────────────────────────

_BRANCH_METRICS = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
                   "open_decisions": 0, "single_concern": False, "testable_criteria": True}


def _over_decomposer():
    """A decomposer that OVER-splits: it proposes children for a node that is
    already atomic by metrics — the engine guardrail must prune them."""
    def decompose(ctx):
        if ctx["depth"] == 0:
            return {"atomic": False, "metrics": _BRANCH_METRICS,
                    "children": [{"id": "real_leaf", "title": "Real leaf"}]}
        # the child is tiny, yet the decomposer still claims it splits further
        return {"atomic": False, "metrics": ATOMIC,
                "children": [{"id": "spurious", "title": "Spurious sub-split"}]}
    return decompose


GOAL = {
    "name": "over-decomp",
    "goal": "a service the decomposer over-splits",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}


def test_engine_prunes_over_decomposition(plugin, tmp_path):
    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools, agents={"decomposer": _over_decomposer()})
    # the spurious deep split was pruned: 'real_leaf' is a leaf, no 'spurious'
    assert "real_leaf" in res.tasks
    assert "spurious" not in res.tasks
    # the guardrail recorded the prune
    assert any(l["type"] == "decomposition-guardrail" and l["detail"] == "over-decomposition"
               for l in res.loops)
    # the realized tree is honest — real_leaf carries no children
    real = None
    for c in res.project["tree"].get("children", []):
        if c["id"] == "real_leaf":
            real = c
    assert real is not None and not real.get("children")


def test_engine_infers_atomicity_from_proposed_children(plugin, tmp_path):
    # a decomposer that omits `atomic` but proposes children for a tiny node:
    # the engine infers claim=branch and the guardrail still prunes it
    def decompose(ctx):
        if ctx["depth"] == 0:
            return {"metrics": _BRANCH_METRICS,
                    "children": [{"id": "small", "title": "Small"}]}
        return {"metrics": ATOMIC, "children": [{"id": "junk", "title": "Junk"}]}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk2"),
                          tools=plugin.tools, agents={"decomposer": decompose})
    assert "small" in res.tasks and "junk" not in res.tasks


# ─── 427: one delta per leaf ──────────────────────────────────────────


def test_deltas_absent_is_inert(plugin):
    # a node that does not report `deltas` keeps pure-threshold behaviour
    assert _check(plugin, ATOMIC)["verdict"] == "leaf"


def test_one_delta_stays_leaf(plugin):
    assert _check(plugin, ATOMIC, deltas=1)["verdict"] == "leaf"


def test_two_deltas_force_branch(plugin):
    res = _check(plugin, ATOMIC, deltas=2)
    assert res["verdict"] == "branch"
    assert any("one delta per leaf" in r for r in res["reasons"])


def test_two_deltas_override_atomic_claim(plugin):
    # claimed atomic, but two deltas is coupled work -> thresholds win (branch)
    res = _check(plugin, ATOMIC, deltas=2, atomic=True)
    assert res["verdict"] == "branch"
    assert res["mismatch"] == "under-decomposition"

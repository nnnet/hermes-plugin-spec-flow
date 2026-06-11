"""Battle test — the closed self-improvement loop (roadmap E).

A revision is not done when the spec is re-derived: the loop CLOSES only when the
gate the signal failed is re-run and now passes. The runner re-checks after every
revision (signal → revision → respec → re-derive → RE-CHECK → resolved) and
records the verdict. A revision may declare ``recheck_fails: N`` to model an
improvement that needs more than one pass, or ``recheck: false`` to opt out.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

_METRICS_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
                   "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_METRICS_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                 "open_decisions": 0, "single_concern": True, "testable_criteria": True}


def _project(revisions):
    return {
        "name": "self-improve-case",
        "goal": "service whose design surfaces a fixable signal",
        "target": "x correct",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {
            "id": "L0", "title": "Service", "metrics": _METRICS_BRANCH,
            "children": [
                {"id": "core", "title": "Core", "metrics": _METRICS_BRANCH,
                 "children": [
                     {"id": "alpha", "title": "Alpha", "metrics": _METRICS_LEAF},
                     {"id": "beta", "title": "Beta", "metrics": _METRICS_LEAF},
                 ]},
            ],
        },
        "revisions": revisions,
    }


def _run(plugin, tmp_path, revisions):
    return eng.run_project(_project(revisions),
                           workspace=str(tmp_path / "wk"), tools=plugin.tools)


# ─── the loop closes: re-check confirms the signal is resolved ────────


def test_revision_is_rechecked_and_resolved(plugin, tmp_path):
    res = _run(plugin, tmp_path, [{
        "method": "level_return", "trigger": "on_level_return",
        "finding": "design review surfaces a fixable flaw in core",
        "invalidates": "core", "effect": "tighten core spec, re-derive children",
    }])
    verified = [l for l in res.loops if l["type"] == "revision-verified"]
    assert verified and verified[0]["resolved"] is True
    assert verified[0]["task"] == "core"


def test_recheck_iterates_until_resolved(plugin, tmp_path):
    res = _run(plugin, tmp_path, [{
        "method": "level_return", "trigger": "on_level_return",
        "finding": "stubborn signal needs two passes",
        "invalidates": "core", "effect": "fix, re-derive",
        "recheck_fails": 2,
    }])
    rechecks = [l for l in res.loops if l["type"] == "revision-recheck"]
    verified = [l for l in res.loops if l["type"] == "revision-verified"]
    assert len(rechecks) == 2 and all(r["resolved"] is False for r in rechecks)
    assert len(verified) == 1 and verified[0]["resolved"] is True


def test_recheck_can_be_opted_out(plugin, tmp_path):
    res = _run(plugin, tmp_path, [{
        "method": "level_return", "trigger": "on_level_return",
        "finding": "tracked but not re-verified here",
        "invalidates": "core", "effect": "re-derive only",
        "recheck": False,
    }])
    assert not any(l["type"] == "revision-verified" for l in res.loops)
    # the revision itself still fired
    assert any(l["type"] == "revision-respec" for l in res.loops)


def test_respec_gate_recheck_emitted(plugin, tmp_path):
    res = _run(plugin, tmp_path, [{
        "method": "level_return", "trigger": "on_level_return",
        "finding": "signal", "invalidates": "core", "effect": "fix",
    }])
    passes = [e for e in res.events
              if e.gate == "respec_gate" and e.verdict == "PASS"]
    assert passes, "a passing respec-gate re-check closes the loop"


def test_oracle_sees_self_improve_episode(plugin, tmp_path):
    res = _run(plugin, tmp_path, [{
        "method": "level_return", "trigger": "on_level_return",
        "finding": "signal", "invalidates": "core", "effect": "fix",
    }])
    summary = plugin.tools.summarize_trace([vars(e) for e in res.events])
    rep = plugin.tools.check_oracle(res, {"expected_episodes": ["self_improve"]}, summary)
    assert rep.ok, plugin.tools.render_oracle(rep)


def test_no_revision_no_self_improve(plugin, tmp_path):
    res = _run(plugin, tmp_path, [])
    assert not any(l["type"] == "revision-verified" for l in res.loops)
    summary = plugin.tools.summarize_trace([vars(e) for e in res.events])
    rep = plugin.tools.check_oracle(res, {"expected_episodes": ["self_improve"]}, summary)
    assert not rep.ok  # the episode must be absent without a revision
